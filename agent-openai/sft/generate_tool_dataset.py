"""
generate_tool_dataset.py - Generate SFT training data with real tool-call traces.

Produces two JSONL files containing multi-turn conversations that include actual
tool invocations, results, and final customer replies:
  data/sft/train_tool.jsonl  — training examples (~20, stratified across 4 domains)
  data/sft/eval_tool.jsonl   — held-out eval examples (~10, no overlap with train)

How it works:
  For each email, gpt-4o (the teacher model) runs a Chat Completions tool-dispatch
  loop using the same skill prompt and tool implementations as the production pipeline.
  The full message sequence — system, user, tool_calls, tool results, final reply — is
  captured directly in fine-tuning format.

Why gpt-4o (not gpt-4o-mini):
  Teacher-student distillation. gpt-4o generates higher-quality tool-call traces
  (better tool selection, better argument construction, better reply quality).
  gpt-4o-mini learns from those traces, not from its own existing behaviour.

Why Chat Completions (not Assistants API):
  Chat Completions message lists are directly serializable to OpenAI fine-tuning JSONL.
  The tool implementations (tools.py ALL_TOOLS) are identical to what the pipeline uses.

Quality filter:
  Examples where gpt-4o made zero tool calls are discarded — they teach text generation
  without tool use, which the existing train.jsonl already covers.

Run after generate_guidelines.py (for consistent skill files):
    python sft/generate_tool_dataset.py

Flags:
    --train-per-domain   Target examples per domain in train set (default: 5)
    --eval-per-domain    Target examples per domain in eval set (default: 3)
    --oversample         Attempt this many emails per slot to hit target after filtering (default: 2)
    --model              Generation model (default: gpt-4o)
    --seed               Random seed (default: 42)
"""

import argparse
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from openai import OpenAI  # noqa: E402

from classifier import classify  # noqa: E402
from skills import load_skills, select_skill  # noqa: E402
from tools import ALL_TOOLS, TOOL_DEFINITIONS  # noqa: E402

DATA_DIR   = Path(__file__).parent.parent / "data"
EMAILS_CSV = DATA_DIR / "emails.csv"
SFT_DIR    = DATA_DIR / "sft"

MAX_TOOL_ROUNDS = 8  # safety limit per email

# Queue → domain key (matches classifier._QUEUE_TO_AGENT)
_QUEUE_TO_DOMAIN = {
    "Technical Support":               "technical_support",
    "IT Support":                      "technical_support",
    "Product Support":                 "technical_support",
    "Service Outages and Maintenance": "technical_support",
    "Billing and Payments":            "billing",
    "Returns and Exchanges":           "returns",
    "Customer Service":                "general",
    "Sales and Pre-Sales":             "general",
    "Human Resources":                 "general",
    "General Inquiry":                 "general",
}

# Tool defs by name, excluding send_reply.
# send_reply is an Assistants-API pattern — in Chat Completions the model ends with
# a text response, so including send_reply would corrupt the training target.
_TOOL_DEF_BY_NAME = {
    d["function"]["name"]: d
    for d in TOOL_DEFINITIONS
    if d["function"]["name"] != "send_reply"
}


def _load_emails_by_domain(seed: int) -> dict[str, list[dict]]:
    """Load English emails grouped by domain, shuffled."""
    by_domain: dict[str, list[dict]] = defaultdict(list)
    with open(EMAILS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("language") != "en":
                continue
            domain = _QUEUE_TO_DOMAIN.get(row.get("queue", ""), "")
            if not domain:
                continue
            by_domain[domain].append({
                "subject": row["subject"],
                "body":    row["body"],
                "queue":   row["queue"],
                "answer":  row.get("answer", ""),
            })

    rng = random.Random(seed)
    for domain in by_domain:
        rng.shuffle(by_domain[domain])
    return dict(by_domain)


def _tool_defs_for_skill(skill_tools: list[str]) -> list[dict]:
    """Return the Chat Completions tool defs for the tools declared in this skill."""
    defs = [_TOOL_DEF_BY_NAME[name] for name in skill_tools if name in _TOOL_DEF_BY_NAME]
    # Fall back to all non-send_reply tools if skill declares none
    return defs if defs else list(_TOOL_DEF_BY_NAME.values())


def _serialize_tool_call(tc) -> dict:
    """Convert an SDK ChatCompletionMessageToolCall object to a serialisable dict."""
    return {
        "id":       tc.id,
        "type":     "function",
        "function": {
            "name":      tc.function.name,
            "arguments": tc.function.arguments,
        },
    }


def _run_chat_tool_loop(
    client: OpenAI,
    model: str,
    system: str,
    user_msg: str,
    tool_defs: list[dict],
) -> list[dict] | None:
    """Run a Chat Completions tool-dispatch loop and return the full message list.

    Returns None if gpt-4o made zero tool calls (example filtered out).
    The returned list starts with the system message and ends with the final
    text reply — directly usable as the `messages` field in a fine-tuning example.
    """
    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user",   "content": user_msg},
    ]
    tool_call_count = 0

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tool_defs,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        if msg.tool_calls:
            tool_call_count += len(msg.tool_calls)
            # Append assistant turn — content must be null when tool_calls present
            messages.append({
                "role":       "assistant",
                "content":    None,
                "tool_calls": [_serialize_tool_call(tc) for tc in msg.tool_calls],
            })
            # Execute each tool and append result
            for tc in msg.tool_calls:
                try:
                    args   = json.loads(tc.function.arguments)
                    result = ALL_TOOLS[tc.function.name](**args)
                except Exception as exc:
                    result = json.dumps({"error": str(exc)})
                messages.append({
                    "role":         "tool",
                    "tool_call_id": tc.id,
                    "content":      result,
                })
        else:
            # Final text reply
            messages.append({"role": "assistant", "content": msg.content or ""})
            break

    if tool_call_count == 0:
        return None  # No tools used — filtered out

    # If MAX_TOOL_ROUNDS exhausted without a final text reply, the last message is
    # a tool result — invalid fine-tuning format (last message must be assistant).
    if messages[-1]["role"] != "assistant":
        return None

    return messages


def _make_example(messages: list[dict], tool_defs: list[dict]) -> dict:
    """Wrap messages and tool defs into the top-level fine-tuning dict."""
    return {"tools": tool_defs, "messages": messages}


# ── Checkpoint + incremental I/O ──────────────────────────────────────────────

CHECKPOINT_PATH = SFT_DIR / "generate_tool_checkpoint.json"


def _load_checkpoint() -> dict:
    if CHECKPOINT_PATH.exists():
        return json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
    return {}


def _save_checkpoint(checkpoint: dict) -> None:
    CHECKPOINT_PATH.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")


def _append_jsonl(path: Path, example: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(example, ensure_ascii=False) + "\n")


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _shuffle_jsonl(path: Path, rng: random.Random) -> None:
    """Read all lines, shuffle in-place, rewrite. Called once at the end."""
    if not path.exists():
        return
    lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rng.shuffle(lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SFT dataset with tool-call traces")
    parser.add_argument("--train-per-domain", type=int, default=5,
                        help="Target training examples per domain (default: 5)")
    parser.add_argument("--eval-per-domain",  type=int, default=3,
                        help="Target eval examples per domain (default: 3)")
    parser.add_argument("--oversample",       type=int, default=2,
                        help="Attempts per slot before giving up (default: 2)")
    parser.add_argument("--model",            default="gpt-4o",
                        help="Generation model — teacher for distillation (default: gpt-4o)")
    parser.add_argument("--seed",             type=int, default=42)
    parser.add_argument("--reset",            action="store_true",
                        help="Ignore checkpoint and start from scratch")
    args = parser.parse_args()

    train_path = SFT_DIR / "train_tool.jsonl"
    eval_path  = SFT_DIR / "eval_tool.jsonl"

    if args.reset:
        CHECKPOINT_PATH.unlink(missing_ok=True)
        train_path.unlink(missing_ok=True)
        eval_path.unlink(missing_ok=True)
        print("Checkpoint cleared — starting from scratch.")

    checkpoint       = _load_checkpoint()
    client           = OpenAI()
    emails_by_domain = _load_emails_by_domain(args.seed)

    total_attempted = 0
    total_filtered  = 0

    for domain, emails in emails_by_domain.items():
        domain_cp = checkpoint.setdefault(domain, {})

        targets = [
            (args.train_per_domain, train_path, "train"),
            (args.eval_per_domain,  eval_path,  "eval"),
        ]

        email_idx = 0  # shared index advances through train then eval

        for target_n, out_path, split in targets:
            split_cp  = domain_cp.setdefault(split, {"collected": 0, "email_idx": 0})
            collected = split_cp["collected"]
            email_idx = split_cp["email_idx"]

            if collected >= target_n:
                print(f"  [{domain}/{split}] already done ({collected}/{target_n}) — skipping")
                continue

            while collected < target_n and email_idx < len(emails):
                email      = emails[email_idx]
                email_idx += 1
                total_attempted += 1

                subject  = email["subject"]
                user_msg = f"Subject: {subject}\n\n{email['body']}"

                classification = classify(client, email)
                agent_key      = classification.get("agent_key", domain)
                email_type     = classification.get("type", "")

                agent_skills              = load_skills(agent_key)
                skill_name, skill_content = select_skill(agent_skills, email_type, subject)
                skill_tools               = agent_skills.get(skill_name, {}).get("tools", [])
                tool_defs                 = _tool_defs_for_skill(skill_tools)

                print(f"  [{domain}/{split} {collected+1}/{target_n}] "
                      f"{subject[:50]}  skill={skill_name} ...", end="", flush=True)

                messages = _run_chat_tool_loop(
                    client, args.model, skill_content, user_msg, tool_defs
                )

                # Save progress after every attempt (successful or not) so we
                # never re-attempt emails that were already processed on a prior run.
                split_cp["email_idx"] = email_idx
                _save_checkpoint(checkpoint)

                if messages is None:
                    total_filtered += 1
                    print(" filtered (no tool calls)")
                    continue

                tool_call_rounds = sum(
                    1 for m in messages
                    if m.get("role") == "assistant" and m.get("tool_calls")
                )
                print(f" ok ({tool_call_rounds} tool call round(s))")

                _append_jsonl(out_path, _make_example(messages, tool_defs))
                collected += 1
                split_cp["collected"] = collected
                _save_checkpoint(checkpoint)

            if collected < target_n:
                print(f"  WARNING [{domain}/{split}]: only {collected}/{target_n} examples collected")

        print(f"  [{domain}] train={domain_cp['train']['collected']}  "
              f"eval={domain_cp['eval']['collected']}")

    # Shuffle both files to mix domains (only meaningful on first full run; safe to repeat)
    rng = random.Random(args.seed)
    _shuffle_jsonl(train_path, rng)
    _shuffle_jsonl(eval_path, rng)

    n_train = _count_jsonl(train_path)
    n_eval  = _count_jsonl(eval_path)

    # Token estimate (rough: 1 token ≈ 4 chars)
    total_chars = sum(
        len(line)
        for path in (train_path,)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    est_tokens = total_chars // 4
    est_cost_per_epoch = est_tokens / 1000 * 0.003

    print(f"\nTrain: {n_train} examples → {train_path}")
    print(f"Eval:  {n_eval} examples  → {eval_path}")
    print(f"Generated: {total_attempted} attempts, {total_filtered} filtered (no tool calls)")
    print(f"Estimated training tokens: ~{est_tokens:,}  "
          f"(~${est_cost_per_epoch:.2f}/epoch · ~${est_cost_per_epoch * 3:.2f} for 3 epochs)")
    print(f"\nCheckpoint: {CHECKPOINT_PATH}")
    print("Re-run to collect more examples. Use --reset to start from scratch.")


if __name__ == "__main__":
    main()
