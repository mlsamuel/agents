"""
email_stream.py - Simulates an incoming email stream from the dataset.

Usage:
    from email_stream import email_stream

    for email in email_stream(limit=10):
        print(email["subject"])
"""

import pandas as pd
from typing import Iterator

DATA_PATH = "data/emails.csv"


def email_stream(
    path: str = DATA_PATH,
    language: str | None = None,   # filter to "en" or "de" or None for all
    limit: int | None = None,
    shuffle: bool = False,
    random_seed: int = 42,
) -> Iterator[dict]:
    """
    Yields emails one at a time as dicts, simulating an incoming mail stream.

    Each email dict has keys:
        subject, body, answer, type, queue, priority, language
    """
    df = pd.read_csv(path)

    if language:
        df = df[df["language"] == language]

    if shuffle:
        df = df.sample(frac=1, random_state=random_seed).reset_index(drop=True)

    if limit:
        df = df.head(limit)

    for _, row in df.iterrows():
        yield row.where(pd.notna(row), other=None).to_dict()
