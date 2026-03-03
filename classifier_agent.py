"""
classifier_agent.py - Classifies incoming emails using Claude.

Usage:
    python classifier_agent.py              # classifies 5 English emails, prints results
    python classifier_agent.py --limit 20   # classifies 20 emails
"""

import json
import argparse
import anthropic
from dotenv import load_dotenv
from email_stream import email_stream

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"  # fast + cheap for classification

SYSTEM_PROMPT = """You are an email classifier for a customer support system.
Given an email subject and body, return a JSON object with these fields:

  - queue: one of the following (pick the single best match):

    Specialist queues (dedicated workflows exist):
      Technical Support, IT Support, Product Support, Service Outages and Maintenance,
      Billing and Payments, Returns and Exchanges, Customer Service, Sales and Pre-Sales,
      Human Resources, General Inquiry

    General topic queues (route to general handler):
      Arts & Entertainment/Movies, Arts & Entertainment/Music,
      Autos & Vehicles/Maintenance, Autos & Vehicles/Sales,
      Beauty & Fitness/Cosmetics, Beauty & Fitness/Fitness Training,
      Books & Literature/Fiction, Books & Literature/Non-Fiction,
      Business & Industrial/Manufacturing,
      Finance/Investments, Finance/Personal Finance,
      Food & Drink/Groceries, Food & Drink/Restaurants,
      Games,
      Health/Medical Services, Health/Mental Health,
      Hobbies & Leisure/Collectibles, Hobbies & Leisure/Crafts,
      Home & Garden/Home Improvement, Home & Garden/Landscaping,
      IT & Technology/Hardware Support, IT & Technology/Network Infrastructure,
      IT & Technology/Security Operations, IT & Technology/Software Development,
      Jobs & Education/Online Courses, Jobs & Education/Recruitment,
      Law & Government/Government Services, Law & Government/Legal Advice,
      News,
      Online Communities/Forums, Online Communities/Social Networks,
      People & Society/Culture & Society,
      Pets & Animals/Pet Services, Pets & Animals/Veterinary Care,
      Real Estate,
      Science/Environmental Science, Science/Research,
      Shopping/E-commerce, Shopping/Retail Stores,
      Sports,
      Travel & Transportation/Air Travel, Travel & Transportation/Land Travel

  - priority: one of [critical, high, medium, low, very_low]
  - type:     one of [Incident, Problem, Request, Change, Question, Complaint]
  - reason:   one short sentence explaining your classification

If the email has no subject line, rely entirely on the body for classification.
Respond with only valid JSON, no markdown fences."""


def classify(client: anthropic.Anthropic, email: dict) -> dict:
    subject = email.get("subject") or "(no subject)"
    body = (email.get("body") or "")[:1500]  # trim very long bodies

    message = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Subject: {subject}\n\nBody:\n{body}",
            }
        ],
    )

    raw = message.content[0].text.strip()
    # Strip markdown code fences if the model added them despite instructions
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    result = json.loads(raw)
    result["subject"] = subject
    result["ground_truth_queue"] = email.get("queue")
    result["ground_truth_priority"] = email.get("priority")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--language", type=str, default="en")
    parser.add_argument("--shuffle", action="store_true", default=False)
    args = parser.parse_args()

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env

    print(f"Classifying {args.limit} emails (language={args.language})...\n")

    correct_queue = 0
    correct_priority = 0
    total = 0

    for email in email_stream(language=args.language, limit=args.limit, shuffle=args.shuffle):
        try:
            result = classify(client, email)
            total += 1

            queue_match = result["queue"] == result["ground_truth_queue"]
            priority_match = result["priority"] == result["ground_truth_priority"]
            correct_queue += queue_match
            correct_priority += priority_match

            print(f"[{total}] {result['subject'][:60]}")
            print(f"     queue:    {result['queue']:<30} (ground truth: {result['ground_truth_queue']}) {'✓' if queue_match else '✗'}")
            print(f"     priority: {result['priority']:<30} (ground truth: {result['ground_truth_priority']}) {'✓' if priority_match else '✗'}")
            print(f"     type:     {result['type']}")
            print(f"     reason:   {result['reason']}")
            print()

        except Exception as e:
            print(f"  [error: {e}]\n")

    if total:
        print(f"--- Accuracy ---")
        print(f"Queue:    {correct_queue}/{total} ({100*correct_queue/total:.0f}%)")
        print(f"Priority: {correct_priority}/{total} ({100*correct_priority/total:.0f}%)")


if __name__ == "__main__":
    main()
