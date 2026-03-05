"""
pipeline.py - Unified entry point for the support agent pipeline.

Modes:
  --eval     → run LLM-as-judge scoring after each email
  --improve  → after eval, generate improvement proposals (requires --eval)
  --apply    → apply proposals to DB immediately (requires --improve)

Usage:
    python pipeline.py --limit 2
    python pipeline.py --no-improve --limit 3
    python pipeline.py --no-eval --limit 3
"""

import argparse

from dotenv import load_dotenv
from client import Client

from email_stream import email_stream
from classifier import classify
from orchestrator_agent import orchestrate
from input_screener import screen_email
from email_sanitizer import sanitize
from evaluator import judge, write_output
from improver import (
    load_kb,
    load_all_skills,
    generate_proposals,
    apply_proposals,
)
import kb
import skills as skills_db

load_dotenv()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--eval",             default=True, action=argparse.BooleanOptionalAction,
                        help="Run LLM-as-judge scoring after each email (default: true)")
    parser.add_argument("--improve",          default=True, action=argparse.BooleanOptionalAction,
                        help="Generate improvement proposals (requires --eval, default: true)")
    parser.add_argument("--apply",            default=True, action=argparse.BooleanOptionalAction,
                        help="Apply proposals to DB immediately (requires --improve, default: true)")
    parser.add_argument("--limit",            type=int,   default=3)
    parser.add_argument("--offset",           type=int,   default=0)
    parser.add_argument("--language",         type=str,   default="en")
    parser.add_argument("--shuffle",          action="store_true", default=False)
    parser.add_argument("--screen",           default=True, action=argparse.BooleanOptionalAction)
    parser.add_argument("--save",             default=True, action=argparse.BooleanOptionalAction,
                        help="Write eval_output.md when --eval is set (default: true)")
    parser.add_argument("--internal-summary", default=False, action=argparse.BooleanOptionalAction)
    parser.add_argument("--min-score",        type=float, default=4.6,
                        help="Threshold for failing emails in improve step (default: 4.0)")
    args = parser.parse_args()

    run_eval    = args.eval
    run_improve = args.improve
    apply       = args.apply

    if run_improve and not run_eval:
        parser.error("--improve requires --eval")

    await kb.get_pool()
    await skills_db.get_pool()

    client = Client()
    output_sections: list[dict] = []

    all_skills = load_all_skills() if run_improve else {}
    kb_entries = load_kb()         if run_improve else []

    mode_tag = "EVAL" if run_eval else ""
    if run_improve:
        mode_tag += "+IMPROVE"
    if apply:
        mode_tag += "+APPLY"
    print(f"Pipeline starting — {args.limit} email(s), language={args.language}"
          f"{' [' + mode_tag + ']' if mode_tag else ''}\n")
    print("=" * 70)

    for i, email in enumerate(
        email_stream(language=args.language, limit=args.limit,
                     offset=args.offset, shuffle=args.shuffle),
        args.offset + 1,
    ):
        subject = email.get("subject") or "(no subject)"
        print(f"\n EMAIL: {subject[:65]}")
        print("-" * 70)

        try:
            # Screen
            if args.screen:
                screen = screen_email(client, email)
                if not screen.safe:
                    print(f"  [screener]    QUARANTINED (score={screen.risk_score}/10) — {screen.reason}")
                    print("=" * 70)
                    continue
                if screen.risk_score >= 3:
                    print(f"  [screener]    warning score={screen.risk_score}/10 — {screen.reason}")

            # Sanitize
            email, warnings = sanitize(email)
            if warnings:
                print(f"  [sanitizer]   stripped {len(warnings)} pattern(s)")

            # Classify
            classification = classify(client, email)
            print(f"  [classifier]  queue={classification['queue']}  "
                  f"priority={classification['priority']}  type={classification['type']}")
            print(f"                reason: {classification['reason']}")

            # Orchestrate
            result = await orchestrate(classification, email)
            multi = len(result.agents_used) > 1
            print(f"  [orchestrator] agents={result.agents_used}  "
                  f"{'MULTI-AGENT  ' if multi else ''}"
                  f"action={result.action}  escalated={result.escalated}")

            skills_str = ", ".join(sub.skill_used for sub in result.results)
            all_tools  = [c["tool"] for sub in result.results for c in sub.tool_calls]
            tools_str  = ", ".join(all_tools) if all_tools else "(none)"

            for sub in result.results:
                print(f"    ↳ [{sub.skill_used}]  ticket={sub.ticket_id or '(none)'}  "
                      f"tools={[c['tool'] for c in sub.tool_calls]}")

            if result.ticket_ids:
                print(f"  [tickets]     {', '.join(result.ticket_ids)}")

            if result.final_reply:
                preview = result.final_reply.replace("\n", " ")[:220]
                print(f"  [final reply] {preview}...")

            if not run_eval:
                print("=" * 70)
                continue

            # Eval
            ground_truth = email.get("answer") or ""
            if not ground_truth:
                print("  [eval]        skipped — no ground truth")
                print("=" * 70)
                continue

            generated = result.final_reply or ""
            if not generated:
                print("  [eval]        skipped — no reply generated")
                print("=" * 70)
                continue

            score = judge(client, email, ground_truth, generated)
            avg   = (score["action"] + score["completeness"] + score["tone"]) / 3
            print(f"  [eval]        action={score['action']}/5  completeness={score['completeness']}/5  "
                  f"tone={score['tone']}/5  avg={avg:.1f}  comment: {score['comment']}")

            internal_summary = result.results[0].internal_summary if result.results else ""
            section = {
                "index":            i,
                "subject":          subject,
                "body":             email.get("body") or "",
                "queue":            classification["queue"],
                "type":             classification["type"],
                "priority":         classification["priority"],
                "skills":           skills_str,
                "tools":            tools_str,
                "ground_truth":     ground_truth,
                "generated":        generated,
                "internal_summary": internal_summary,
                "score":            score,
                "avg":              avg,
            }
            output_sections.append(section)

            # Improve
            if run_improve and avg < args.min_score:
                skill_name = skills_str.split(",")[0].strip()
                skill_info = all_skills.get(skill_name)
                print(f"  [improve]     analysing skill '{skill_name}' …")
                try:
                    proposals = generate_proposals(client, skill_name, skill_info, [section], kb_entries)
                    print(f"  [improve]     {len(proposals)} proposal(s)")
                    for p in proposals:
                        target = p.get("skill_file") or p.get("entry", {}).get("id", "kb")
                        print(f"     {p['type'].upper():12}  {target}  — {p['rationale'][:80]}")
                    if proposals and apply:
                        await apply_proposals(client, proposals)
                        all_skills = load_all_skills()
                        kb_entries = load_kb()
                    elif proposals:
                        print("  [improve]     --no-apply: proposals not written to DB")
                except Exception as exc:
                    print(f"  [improve]     error: {exc}")

        except Exception as exc:
            print(f"  [error]       {exc}")

        print("=" * 70)

    if not run_eval:
        return

    # Aggregate stats
    if output_sections:
        print()
        n = len(output_sections)
        avg_action       = sum(s["score"]["action"]       for s in output_sections) / n
        avg_completeness = sum(s["score"]["completeness"] for s in output_sections) / n
        avg_tone         = sum(s["score"]["tone"]         for s in output_sections) / n
        overall          = (avg_action + avg_completeness + avg_tone) / 3
        print(f"EVAL SUMMARY ({n} emails scored)")
        print(f"  action:       {avg_action:.1f}/5")
        print(f"  completeness: {avg_completeness:.1f}/5")
        print(f"  tone:         {avg_tone:.1f}/5")
        print(f"  overall:      {overall:.1f}/5")

    if args.save and output_sections:
        write_output(output_sections, include_internal_summary=args.internal_summary)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
