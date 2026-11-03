"""Undo an apply run from its journal (STU-407).

Operations are inverted in reverse order, so a message that was labelled then
un-inboxed is re-inboxed then un-labelled. Only status "ok" entries are
inverted: a "planned" line never touched the mailbox, and an "error" line is
the operation that did not happen.

Reversibility rests on never using permanent deletion: `trash` is undone with
`untrash` (Gmail keeps trashed mail 30 days), the `untrash` a reopened thread
issues (STU-983) is undone with `trash`, and every label removal is recorded by
name so the label can be put back.

The drafts a run created are reverted too (STU-854), and there deletion *is* the
inverse: the draft did not exist before the run. Never a draft a human has since
touched — see `undo_drafts`.
"""

from dataclasses import dataclass
from pathlib import Path

from email_classifier.apply.draft_journal import record
from email_classifier.apply.journal import ApplyJournal, Operation
from email_classifier.orchestrate.drafts import edited_since
from email_classifier.providers import Drafter, Writer

_INBOX = "INBOX"
_INVERSE = {
    "add_label": "remove_label",
    "remove_label": "add_label",
    "trash": "untrash",
    "untrash": "trash",
}


@dataclass
class UndoOutcome:
    message_id: str
    operation: str         # the inverse operation attempted
    status: str            # "undone" | "error"
    error: str | None = None


def undo_entries(
    entries: list[dict], writer: Writer, journal: ApplyJournal
) -> list[UndoOutcome]:
    label_ids = {_INBOX: _INBOX}

    def label_id(name: str) -> str:
        if name not in label_ids:
            label_ids[name] = writer.ensure_label(name)
        return label_ids[name]

    outcomes: list[UndoOutcome] = []
    for entry in reversed(entries):
        if entry["status"] != "ok" or entry["op"] not in _INVERSE:
            continue
        inverse = Operation(_INVERSE[entry["op"]], entry["label"])
        message_id = entry["message_id"]
        try:
            if inverse.kind == "untrash":
                writer.untrash(message_id)
            elif inverse.kind == "trash":
                writer.trash(message_id)
            elif inverse.kind == "add_label":
                writer.add_label(message_id, label_id(inverse.label))
            else:
                writer.remove_label(message_id, label_id(inverse.label))
            journal.record(message_id, entry["rule_id"], inverse, "ok")
            outcomes.append(UndoOutcome(message_id, inverse.kind, "undone"))
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            journal.record(message_id, entry["rule_id"], inverse, "error", detail)
            outcomes.append(UndoOutcome(message_id, inverse.kind, "error", detail))
    return outcomes


@dataclass
class DraftUndoOutcome:
    message_id: str
    draft_id: str
    status: str            # "deleted" | "kept" | "gone" | "error"
    detail: str | None = None


def drafts_of_run(entries: list[dict], message_ids: set[str]) -> dict[str, dict]:
    """The drafts still standing that a run put in the mailbox, by draft id.

    Scoped by the run's own report — the message ids it marked `needs_reply` are
    exactly the ones it could have drafted — and deliberately not by the line's
    `run_id` (STU-917), which an audit reads but undo must not: an earlier run's
    draft on a conversation this run also marked `needs_reply` is still a draft
    this run would otherwise have made, and leaving it standing is the silent
    survivor STU-854 exists to prevent.

    The last line for a draft id wins, so a draft this function already reported
    deleted is not offered a second time.
    """
    latest: dict[str, dict] = {}
    for entry in entries:
        if entry.get("draft_id") and entry["message_id"] in message_ids:
            latest[entry["draft_id"]] = entry
    return {draft_id: e for draft_id, e in latest.items() if e["status"] == "ok"}


def undo_drafts(
    candidates: dict[str, dict], drafter: Drafter, journal: Path, run_id: str
) -> list[DraftUndoOutcome]:
    """Delete each draft, unless the mailbox says it is no longer the run's own.

    Three ways it stops being the run's: it was sent, a human edited it, or it is
    already gone. The first two are a human's work and are left alone — the whole
    point of asking the mailbox before deleting. Only the deletions are journaled;
    a draft left standing changed nothing.
    """
    outcomes: list[DraftUndoOutcome] = []
    for draft_id, entry in candidates.items():
        try:
            draft = drafter.get_draft(draft_id)
            if draft is None:
                status, detail = "gone", "already deleted"
            elif not draft["isDraft"]:
                status, detail = "kept", "sent since the run"
            elif edited_since(draft["lastModifiedDateTime"], entry["timestamp"]):
                status, detail = "kept", f"edited at {draft['lastModifiedDateTime']}"
            else:
                drafter.delete_draft(draft_id)
                status, detail = "deleted", None
                record(journal, run_id, entry["message_id"], entry["thread_id"],
                       status, draft_id)
        except Exception as exc:
            status, detail = "error", f"{type(exc).__name__}: {exc}"
        outcomes.append(DraftUndoOutcome(entry["message_id"], draft_id, status, detail))
    return outcomes


def render_summary(outcomes: list[UndoOutcome], run_id: str) -> str:
    undone = [o for o in outcomes if o.status == "undone"]
    errors = [o for o in outcomes if o.status == "error"]
    lines = [
        f"=== Undo (run {run_id}) ===",
        f"{len(undone)} operations reverted | {len(errors)} errors "
        f"| {len({o.message_id for o in undone})} messages touched",
    ]
    lines.extend(f"  ERROR {o.message_id} {o.operation}: {o.error}" for o in errors)
    return "\n".join(lines)


def render_drafts_summary(outcomes: list[DraftUndoOutcome]) -> str:
    """Every draft not deleted is named — a silent survivor is the STU-854 bug."""
    def count(status: str) -> int:
        return sum(1 for o in outcomes if o.status == status)

    lines = [
        f"  drafts: {count('deleted')} deleted | {count('kept')} kept "
        f"| {count('gone')} already gone | {count('error')} errors"
    ]
    lines.extend(
        f"    {o.status.upper()} {o.message_id} draft {o.draft_id}: {o.detail}"
        for o in outcomes
        if o.status in ("kept", "error")
    )
    return "\n".join(lines)
