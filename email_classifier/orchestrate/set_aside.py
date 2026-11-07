"""Conversations stage 2 keeps failing on are set aside, not retried every poll (STU-2109).

A message whose classify call times out gets no verdict, so the next poll escalates it
again: three attempts of 180 s on a model that serves one request at a time, every hour,
without end. The reports already record each poll's outcome per message, so the streak
is read from them rather than kept in a second store.
"""

import json
from pathlib import Path

# A message that failed once, or twice, still gets its retry.
MAX_FAILED_POLLS = 3

SET_ASIDE_ERROR = "set_aside_after_repeated_failures"


def failed_polls(report_dir: Path, message_ids: set[str]) -> dict[str, int]:
    """Consecutive most-recent reports in which each id's stage-2 verdict errored.

    A report that holds no stage-2 verdict for an id (not fetched, or decided by a rule)
    neither extends nor breaks its streak; an `ok` verdict ends it. A message already set
    aside is recorded as an error again, so its streak keeps growing until a report
    shelf prune forgets it.
    """
    streak = dict.fromkeys(message_ids, 0)
    open_ids = set(message_ids)
    for path in sorted(report_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not open_ids:
            break
        try:
            verdicts = json.loads(path.read_text(encoding="utf-8")).get("verdicts", [])
        except (OSError, ValueError):
            continue
        for v in verdicts:
            mid = v.get("message_id")
            if mid not in open_ids or v.get("path") != "llm":
                continue
            if v.get("status") == "error":
                streak[mid] += 1
            else:
                open_ids.discard(mid)
    return streak


def is_set_aside(message_ids: tuple[str, ...], streaks: dict[str, int]) -> bool:
    """A conversation is set aside when every message it escalates keeps failing: a new
    message in the thread has no streak, so it brings the conversation back."""
    return all(streaks.get(m, 0) >= MAX_FAILED_POLLS for m in message_ids)
