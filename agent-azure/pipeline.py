"""
pipeline.py - Main entry point for the agent-azure support pipeline.

Runs the full classify → orchestrate → eval → improve loop over emails.csv.

Usage:
    python pipeline.py
    python pipeline.py --limit 5 --no-improve
    python pipeline.py --limit 10 --min-score 4.0
    python pipeline.py --no-eval --limit 3
"""

import argparse
import csv
import os
from dataclasses import dataclass
from pathlib import Path

from logger import get_logger  # must be first — silences third-party loggers
from dotenv import load_dotenv
from azure.ai.agents import AgentsClient
from azure.identity import DefaultAzureCredential

from classifier import classify, create_agent as create_classifier_agent
from evaluator import append_section, init_output, judge
from guardrails import GuardrailError
from improver import apply_proposals, create_agents as create_improver_agents, generate_proposals
from orchestrator_agent import orchestrate
from skills import all_skills, rollback
from tools import ALL_TOOLS
from store import (
    REGRESSION_THRESHOLD,
    add_training_email,
    append_run_result,
    get_training,
)
from tracing import setup_tracing


@dataclass
class _AgentPool:
    classifier: object
    improver: object
    kb_merger: object
    guideline_merger: object


def _create_pool(client: AgentsClient) -> _AgentPool:
    imp, kb, gl = create_improver_agents(client)
    return _AgentPool(
        classifier=create_classifier_agent(client),
        improver=imp,
        kb_merger=kb,
        guideline_merger=gl,
    )


def _delete_pool(client: AgentsClient, pool: _AgentPool) -> None:
    for agent in (pool.classifier, pool.improver, pool.kb_merger, pool.guideline_merger):
        try:
            client.delete_agent(agent.id)
        except Exception:
            pass

load_dotenv(Path(__file__).parent / ".env")

log = get_logger(__name__)

DATA_DIR = Path(__file__).parent / "data"
EMAILS_CSV = DATA_DIR / "emails.csv"


def _load_emails(limit: int, offset: int, language: str = "en") -> list[dict]:
    """Load emails from CSV filtered by language, applying offset then limit."""
    if not EMAILS_CSV.exists():
        raise FileNotFoundError(f"emails.csv not found at {EMAILS_CSV}.")

    emails = []
    skipped = 0
    with open(EMAILS_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if language and row.get("language") != language:
                continue
            if skipped < offset:
                skipped += 1
                continue
            if len(emails) >= limit:
                break
            emails.append({
                "subject":  row.get("subject", ""),
                "body":     row.get("body", ""),
                "queue":    row.get("queue", ""),
                "priority": row.get("priority", ""),
                "answer":   row.get("answer", ""),
            })
    return emails


def _log_steps(r) -> None:
    """Emit DEBUG traces for the steps of one specialist agent run."""
    if not r.steps_log:
        return
    log.debug("      %-5s  %-18s  detail", "step", "type")
    log.debug("      %s  %s  %s", "─"*5, "─"*18, "─"*50)
    for s in r.steps_log:
        if s["type"] == "function":
            log.debug("      %-5s  %-18s  args=%s",
                      s["step"], "fn: " + s["name"], s["args"])
        elif s["type"] == "code_interpreter":
            n_out = s.get("output_count", "?")
            out_label = (
                s["output"].replace("\n", " | ")[:300]
                if s["output"]
                else f"(no output — {n_out} output object(s) in response)"
            )
            log.debug("      %-5s  %-18s  output: %s",
                      s["step"], "code_interpreter", out_label)
            log.debug("      %-5s  %-18s  code:   %s",
                      "", "", s["code"].replace("\n", "↵ ")[:250])
        elif s["type"] == "file_search":
            log.debug("      %-5s  %-18s  files=%s",
                      s["step"], "file_search", s["files"])


def _run_email(client, email, i, args, vector_store_id, tracer, out_path, pool: _AgentPool) -> tuple[dict | None, dict]:
    """Classify, orchestrate, eval one email.

    Returns (section, classification).
    section is None when eval is off, no ground truth, or no reply generated.
    Raises GuardrailError or Exception on hard failures.
    """
    subject = email.get("subject") or "(no subject)"
    print(f"\n EMAIL [{i}]: {subject[:65]}")
    print("-" * 70)

    with tracer.start_as_current_span("pipeline.email") as span:
        span.set_attribute("email.index", i)
        span.set_attribute("email.subject", subject[:120])

        # Classify
        with tracer.start_as_current_span("pipeline.classify") as cls_span:
            classification = classify(client, email, agent=pool.classifier)
            cls_span.set_attribute("classification.queue", classification["queue"])
            cls_span.set_attribute("classification.priority", classification["priority"])
            cls_span.set_attribute("classification.type", classification["type"])
        print(f"  [classifier]   queue={classification['queue']}  "
              f"priority={classification['priority']}  type={classification['type']}")
        print(f"                 reason: {classification.get('reason', '')}")
        span.set_attribute("classification.queue", classification["queue"])

        # Orchestrate
        result = orchestrate(client, email, classification, vector_store_id, tracer)
        multi = len(result.agents_used) > 1
        print(f"  [orchestrator] agents={result.agents_used}  "
              f"{'MULTI-AGENT  ' if multi else ''}"
              f"action={result.action}  escalated={result.escalated}")

        skills_str = ", ".join(r.skill_name for r in result.results)
        all_tools = [t for r in result.results for t in r.tools_called]
        tools_str = ", ".join(all_tools) if all_tools else "(none)"

        for r in result.results:
            searched = (f"  kb={r.files_searched}" if r.files_searched else "")
            print(f"    ↳ [{r.agent_key}]  skill={r.skill_name}  "
                  f"ticket={r.ticket_id or '(none)'}  tools={r.tools_called}{searched}")
            _log_steps(r)

        if result.ticket_ids:
            print(f"  [tickets]      {', '.join(result.ticket_ids)}")

        if result.final_reply:
            preview = result.final_reply.replace("\n", " ")[:220]
            print(f"  [final reply]  {preview}...")

        if not args.eval:
            return None, classification

        # Eval
        ground_truth = email.get("answer") or ""
        if not ground_truth:
            print("  [eval]         skipped — no ground truth")
            return None, classification

        generated = result.final_reply or ""
        if not generated:
            print("  [eval]         skipped — no reply generated")
            return None, classification

        with tracer.start_as_current_span("eval") as eval_span:
            scores = judge(email, ground_truth, generated)
            avg = scores["avg"]
            eval_span.set_attribute("eval.avg", avg)
            eval_span.set_attribute("eval.groundedness", scores["groundedness"])
            eval_span.set_attribute("eval.relevance", scores["relevance"])
            eval_span.set_attribute("eval.coherence", scores["coherence"])
            eval_span.set_attribute("eval.fluency", scores["fluency"])

        print(f"  [eval]         groundedness={scores['groundedness']}/5  "
              f"relevance={scores['relevance']}/5  "
              f"coherence={scores['coherence']}/5  fluency={scores['fluency']}/5  avg={avg:.1f}  "
              f"comment: {scores['comment']}")

        section = {
            "index":          i,
            "subject":        subject,
            "body":           email.get("body") or "",
            "queue":          classification["queue"],
            "type":           classification["type"],
            "priority":       classification["priority"],
            "skills":         skills_str,
            "tools":          tools_str,
            "files_searched": [f for r in result.results for f in r.files_searched],
            "ground_truth":   ground_truth,
            "generated":      generated,
            "score":          scores,
            "avg":            avg,
        }
        append_run_result(section)
        if args.save:
            append_section(section, out_path)

        return section, classification


def _run_improve(
    client, section, classification, skill_map, tally, vector_store_id, tracer, pool: _AgentPool
) -> dict:
    """Propose, apply, and regression-check improvements. Returns updated skill_map."""
    skill_name = section["skills"].split(",")[0].strip()
    skill_info = skill_map.get(skill_name)
    print(f"  [improve]      analysing skill '{skill_name}' …")

    try:
        with tracer.start_as_current_span("improve") as imp_span:
            proposals = generate_proposals(client, skill_name, skill_info, section, agent=pool.improver)
            imp_span.set_attribute("proposals.count", len(proposals))
            print(f"  [improve]      {len(proposals)} proposal(s)")
            for p in proposals:
                target = p.get("entry", {}).get("topic", skill_name)
                print(f"     {p['type'].upper():14}  {target}  — {p['rationale'][:80]}")

            if proposals:
                apply_proposals(
                    client, proposals, vector_store_id,
                    kb_merger_agent=pool.kb_merger,
                    guideline_merger_agent=pool.guideline_merger,
                )
                skill_map = all_skills()
                for p in proposals:
                    if p["type"] in tally:
                        tally[p["type"]] += 1

            # Regression: re-eval training emails for this skill
            training_emails = get_training(skill_name)
            if training_emails:
                failures = []
                for te in training_emails:
                    te_email = {"subject": te["subject"], "body": te["body"]}
                    te_cls = classify(client, te_email, agent=pool.classifier)
                    te_result = orchestrate(client, te_email, te_cls, vector_store_id, tracer)
                    te_generated = te_result.final_reply or ""
                    if te_generated:
                        te_scores = judge(te_email, te["answer"], te_generated)
                        if te_scores["avg"] < REGRESSION_THRESHOLD:
                            failures.append({"subject": te["subject"], "avg": te_scores["avg"]})
                if failures:
                    print(f"  [regression]   WARN {len(failures)} email(s) below {REGRESSION_THRESHOLD}:")
                    for f in failures:
                        print(f"     avg={f['avg']:.1f}  {f['subject'][:60]}")
                    if any(p["type"] == "skill_edit" for p in proposals):
                        agent_key = classification.get("agent_key", "general")
                        reverted = rollback(agent_key, skill_name)
                        if reverted:
                            skill_map = all_skills()
                            print(f"  [regression]   reverted '{skill_name}' to previous version")
                        else:
                            print(f"  [regression]   could not revert '{skill_name}' — no previous version")
                else:
                    print(f"  [regression]   ok ({len(training_emails)} emails checked)")

            # Add to training set
            ground_truth = section.get("ground_truth", "")
            if ground_truth:
                added = add_training_email(skill_name, section["subject"], section["body"], ground_truth)
                if added:
                    tally["training_added"] += 1
                    print(f"  [training]     added to regression set for '{skill_name}'")

    except Exception as exc:
        print(f"  [improve]      error: {exc}")

    return skill_map


def _print_summary(output_sections, tally, args, out_path) -> None:
    """Print post-loop eval summary."""
    if not (args.eval and output_sections):
        return
    n = len(output_sections)
    avg_groundedness = sum(s["score"]["groundedness"] for s in output_sections) / n
    avg_relevance    = sum(s["score"]["relevance"]    for s in output_sections) / n
    avg_coherence    = sum(s["score"]["coherence"]    for s in output_sections) / n
    avg_fluency      = sum(s["score"]["fluency"]      for s in output_sections) / n
    overall          = (avg_groundedness + avg_relevance + avg_coherence + avg_fluency) / 4
    print(f"\nEVAL SUMMARY ({n} emails scored)")
    print(f"  groundedness: {avg_groundedness:.1f}/5")
    print(f"  relevance:    {avg_relevance:.1f}/5")
    print(f"  coherence:    {avg_coherence:.1f}/5")
    print(f"  fluency:      {avg_fluency:.1f}/5")
    print(f"  overall:      {overall:.1f}/5")
    if args.improve:
        print(f"  skills:       {tally['skill_edit']} edited, {tally['new_skill']} new")
        print(f"  kb entries:   {tally['kb_entry']}")
        print(f"  guidelines:   {tally['agent_guideline']}")
        print(f"  training set: {tally['training_added']} added")
    if args.save:
        print(f"\n  Saved to {out_path}")
        print(f"  Results appended to data/pipeline_results.json")


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-azure support pipeline")
    parser.add_argument("--eval",    default=True, action=argparse.BooleanOptionalAction,
                        help="Run LLM-as-judge scoring (default: on)")
    parser.add_argument("--improve", default=True, action=argparse.BooleanOptionalAction,
                        help="Generate and apply improvement proposals (requires --eval, default: on)")
    parser.add_argument("--limit",    type=int, default=3)
    parser.add_argument("--offset",   type=int, default=0)
    parser.add_argument("--language", type=str, default="en",
                        help="Filter emails by language code (default: en)")
    parser.add_argument("--min-score", type=float, default=4.5,
                        help="Threshold below which improve is triggered (default: 4.5)")
    parser.add_argument("--save",    default=True, action=argparse.BooleanOptionalAction,
                        help="Write eval_output.md (default: on)")
    args = parser.parse_args()

    if args.improve and not args.eval:
        parser.error("--improve requires --eval")

    vector_store_id = os.environ.get("VECTOR_STORE_ID", "")
    if not vector_store_id:
        print("ERROR: VECTOR_STORE_ID not set. Run kb_setup.py first.")
        return

    client = AgentsClient(
        endpoint=os.environ["PROJECT_ENDPOINT"],
        credential=DefaultAzureCredential(),
    )
    client.enable_auto_function_calls(set(ALL_TOOLS.values()))
    tracer = setup_tracing()

    mode_parts = []
    if args.eval:
        mode_parts.append("EVAL")
    if args.improve:
        mode_parts.append("IMPROVE")
    mode_tag = "+".join(mode_parts)

    print(f"Pipeline starting — {args.limit} email(s), language={args.language}"
          f"{' [' + mode_tag + ']' if mode_tag else ''}")
    print("=" * 70)

    out_path = "eval_output.md"
    if args.eval and args.save:
        init_output(out_path)

    emails = _load_emails(args.limit, args.offset, args.language)
    output_sections: list[dict] = []
    skill_map = all_skills() if args.improve else {}
    tally = {
        "skill_edit": 0, "new_skill": 0,
        "kb_entry": 0, "agent_guideline": 0,
        "training_added": 0,
    }

    pool = _create_pool(client)
    try:
        for i, email in enumerate(emails, args.offset + 1):
            try:
                section, classification = _run_email(
                    client, email, i, args, vector_store_id, tracer, out_path, pool
                )
            except GuardrailError as e:
                print(f"  [guardrail]    BLOCKED — {e}")
                print("=" * 70)
                continue
            except Exception as exc:
                print(f"  [error]        {exc}")
                print("=" * 70)
                continue

            if section is not None:
                output_sections.append(section)
                if args.improve and section["avg"] < args.min_score:
                    skill_map = _run_improve(
                        client, section, classification, skill_map, tally, vector_store_id, tracer, pool
                    )

            print("=" * 70)
    finally:
        _delete_pool(client, pool)

    _print_summary(output_sections, tally, args, out_path)


if __name__ == "__main__":
    main()
