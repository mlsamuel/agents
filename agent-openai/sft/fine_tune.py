"""
fine_tune.py - Upload training data and start an OpenAI fine-tuning job.

Run after generate_dataset.py:
    python sft/fine_tune.py

The fine-tuned model ID is printed on completion and written to data/sft/model_id.txt.
Add it to .env as FINETUNED_MODEL=ft:gpt-4o-mini-... to use it in the pipeline.

Flags:
    --model        Base model to fine-tune (default: gpt-4o-mini-2024-07-18)
    --suffix       Model name suffix for identification (default: cs-agent)
    --epochs       Training epochs (default: 3)
    --poll         Polling interval in seconds (default: 30)
"""

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).parent.parent / ".env")

SFT_DIR    = Path(__file__).parent.parent / "data" / "sft"
TRAIN_FILE = SFT_DIR / "train.jsonl"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fine-tune a model on the SFT dataset")
    parser.add_argument("--model",      default="gpt-4o-mini-2024-07-18")
    parser.add_argument("--suffix",     default="cs-agent",
                        help="Suffix appended to the fine-tuned model name")
    parser.add_argument("--epochs",     type=int, default=3)
    parser.add_argument("--poll",       type=int, default=30,
                        help="Status polling interval in seconds")
    parser.add_argument("--train-file", type=Path, default=TRAIN_FILE,
                        help="Path to training JSONL (default: data/sft/train.jsonl)")
    args = parser.parse_args()

    train_file = args.train_file
    if not train_file.exists():
        print(f"ERROR: {train_file} not found.")
        sys.exit(1)

    client = OpenAI()

    # Upload training file
    print(f"Uploading {train_file} …")
    with open(train_file, "rb") as f:
        upload = client.files.create(file=f, purpose="fine-tune")
    print(f"  File uploaded: {upload.id}")

    # Start fine-tuning job
    print(f"Starting fine-tune job (base={args.model}, epochs={args.epochs}) …")
    job = client.fine_tuning.jobs.create(
        training_file=upload.id,
        model=args.model,
        suffix=args.suffix,
        hyperparameters={"n_epochs": args.epochs},
    )
    print(f"  Job ID: {job.id}")
    print(f"  Status: {job.status}")

    # Poll until terminal state
    print(f"\nPolling every {args.poll}s …")
    terminal = {"succeeded", "failed", "cancelled"}
    while job.status not in terminal:
        time.sleep(args.poll)
        job = client.fine_tuning.jobs.retrieve(job.id)
        events = list(client.fine_tuning.jobs.list_events(job.id, limit=1))
        latest_msg = events[0].message if events else ""
        print(f"  [{job.status}] {latest_msg}")

    print(f"\nJob finished: {job.status}")

    if job.status != "succeeded":
        print(f"Fine-tuning failed. Check job {job.id} in the OpenAI dashboard.")
        sys.exit(1)

    model_id = job.fine_tuned_model
    print(f"Fine-tuned model: {model_id}")

    # Save model ID
    SFT_DIR.mkdir(parents=True, exist_ok=True)
    model_id_path = SFT_DIR / "model_id.txt"
    model_id_path.write_text(model_id, encoding="utf-8")
    print(f"Model ID saved to {model_id_path}")
    print(f"\nAdd to your .env:  FINETUNED_MODEL={model_id}")


if __name__ == "__main__":
    main()
