"""
pipeline.py — Unified entry point for the LangGraph support agent pipeline.

Modes:
  --eval     → run LLM-as-judge scoring after each email
  --improve  → after eval, generate improvement proposals (requires --eval)
  --apply    → apply proposals to DB immediately (requires --improve)
  --resume   → resume an interrupted (escalated) pipeline run
  --decision → human decision to pass when resuming ("approve" or "override: <text>")

Usage:
    python pipeline.py --limit 2
    python pipeline.py --no-improve --limit 3
    python pipeline.py --no-eval --limit 3
    python pipeline.py --resume email-<run_id>-<index> --decision "approve"
    python pipeline.py --resume email-<run_id>-<index> --decision "override: Please refund immediately"

This is structurally identical to agent-cli/pipeline.py but uses the compiled
LangGraph StateGraph instead of orchestrate(). Key differences:

  agent-cli:  orchestrate(classification, email) → OrchestratorResult
  agent-langgraph: compiled_graph.ainvoke(initial_state, config) → PipelineState

The graph handles screening, sanitization, classification, decomposition,
fan-out, and merging internally. Eval and improve are also graph nodes.
"""

import argparse
import asyncio
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv
from langgraph.types import Command

from checkpointer import get_checkpointer
from client import Client
from email_stream import email_stream
from evaluator import init_output, append_section
from graph import build_main_graph
from logger import get_logger
import store as kb
import skills as skills_db

load_dotenv(Path(__file__).parent / ".env")
log = get_logger(__name__)


# ── Pipeline runner ────────────────────────────────────────────────────────────

async def run_email(
    email: dict,
    compiled_graph,
    thread_id: str,
    run_eval: bool,
    run_improve: bool = True,
) -> dict:
    """Run one email through the compiled graph. Returns final PipelineState."""
    initial_state = {
        "email": email,
        "screen_passed": True,
        "screen_reason": "",
        "classification": {"queue": "", "priority": "", "type": "", "reason": ""},
        "agent_keys": [],
        "parallel": True,
        "agent_results": [],
        "final_reply": "",
        "action": "pending",
        "escalation_pending": False,
        "human_decision": None,
        "eval_score": None,
        "eval_avg": None,
        "run_improve": run_improve,
    }
    config = {"configurable": {"thread_id": thread_id}}
    return await compiled_graph.ainvoke(initial_state, config=config)


async def resume_pipeline(thread_id: str, decision: str, compiled_graph) -> dict:
    """Resume an interrupted pipeline after a human decision."""
    config = {"configurable": {"thread_id": thread_id}}
    return await compiled_graph.ainvoke(Command(resume=decision), config=config)


async def serve_mode(compiled_graph) -> None:
    """
    Long-running loop: poll escalation_queue for human decisions and resume them.

    This is the sole process that calls ainvoke(Command(resume=...)) — the UI
    backend only writes decisions to the DB and never imports the graph directly.

    Status transitions handled here:
      decided → (resume) → approved | overridden
    """
    print("Pipeline service running — polling for human decisions every 5s  (Ctrl+C to stop)")
    while True:
        decided = await kb.get_decided_escalations()
        for row in decided:
            thread_id = row["thread_id"]
            decision  = row["human_decision"]
            log.info("serve: resuming %s with decision %r", thread_id, decision)
            print(f"\n  [serve] Resuming {thread_id}  decision={decision!r}")
            try:
                final_state = await resume_pipeline(thread_id, decision, compiled_graph)
                status = "overridden" if decision.lower().startswith("override") else "approved"
                await kb.resolve_escalation(thread_id, status, decision)
                _print_result(final_state)
            except Exception as exc:
                log.error("serve: failed to resume %s: %s", thread_id, exc)
                print(f"  [serve] ERROR resuming {thread_id}: {exc}")
        await asyncio.sleep(5)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="LangGraph support agent pipeline — StateGraph + Send API + interrupt()"
    )
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
    parser.add_argument("--min-score",        type=float, default=4.5,
                        help="Threshold for triggering improve step (default: 4.5)")
    # Human-in-the-loop resume
    parser.add_argument("--resume",   type=str, default=None,
                        help="Thread ID to resume after an escalation interrupt")
    parser.add_argument("--decision", type=str, default=None,
                        help="Human decision when resuming: 'approve' or 'override: <text>'")
    parser.add_argument("--serve",    default=True, action=argparse.BooleanOptionalAction,
                        help="After batch processing, poll for human decisions and resume "
                             "interrupted pipelines (default: true)")
    args = parser.parse_args()

    run_eval    = args.eval
    run_improve = args.improve and run_eval

    if args.improve and not args.eval:
        parser.error("--improve requires --eval")

    # ── DB setup ─────────────────────────────────────────────────────────────
    await kb.get_pool()
    await skills_db.get_pool()

    # ── Build graph ──────────────────────────────────────────────────────────
    checkpointer = await get_checkpointer()
    compiled = build_main_graph(checkpointer=checkpointer)

    # ── Resume mode ──────────────────────────────────────────────────────────
    if args.resume:
        if not args.decision:
            parser.error("--resume requires --decision")
        print(f"\nResuming thread '{args.resume}' with decision: {args.decision!r}")
        final_state = await resume_pipeline(args.resume, args.decision, compiled)
        _print_result(final_state, include_internal=args.internal_summary)
        return

    # ── Serve-only mode (--limit 0 --serve) ──────────────────────────────────
    if args.limit == 0 and args.serve:
        await serve_mode(compiled)
        return

    # ── Normal pipeline mode ─────────────────────────────────────────────────
    run_id = await kb.create_run(args.limit, args.offset, args.language)
    run_uuid = str(run_id)

    out_path = "eval_output.md"
    if run_eval and args.save:
        init_output(out_path)

    client = Client()
    output_sections: list[dict] = []

    # Counters for improve tally
    tally: dict[str, int] = {
        "skill_edit": 0, "new_skill": 0,
        "kb_entry": 0, "agent_guideline": 0,
    }

    mode_tag = "EVAL" if run_eval else ""
    if run_improve:
        mode_tag += "+IMPROVE"
    if args.apply:
        mode_tag += "+APPLY"
    print(f"Pipeline starting — {args.limit} email(s), language={args.language}"
          f"{' [' + mode_tag + ']' if mode_tag else ''}")
    print(f"Graph: StateGraph + Send API + interrupt() | checkpointer: AsyncPostgresSaver")
    print("=" * 70)

    for i, email in enumerate(
            email_stream(language=args.language, limit=args.limit,
                         offset=args.offset, shuffle=args.shuffle),
            args.offset + 1,
        ):
        subject = email.get("subject") or "(no subject)"
        print(f"\n EMAIL: {subject[:65]}")
        print("-" * 70)

        thread_id = f"email-{run_uuid}-{i}"

        try:
            final_state = await run_email(email, compiled, thread_id, run_eval, run_improve)

            # Check if pipeline paused due to escalation interrupt
            if not final_state.get("screen_passed") and final_state.get("screen_reason"):
                screen_reason = final_state.get("screen_reason", "")
                print(f"  [screener]    QUARANTINED — {screen_reason}")
                print("=" * 70)
                continue

            cls = final_state.get("classification") or {}
            if cls.get("queue"):
                print(f"  [classifier]  queue={cls.get('queue')}  "
                      f"priority={cls.get('priority')}  type={cls.get('type')}")
                print(f"                reason: {cls.get('reason', '')}")

            results = final_state.get("agent_results") or []
            if results:
                agents_used = [r.get("agent_key") for r in results]
                multi = len(results) > 1
                any_escalated = any(r.get("escalated") for r in results)
                print(f"  [graph]       agents={agents_used}  "
                      f"{'MULTI-AGENT  ' if multi else ''}"
                      f"action={final_state.get('action')}  escalated={any_escalated}")
                for r in results:
                    tools_used = [c["tool"] for c in (r.get("tool_calls") or [])]
                    print(f"    ↳ [{r.get('skill_used')}]  ticket={r.get('ticket_id') or '(none)'}  "
                          f"tools={tools_used}")

            # Check if interrupted waiting for human
            escalated_but_no_decision = (
                any(r.get("escalated") for r in results)
                and final_state.get("human_decision") is None
            )
            if escalated_but_no_decision:
                print(f"  [interrupt]   PAUSED — escalation requires human review")
                print(f"                Resume: python pipeline.py --resume {thread_id} --decision 'approve'")
                print("=" * 70)
                continue

            final_reply = final_state.get("final_reply", "")
            if final_reply:
                preview = final_reply.replace("\n", " ")[:220]
                print(f"  [final reply] {preview}...")

            if not run_eval:
                print("=" * 70)
                continue

            # ── Eval results ─────────────────────────────────────────────────
            ground_truth = email.get("answer") or ""
            if not ground_truth:
                print("  [eval]        skipped — no ground truth")
                print("=" * 70)
                continue

            if not final_reply:
                print("  [eval]        skipped — no reply generated")
                print("=" * 70)
                continue

            score = final_state.get("eval_score")
            avg   = final_state.get("eval_avg")

            if score and avg is not None:
                print(f"  [eval]        action={score['action']}/5  "
                      f"completeness={score['completeness']}/5  "
                      f"tone={score['tone']}/5  avg={avg:.1f}  "
                      f"comment: {score.get('comment', '')}")

                skills_str = ", ".join(r.get("skill_used", "") for r in results)
                all_tools  = [c["tool"] for r in results for c in (r.get("tool_calls") or [])]
                tools_str  = ", ".join(all_tools) if all_tools else "(none)"

                section = {
                    "index":            i,
                    "subject":          subject,
                    "body":             email.get("body") or "",
                    "queue":            cls.get("queue", ""),
                    "type":             cls.get("type", ""),
                    "priority":         cls.get("priority", ""),
                    "skills":           skills_str,
                    "tools":            tools_str,
                    "ground_truth":     ground_truth,
                    "generated":        final_reply,
                    "internal_summary": results[0].get("internal_summary", "") if results else "",
                    "score":            score,
                    "avg":              avg,
                }
                output_sections.append(section)
                await kb.store_result(run_id, section)
                if args.save:
                    append_section(section, out_path, include_internal_summary=args.internal_summary)

                if run_improve and avg < args.min_score:
                    print(f"  [improve]     avg={avg:.1f} < {args.min_score} — proposals generated by graph node")

        except Exception as exc:
            log.exception("Error processing email '%s': %s", subject, exc)
            print(f"  [error]       {exc}")

        print("=" * 70)

    # ── Serve mode (poll for human decisions after batch) ────────────────────
    if args.serve:
        await serve_mode(compiled)
        return

    # ── Summary ──────────────────────────────────────────────────────────────
    if not run_eval:
        print(f"\n{client.usage_summary()}")
        return

    await kb.update_run_stats(run_id, output_sections)

    if output_sections:
        n = len(output_sections)
        avg_action       = sum(s["score"]["action"]       for s in output_sections) / n
        avg_completeness = sum(s["score"]["completeness"] for s in output_sections) / n
        avg_tone         = sum(s["score"]["tone"]         for s in output_sections) / n
        overall          = (avg_action + avg_completeness + avg_tone) / 3
        print(f"\nEVAL SUMMARY ({n} emails scored)")
        print(f"  action:       {avg_action:.1f}/5")
        print(f"  completeness: {avg_completeness:.1f}/5")
        print(f"  tone:         {avg_tone:.1f}/5")
        print(f"  overall:      {overall:.1f}/5")
        print(f"  {client.usage_summary()}")

    if args.save and output_sections:
        print(f"\nSaved to {out_path}")

    # ── Graph visualisation hint ──────────────────────────────────────────────
    print("\nTip: visualise the graph with:")
    print("  python -c \"from graph import build_main_graph; g = build_main_graph(); "
          "print(g.get_graph().draw_mermaid())\" > graph.md")


def _print_result(state: dict, include_internal: bool = False) -> None:
    """Print a single email result from a resumed pipeline."""
    cls = state.get("classification") or {}
    results = state.get("agent_results") or []
    print(f"\n  queue={cls.get('queue')}  priority={cls.get('priority')}  type={cls.get('type')}")
    for r in results:
        print(f"  [{r.get('skill_used')}]  ticket={r.get('ticket_id')}  action={r.get('action')}")
    final_reply = state.get("final_reply", "")
    if final_reply:
        print(f"\n  [final reply]\n{final_reply}")
    score = state.get("eval_score")
    if score:
        avg = state.get("eval_avg", 0)
        print(f"\n  [eval]  action={score['action']}/5  completeness={score['completeness']}/5  "
              f"tone={score['tone']}/5  avg={avg:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
