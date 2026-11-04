"""One status category per conversation, on its latest inbound message (STU-2007).

With `auto_apply` off nothing in the Inbox says what the assistant did with a mail:
a draft in Drafts, or nothing, and nothing reads exactly like a failure. The status is
written under its own flag, `status_labels`, and moves nothing.

The status is read off the mailbox after drafting, never off what the drafting stage
claims: whether a draft waits, and whether the club replied since, are the same two
reads `drafts-pre` decides on (`conversation_verdict`), asked again once the fan-out is
done. A conversation owed a reply that still has neither is an error, whatever the
stage said about it.

These names are a set of their own, disjoint from `email-tool/<bucket>`: the executor resolves
only the taxonomy's buckets, so a status on a message never reads as a filing.

Outlook only. The names are accented, which an IMAP keyword cannot carry (the reason
STU-2049 does not mark IMAP drafts), and Gmail has no thread reader; the policy refuses
the flag on either rather than leaving it a silent no-op.
"""

from dataclasses import dataclass

from email_classifier.apply.journal import ApplyJournal, Operation
from email_classifier.core.contract import Taxonomy
from email_classifier.orchestrate import conversation, drafts
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.placeholder import PLACEHOLDER_CATEGORY
from email_classifier.providers import ELSEWHERE_LABEL, LABEL_PREFIX, TRASH_LABEL, Writer

DRAFTED = f"{LABEL_PREFIX}brouillon"
TO_COMPLETE = PLACEHOLDER_CATEGORY
NO_REPLY = f"{LABEL_PREFIX}pas-de-réponse"
ANSWERED = f"{LABEL_PREFIX}déjà-répondu"
YOURS = f"{LABEL_PREFIX}à-toi"
ERROR = f"{LABEL_PREFIX}erreur"

STATUS_LABELS = (DRAFTED, TO_COMPLETE, NO_REPLY, ANSWERED, YOURS, ERROR)


@dataclass(frozen=True)
class ReplyState:
    """What the mailbox says about a conversation owed a reply, after drafting."""
    hold: str | None          # a `content_hold` name: mail a human answers by hand
    draft: dict | None        # the conversation's draft as `get_draft` returns it
    last_sent: str | None     # the club's latest reply on the thread


def reply_status(date: str, state: ReplyState) -> str:
    if state.hold:
        return YOURS
    verdict = drafts.conversation_verdict(date, state.draft, state.last_sent, None, "")
    if verdict in (drafts.ANSWERED, drafts.SUPERSEDED):
        return ANSWERED
    if verdict == drafts.DRAFT:
        return ERROR
    return TO_COMPLETE if PLACEHOLDER_CATEGORY in (state.draft.get("categories") or []) \
        else DRAFTED


def _latest(messages: list[EmailVerdict]) -> EmailVerdict:
    return max(messages, key=lambda m: (m.date or "", m.message_id))


def derive(report: dict, taxonomy: Taxonomy, look) -> dict[str, str]:
    """`{message_id: status}` for the latest message of every conversation in the report.

    `look(verdict) -> ReplyState` is asked only for a conversation owed a reply. A
    reply the reconciliation dropped without anyone deciding nothing was owed
    (`held_replies`, a clamp) is the reviewer's, not a no-reply: a mail silently left
    unanswered is the failure the whole pipeline exists to avoid.
    """
    held = drafts.held_replies(report, taxonomy)
    verdicts = [EmailVerdict(**v) for v in report["verdicts"]]
    out: dict[str, str] = {}
    for messages in conversation.group(verdicts).values():
        try:
            decision = conversation.reconcile(messages, taxonomy)
        except ValueError:
            decision = None
        if decision is None:
            out[_latest(messages).message_id] = ERROR
            continue
        latest = next(m for m in messages if m.message_id == decision.latest_message_id)
        if decision.suggested_action == drafts.REPLY_ACTION:
            status = reply_status(latest.date, look(latest))
        elif any(m.message_id in held for m in messages):
            status = YOURS
        else:
            status = NO_REPLY
        out[latest.message_id] = status
    return out


def write(report: dict, statuses: dict[str, str], writer: Writer,
          journal: ApplyJournal) -> int:
    """Converge every message of each conversation on its status, one write at a time.

    A state diff like apply's (STU-963): only names in `STATUS_LABELS` are read or
    removed, so a category a human set is never touched, and the latest message's
    status replaces the one an earlier run put there. The other messages of the thread
    lose theirs, so a conversation carries exactly one. Removal before the add: a run
    that dies between leaves no status, which the next run completes. A message in the
    trash or outside the folders this app files to is left alone, as apply leaves it.

    Every write is journaled `ok` under the run, which is what `make undo` inverts.
    Returns the number of messages that failed; each failure is journaled.
    """
    status_of = {lid: name for name in STATUS_LABELS
                 if (lid := writer.find_label(name)) is not None}
    failed = 0
    for verdict in report["verdicts"]:
        message_id = verdict["message_id"]
        wanted = statuses.get(message_id)
        current = Operation("plan")
        try:
            carried = writer.label_ids(message_id)
            if ELSEWHERE_LABEL in carried or TRASH_LABEL in carried:
                continue
            present = {status_of[lid] for lid in carried if lid in status_of}
            for name in sorted(present - {wanted}):
                current = Operation("remove_label", name)
                writer.remove_label(message_id, writer.find_label(name))
                journal.record(message_id, None, current, "ok")
            if wanted and wanted not in present:
                current = Operation("add_label", wanted)
                writer.add_label(message_id, writer.ensure_label(wanted))
                journal.record(message_id, None, current, "ok")
        except Exception as exc:
            failed += 1
            journal.record(message_id, None, current, "error", f"{type(exc).__name__}: {exc}")
    return failed
