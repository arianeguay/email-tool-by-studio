#!/usr/bin/env python3
"""Post-stage of `drafts` (STU-850): count what the mailbox actually received.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

Counts drafts from the tool's journal, never from the agents' outputs: an item
whose QA loop ended in rejection, or whose child run died, contributes nothing
here however confidently it reported success. A resumed item is counted too —
its draft was created by an earlier attempt at this run and is still in the
mailbox, and a replay keeps the run id, so the line is still ours.

Only ours: the journal is shared, so the count is scoped by `run_id` (STU-1003).
Another run's draft on a mail this one also has pending is that run's, and this
number is written straight into `logs/drafts/stages.jsonl` for an audit to read.

A missing fan-out output — condition-skipped because nothing needed a reply, or
a tolerated failure — is not an error: zero drafts is a legitimate outcome of a
mailbox with no pending questions. Exits 0 either way.

Which is why reaching here is itself worth recording (STU-917): this closes the
run's line in `logs/drafts/stages.jsonl`, so "zero drafts, stage ran" stops being
indistinguishable from "zero drafts, stage never got here".

An item that failed leaves no draft behind (STU-1547). `write-draft` runs before
`draft-qa` judges, so a group that ends rejected after its last iteration has
already put an unapproved body in the mailbox, beside the drafts QA approved: on
runs `4ed439fb` and `22a0b39c` those were the most sensitive questions of the
selection period. The draft this run wrote on that conversation is deleted and
journaled `deleted`, unless a human has edited it since, and the conversation is
counted held as `rejected`. One `write-draft` left alone for a human (STU-1548)
is counted held as `blocked`, the verdict `drafts-pre` gives the same draft.

Output: { "run_id", "pending", "drafted", "failed", "journal_path" }
"""

import json
import sys
from pathlib import Path

from email_classifier import cost
from email_classifier.apply.draft_journal import COMPLETED, record, record_stage
from email_classifier.apply.journal import read_entries
from email_classifier.orchestrate import batch, drafts
from email_classifier.providers import get_drafter
from email_classifier.stage1.policy import load_policy

REJECTED = "rejected"
_FANOUT_STAGE = "draft-fanout"
_MAP_STAGE = "drafts"
_PRE_STAGE = "drafts-pre"
# Where Studio writes this execution's journal: beside the process's cwd.
RUNS_DIR = Path(".studio/runs")


def _fanout_from_journal(run_id: str) -> dict | None:
    """The map's own output, read off this execution's Studio journal (STU-1503).

    A `call:` that fails under `on_failure: continue` propagates no output, and a
    map whose every item failed fails the call: a lone conversation whose group
    ended rejected hands this stage nothing, while its item record, with every
    attempt and token, is in the journal. The newest log naming the run is this
    execution's, since Studio opens it before `drafts-pre` runs.
    """
    if not RUNS_DIR.is_dir():
        return None
    for log in sorted(RUNS_DIR.glob("*.jsonl"), reverse=True):
        text = log.read_text(errors="ignore")
        if run_id not in text:
            continue
        found = None
        for line in text.splitlines():
            if f'"stage":"{_MAP_STAGE}"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            output = event.get("output")
            if (event.get("event") == "stage_complete" and isinstance(output, dict)
                    and isinstance(output.get("results"), list)):
                found = output
        return found
    return None


def _clear_failed(pre: dict, results: dict, entries: list[dict], journal: Path) -> dict:
    """Delete this run's draft on each failed conversation; return why each is held.

    Keyed by message id, `blocked` when `write-draft` left a human's draft alone,
    `rejected` when the draft this run wrote is no longer in Drafts. A draft a human
    edited since stays, and so does its conversation's place in `selected`.
    """
    run_id = pre["run_id"]
    held: dict[str, str] = {}
    drafter = None
    for index, result in results.items():
        item = pre["pending"][index] if index < len(pre["pending"]) else None
        if result.get("status") == "success" or item is None:
            continue
        message_id = item["message_id"]
        rows = [e for e in entries
                if e.get("run_id") == run_id and e.get("message_id") == message_id]
        if any(e.get("status") == "held" for e in rows):
            held[message_id] = drafts.BLOCKED
            continue
        ours = next((e for e in reversed(rows) if e.get("status") == "ok"), None)
        if ours is None:
            continue
        if drafter is None:
            drafter = get_drafter(load_policy(Path(pre["paths"]["policy"])).email_provider)
        draft = drafter.get_draft(ours["draft_id"])
        if draft is not None:
            if drafts.edited_since(draft["lastModifiedDateTime"], ours["timestamp"]):
                continue
            drafter.delete_draft(ours["draft_id"])
            record(journal, run_id, message_id, ours["thread_id"], "deleted",
                   draft_id=ours["draft_id"])
        held[message_id] = REJECTED
    return held


def _with_held(selection: dict | None, held: dict[str, str]) -> dict | None:
    """`drafts-pre`'s selection with the conversations held after the fan-out moved
    from `selected` to `held`, so `selected` plus `held` stays `needs_reply`."""
    if selection is None or not held:
        return selection
    counts = dict(selection["held"])
    for reason in held.values():
        counts[reason] = counts.get(reason, 0) + 1
    return {**selection, "selected": selection["selected"] - len(held),
            "held": dict(sorted(counts.items()))}


def main() -> None:
    payload = json.load(sys.stdin)
    outputs = payload.get("previous_outputs") or {}
    pre = outputs[_PRE_STAGE]
    journal = Path(pre["paths"]["draft_journal"])

    pending = {item["message_id"] for item in pre["pending"]}
    entries = read_entries(journal) if journal.exists() else []
    fanout = outputs.get(_FANOUT_STAGE)
    if fanout is None and pending:
        fanout = _fanout_from_journal(pre["run_id"])
    results = batch.results_by_index(fanout)
    held = _clear_failed(pre, results, entries, journal)
    drafted = pending & drafts.drafted_message_ids(entries, pre["run_id"]) - set(held)
    failed = sum(1 for result in results.values() if result.get("status") != "success")

    # What the fan-out spent and how many calls bought it, both off the per-item
    # results already read above: each carries the child run's per-stage record, so
    # an attempt is counted where it was made rather than inferred from this run's
    # own journal rows (STU-1210).
    # A resumed item is dropped rather than counted unpriced: its draft was bought by
    # an earlier attempt, so it is not a call this run made.
    # Items were dispatched and nothing says what they did: one record-less entry each,
    # so the leg reads as an unrecorded floor and never as a run that cost nothing.
    items = (results.values() if fanout is not None
             else [{} for _ in pre["pending"]])
    spend = cost.leg((result for result in items if not result.get("cached")),
                     pre.get("provider"))
    # `drafts-pre`'s own decision, carried through rather than re-derived (STU-1186):
    # what it selected out of `needs_reply` and what it held back under which reason.
    # Absent from a `drafts-pre` output written before that field, and passed on absent
    # — a reader must read that as unknown, not as zero held.
    record_stage(Path(pre["paths"]["stage_log"]), pre["run_id"], COMPLETED,
                 pre["needs_reply"], len(drafted), tokens=spend,
                 selection=_with_held(pre.get("selection"), held))

    print(f"[drafts-post] {len(drafted)}/{len(pending)} drafted | {failed} failed",
          file=sys.stderr)
    json.dump({
        "run_id": pre["run_id"],
        "pending": len(pending),
        "drafted": len(drafted),
        "failed": failed,
        "journal_path": str(journal),
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
