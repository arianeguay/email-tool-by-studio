"""Drafts journal (STU-850): one JSONL line per draft the mailbox actually received.

Written by the code that called Graph, never by the agent claiming success —
that line is what `drafts-post` counts and what `undo` deletes (STU-854).

The file is shared by every run, and each line names the run that wrote it
(STU-917). Scoping it by message id alone attributes an earlier run's drafts to
this one — two runs over the same mailbox share most of their `needs_reply` set,
so an audit reading message ids reports a stage that drafted nothing as nearly
covered. The run id reaches here through the fan-out item, which is the resume
key, so an item now differs between runs; that costs close to nothing, because a
mail already carrying a draft never reaches the fan-out at all (STU-945) and a
same-run replay keeps the same run id and still hits.

`logs/drafts/stages.jsonl` is the second file, and answers what the per-draft
lines cannot: what happened to a stage that wrote none of them. `drafts-pre`
opens a run there with `started` before anything can fail, so a stage killed from
outside still leaves a trace; `failed` and `completed` close it.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

STARTED = "started"
FAILED = "failed"
COMPLETED = "completed"


def _append(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()


def record(
    journal: Path,
    run_id: str | None,
    message_id: str,
    thread_id: str,
    status: str,
    draft_id: str | None = None,
    updated: bool | None = None,
    error: str | None = None,
    prompt_fingerprint: str | None = None,
    sent_at: str | None = None,
) -> None:
    """`run_id` is None only for `redraft`, which answers to a human, not a run.

    `prompt_fingerprint` is what the draft was written under (STU-886), and only
    `write-draft` has it: the next run compares it to today's to decide whether the
    draft it is holding still says what this repo would say. None everywhere else,
    because every other writer reports on a draft it did not compose — and None on a
    row older than the field, which `conversation_verdict` reads as "not today's".

    `sent_at` is the mailbox's send time, carried only by a `sent` row (STU-1549).
    """
    _append(journal, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "message_id": message_id,
        "thread_id": thread_id,
        "draft_id": draft_id,
        "updated": updated,
        "status": status,
        "error": error,
        "prompt_fingerprint": prompt_fingerprint,
        "sent_at": sent_at,
    })


def record_stage(
    log: Path,
    run_id: str,
    status: str,
    needs_reply: int,
    drafted: int = 0,
    error: str | None = None,
    tokens: dict | None = None,
    selection: dict | None = None,
) -> None:
    """The drafts stage's own outcome, including when it produced nothing (STU-917).

    A tolerated failure still has to be a recorded one: `drafts` is the single
    `mailbox-full` stage allowed to die (STU-850), and until this line existed a
    dead stage and a mailbox with nothing to answer left the same artifacts —
    none.

    `tokens` is what the drafting fan-out spent, and this line is where it lives
    (STU-1012). Not in the report: the report is the classification run's artifact,
    and a stage that spends most of a `mailbox-full` run's bill answers for it on
    its own record. Only `completed` carries it — `drafts-post` is the only writer
    that has seen the fan-out's results, and the `failed` line is written by
    `drafts-pre`, which dies before any item is spawned.

    `selection` is what `drafts-pre` decided (STU-1186), carried through its output to
    the same terminal line: how many of the `needs_reply` conversations it handed to
    the fan-out, and how many it held back under which reason. Without it a stage that
    correctly drafted nothing and one that lost a reply share a shape, and the answer
    lived only in the Studio journal, which is pruned at 30 days.

    None is the honest value everywhere it is not known, never `{}` (STU-912): a
    `started` line is written before anything is decided, a `failed` one by a stage
    that died deciding, and a line older than this field decided nothing it could
    say. A reader that cannot find the breakdown must say so rather than report zero
    held — which is the direction that reads as "nothing was owed".
    """
    _append(log, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "stage": "drafts",
        "status": status,
        "needs_reply": needs_reply,
        "drafted": drafted,
        "error": error,
        "tokens": tokens,
        "selection": selection,
    })
