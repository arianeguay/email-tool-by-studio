"""Bucket-mixed selection over the fixture corpus (STU-845).

Pure: no mailbox, no network. Decides which synthetic emails a seed round sends,
so the seeded mailbox has a configurable bucket distribution instead of N copies
of one email.
"""

import random
from pathlib import Path

from email_classifier.core import schema
from email_classifier.core.types import EmailInput


def load_buckets(corpus: Path) -> dict[str, list[EmailInput]]:
    """Each fixture subdirectory -> its emails, keyed by directory name."""
    buckets = {
        directory.name: [schema.load(f) for f in sorted(directory.glob("*.json"))]
        for directory in sorted(p for p in corpus.iterdir() if p.is_dir())
    }
    buckets = {name: emails for name, emails in buckets.items() if emails}
    if not buckets:
        raise ValueError(f"no fixture bucket found under {corpus}")
    return buckets


def load_threads(corpus: Path) -> list[list[EmailInput]]:
    """Each conversation subdirectory -> its messages, in the order they arrived.

    A thread corpus is read whole and never mixed: the unit a round lays down is
    the conversation, so there is nothing to weight and nothing to draw from.
    The convention `fixtures-threads/README.md` states is checked here rather
    than assumed — a directory whose messages disagree on `thread_id`, or whose
    dates run backwards, would seed a chain that is not the fixture.
    """
    threads = []
    for directory in sorted(p for p in corpus.iterdir() if p.is_dir()):
        messages = [schema.load(f) for f in sorted(directory.glob("*.json"))]
        if len(messages) < 2:
            continue
        thread_ids = {message.thread_id for message in messages}
        if len(thread_ids) > 1:
            raise ValueError(f"{directory.name} spans {len(thread_ids)} thread ids")
        if [m.date for m in messages] != sorted(m.date for m in messages):
            raise ValueError(f"{directory.name} is not in date order")
        threads.append(messages)
    if not threads:
        raise ValueError(f"no conversation found under {corpus}")
    return threads


def parse_mix(spec: str) -> dict[str, float]:
    """`"bucket_a=40,bucket_c=60"` -> weights. An empty spec yields no mix."""
    weights: dict[str, float] = {}
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        name, separator, weight = part.partition("=")
        if not separator:
            raise ValueError(f"malformed bucket mix entry {part!r} — expected 'bucket=weight'")
        weights[name.strip()] = float(weight)
    return weights


def allocate(weights: dict[str, float], count: int) -> dict[str, int]:
    """Split `count` across the weights, largest remainder first so it sums exactly."""
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("bucket mix weights must sum above zero")
    exact = {name: count * weight / total for name, weight in weights.items()}
    allocated = {name: int(value) for name, value in exact.items()}
    by_remainder = sorted(exact, key=lambda name: exact[name] - allocated[name], reverse=True)
    for name in by_remainder[: count - sum(allocated.values())]:
        allocated[name] += 1
    return allocated


def select(
    buckets: dict[str, list[EmailInput]],
    count: int,
    mix: dict[str, float] | None = None,
    seed: int = 0,
) -> list[tuple[str, EmailInput]]:
    """Draw `count` (bucket, email) pairs, defaulting to the corpus's own proportions.

    A bucket is drawn without replacement until exhausted, then reshuffled, so an
    oversized count repeats fixtures as late and as evenly as possible.
    """
    weights = mix or {name: float(len(emails)) for name, emails in buckets.items()}
    unknown = sorted(set(weights) - set(buckets))
    if unknown:
        raise ValueError(f"unknown bucket(s) in the mix: {', '.join(unknown)}")

    rng = random.Random(seed)
    drawn: list[tuple[str, EmailInput]] = []
    for name, wanted in allocate(weights, count).items():
        pool = buckets[name]
        picks: list[EmailInput] = []
        while len(picks) < wanted:
            shuffled = pool[:]
            rng.shuffle(shuffled)
            picks += shuffled[: wanted - len(picks)]
        drawn += [(name, email) for email in picks]
    rng.shuffle(drawn)
    return drawn
