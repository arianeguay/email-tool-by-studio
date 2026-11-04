"""Apply executor (STU-406): report verdicts -> mailbox operations, policy-gated.

The unit is the conversation, not the message (STU-940): `orchestrate/conversation.py`
reconciles a thread's per-message verdicts into one, and every message of the thread
then gets that one's operations. Categories are per-message either way, but a folder
move is not — a thread applied message by message ends up split between the Inbox and
the Archive, and the second apply moves a message out from under the first.

Mapping: keep_visible / needs_reply -> no mailbox change; label_archive ->
ensure "email-tool/<bucket>" label, add it, remove INBOX; mark_for_delete -> trash,
but only when the policy allows it — otherwise downgraded to archive here,
even if the report was produced under a more permissive policy.

Gating: only status=="ok" verdicts with a concrete action are applicable.
When deterministic_can_finalize is false, only high-confidence deterministic
verdicts finalize; everything else is left untouched for review. A conversation
finalizes only when all of its messages do — a half-finalized thread is that
same split, arrived at through the policy instead of through disagreement.

Dry-run is the default (STU-407): the plan is journaled, no mutation call is
issued. Real mutations require dry_run=False, i.e. an explicit `--apply`.

Idempotence is a state diff, not a marker test (STU-963). Every applied message
still gets the "email-tool/processed" marker, added AFTER the operation — a failure
before it means retry next run, the reverse would silently drop the message —
but what decides whether to act is the `email-tool/<bucket>` the message already carries
against the one the verdict now asks for. Equal means converged, and a marked
message with nothing left to do is skipped exactly as before. Different means a
later run corrects an earlier one's work, in both directions: `unarchive` puts a
message back in the Inbox when a reply reopens a thread that was archived.

That rests on the bucket label meaning "we moved it", so the label is written
last: out of the Inbox first, then labelled; back into the Inbox first, then
unlabelled. A run that dies between the two leaves a message with no label, which
the next run reads as unfinished and completes. The other order would leave a
label without the move, which the next run would read as converged and strand in
the Inbox forever.

That is also what leaves a human's own move standing. Only this app writes
`email-tool/<bucket>`, so dragging an archived message back to the Inbox does not remove
it; the verdict still matches, the message reads as converged, and nothing
touches it. A verdict that genuinely changed does override the move, message and
folder both — which is the case where acting is the point.

The trash is the third location on that same axis (STU-983), not an exception to
it: `label_ids` reports `TRASH` the way it reports `INBOX`, so a conversation that
a reply reconciles back to A or B untrashes the half an earlier run threw out.

Which half is ours needs its own record, and gets one — `email-tool/deleted`, written
before the move. `email-tool/<bucket>` says where we filed a message; nothing said we had
thrown one out, and no location label can say who did. Nor can it be inferred from
the absence of a bucket: an applied keep_visible leaves a message marked and
unbucketed too, so that test would read every message a human deletes after we
have read it as our own, and resurrect it. The label is read in both directions —
in the trash it means ours to bring back, out of the trash it means somebody put
it back and we do not throw it out again — and each provider's
`conversation_message_ids` re-admits only messages that carry it.

The axis has a third answer, and not acting is the whole of it (STU-1075). A
message in none of the three places this app puts mail — not the Inbox, not the
Archive it files to, not the trash — is one the mailbox is holding somewhere we
never put it: the Deletions dumpster a hard delete leaves it in, or a folder a
human filed it to. `label_ids` reports that as `ELSEWHERE` and the message is
skipped, marker included. Without it a report outliving its mail read as a live
message that merely already sat outside the Inbox, so `make apply RUN_ID=<older
run>` — a rollback, a retry — planned an archive with no move, stamped
`email-tool/<bucket>` onto mail in the dumpster, and journalled every operation `ok`.

Fetching is what makes any of this reachable: the conversation is the unit within
one report, so a report has to hold the whole thread. `fetch/threads.py` widens
every report-feeding fetch to do that.

Each message is isolated: an exception is recorded as an "error" outcome, in
the journal too, and the batch continues.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from email_classifier.apply.journal import ApplyJournal, Operation
from email_classifier.core.contract import Taxonomy
from email_classifier.orchestrate import conversation
from email_classifier.orchestrate.conversation import ThreadVerdict
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.providers import (
    bucket_label,
    DELETED_LABEL,
    ELSEWHERE_LABEL as _ELSEWHERE,
    INBOX_LABEL as _INBOX,
    PROCESSED_LABEL,
    TRASH_LABEL as _TRASH,
    Writer,
)
from email_classifier.stage1.policy import Policy

_APPLICABLE_ACTIONS = frozenset(
    {"keep_visible", "needs_reply", "label_archive", "mark_for_delete"}
)


@dataclass
class ApplyOutcome:
    message_id: str
    # "applied" | "planned" | "skipped_processed" | "skipped_policy"
    # | "skipped_conversation" | "skipped_human_trash" | "skipped_elsewhere" | "error"
    status: str
    # "none" | "archive" | "unarchive" | "trash" | "untrash" when applied or planned
    operation: str | None
    error: str | None = None


def plan_operations(
    decision: ThreadVerdict,
    policy: Policy,
    applied_bucket: str | None = None,
    in_inbox: bool = True,
    in_trash: bool = False,
    applied_deleted: bool = False,
    filed_elsewhere: bool = False,
    expired: bool = False,
) -> tuple[str, list[Operation]]:
    """One message's operations under its conversation's verdict. Pure — no I/O.

    `applied_bucket` is the `email-tool/<bucket>` the message already carries, None when it
    carries none; `in_inbox` and `in_trash` are where it currently sits. Only this
    app writes those labels, so one is the record of what we last did — and the
    operation order below is what keeps that record honest at every point a run can
    die: whatever it leaves behind has to read as unfinished to the next run, never
    as converged and never as somebody else's doing. Archiving therefore moves first
    and labels last, and trashing labels first and moves last, for the same reason
    from opposite ends. The plan is the difference between that record and what the
    verdict now asks for, which is what lets a later run converge a thread an earlier
    one already acted on (STU-963) — including back into the Inbox, the direction no
    operation used to exist for.

    Reading the label rather than a stored history is also what keeps a human's own
    move: dragging an archived message back to the Inbox leaves `email-tool/C` on it, so an
    unchanged verdict reads as converged and nothing touches it. Only a verdict that
    actually changed overrides a human — and then it moves the message too, which is
    what `in_inbox` is read for.

    The trash is the third location, and `applied_deleted` — the `email-tool/deleted` label
    — is its half of the record (STU-983). It has to be written rather than
    inferred: a trashed message may carry no bucket, but so does every message
    applied as keep_visible, so "marked and unbucketed" cannot tell our own trash
    from mail a human threw out after we had read it.

    Both readings of that record refuse to act. In the trash without it, the
    deletion is somebody else's and the message is not ours to bring back. Out of
    the trash with it, somebody put the message back — or a run died before the
    move — and this app does not throw it out a second time. That is the same
    "a human's move stands" rule the bucket label gives the Archive, and it costs
    the same thing: a trash that dies is not retried.

    `filed_elsewhere` is the location axis's third answer (STU-1075): the message
    is in none of the three folders this app writes to. Nothing in that state is
    ours to move — the message is either gone from the mailbox as live mail, or
    filed by a human, and neither is undone by an archive.

    `expired` is the conversation's rule expiry having passed (STU-2070): it turns
    the verdict into an archive, past any clamp, since the rule's author asked for
    exactly that. Only a message still in the Inbox, or one we already filed: out of
    the Inbox with no bucket of ours, a human moved it, and that move stands.
    """
    action = decision.suggested_action
    # `apply_verdicts` turns this away earlier so the message is not stamped either;
    # the plan says it too, for the reason somebody else's trash does below.
    if filed_elsewhere:
        return "none", []
    if expired and action != "mark_for_delete" and (in_inbox or applied_bucket):
        action = "label_archive"
    if action == "mark_for_delete" and action not in policy.destructive_actions_allowed:
        action = "label_archive"

    # Bucket off before any move: it says where we filed the message, and a message
    # on its way out of the Archive is no longer filed anywhere.
    unlabel = [Operation("remove_label", bucket_label(applied_bucket))] if applied_bucket else []
    our_trash = in_trash and applied_deleted

    if action == "mark_for_delete":
        if in_trash or applied_deleted:
            return "none", []
        return "trash", [*unlabel, Operation("add_label", DELETED_LABEL), Operation("trash")]

    # Somebody else's deletion. `apply_verdicts` turns this away earlier so the
    # message is not stamped either; the plan says it too, because this function is
    # where the rule is read from and a caller of it must not have to know.
    if in_trash and not our_trash:
        return "none", []

    # Where `untrash` lands is provider-specific — Gmail restores the labels the
    # message had before the trash, Outlook always moves to the Inbox — so a plan
    # coming out of the trash states its destination rather than inheriting one.
    # `email-tool/deleted` comes off after the move, so a crash leaves the message out of
    # the trash still carrying it: protected rather than silently re-deletable.
    restore = (
        [Operation("untrash"), Operation("remove_label", DELETED_LABEL)]
        if our_trash else []
    )
    leaves_inbox = in_inbox or our_trash

    if action == "label_archive":
        if applied_bucket == decision.bucket and not our_trash:
            return "none", []
        return "archive", [
            *unlabel,
            *restore,
            *([Operation("remove_label", _INBOX)] if leaves_inbox else []),
            Operation("add_label", bucket_label(decision.bucket)),
        ]
    if applied_bucket is None and not our_trash:
        return "none", []
    return ("untrash" if our_trash else "unarchive"), [
        *restore,
        *([] if in_inbox else [Operation("add_label", _INBOX)]),
        *unlabel,
    ]


def apply_verdicts(
    verdicts: list[EmailVerdict],
    writer: Writer,
    policy: Policy,
    journal: ApplyJournal,
    taxonomy: Taxonomy,
    dry_run: bool = True,
    now: datetime | None = None,
) -> list[ApplyOutcome]:
    now = now or datetime.now(timezone.utc)
    label_ids = {_INBOX: _INBOX}
    marker_id = (
        writer.find_label(PROCESSED_LABEL) if dry_run else writer.ensure_label(PROCESSED_LABEL)
    )
    if marker_id is not None:
        label_ids[PROCESSED_LABEL] = marker_id

    def label_id(name: str) -> str:
        if name not in label_ids:
            label_ids[name] = writer.ensure_label(name)
        return label_ids[name]

    # Bucket label ids, resolved once through `find_label`: reading what a message
    # already carries must never create a label the mailbox does not have.
    bucket_of = {
        found: bucket
        for bucket in taxonomy.buckets
        if (found := writer.find_label(bucket_label(bucket))) is not None
    }
    deleted_id = writer.find_label(DELETED_LABEL)

    def finalizable(v: EmailVerdict) -> bool:
        if v.status != "ok" or v.suggested_action not in _APPLICABLE_ACTIONS:
            return False
        if policy.deterministic_can_finalize:
            return True
        return v.path == "deterministic" and v.confidence == "high"

    def execute(operation: Operation, message_id: str) -> None:
        if operation.kind == "trash":
            writer.trash(message_id)
        elif operation.kind == "untrash":
            writer.untrash(message_id)
        elif operation.kind == "add_label":
            writer.add_label(message_id, label_id(operation.label))
        else:
            writer.remove_label(message_id, label_id(operation.label))

    outcomes: list[ApplyOutcome] = []
    for messages in conversation.group(verdicts).values():
        try:
            decision = conversation.reconcile(messages, taxonomy)
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            for v in messages:
                journal.record(v.message_id, v.rule_id, Operation("plan"), "error", detail)
            outcomes.extend(ApplyOutcome(v.message_id, "error", None, detail) for v in messages)
            continue
        if decision is None or not all(finalizable(v) for v in messages):
            outcomes.extend(
                ApplyOutcome(
                    v.message_id,
                    "skipped_policy" if not finalizable(v) else "skipped_conversation",
                    None,
                )
                for v in messages
            )
            continue
        expired = bool(decision.expires_at) and datetime.fromisoformat(decision.expires_at) <= now
        for v in messages:
            current = Operation("plan")
            try:
                carried = writer.label_ids(v.message_id)
                processed = marker_id is not None and marker_id in carried
                applied_bucket = next(
                    (bucket_of[label] for label in carried if label in bucket_of), None
                )
                applied_deleted = deleted_id is not None and deleted_id in carried
                # Held somewhere this app never put it — the dumpster of a hard
                # delete, or a human's own folder. A report can outlive its mail
                # by an ordinary route (`make apply RUN_ID=<an older run>`), and
                # what that mail needs is to be left alone, marker included.
                if _ELSEWHERE in carried:
                    outcomes.append(ApplyOutcome(v.message_id, "skipped_elsewhere", None))
                    continue
                # A message in the trash carrying no record of our deleting it was
                # thrown out by a human — since the fetch admits no other kind, one
                # who did it between that fetch and now. Not ours to move, and not
                # ours to stamp: the marker would be litter in their trash.
                if _TRASH in carried and not applied_deleted:
                    outcomes.append(
                        ApplyOutcome(v.message_id, "skipped_human_trash", None)
                    )
                    continue
                name, operations = plan_operations(
                    decision, policy, applied_bucket, _INBOX in carried,
                    _TRASH in carried, applied_deleted, _ELSEWHERE in carried, expired,
                )
                # A converged message is done. The trash is no longer an exception:
                # `label_ids` reports it, so a trashed message diffs like any other.
                #
                # Trashing one included: `email-tool/deleted` records that, so a message
                # somebody put back reads as converged rather than as one to throw
                # out again.
                if processed and not operations:
                    outcomes.append(ApplyOutcome(v.message_id, "skipped_processed", None))
                    continue
                # Not re-stamped when it is already there: the mailbox would not
                # change, but the journal line would, and undoing this run would
                # then strip a marker an earlier run put on.
                marker = [] if processed else [Operation("add_label", PROCESSED_LABEL)]
                for current in [*operations, *marker]:
                    if not dry_run:
                        execute(current, v.message_id)
                    journal.record(
                        v.message_id, decision.rule_id, current,
                        "planned" if dry_run else "ok",
                    )
                outcomes.append(
                    ApplyOutcome(v.message_id, "planned" if dry_run else "applied", name)
                )
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
                journal.record(v.message_id, decision.rule_id, current, "error", detail)
                outcomes.append(ApplyOutcome(v.message_id, "error", None, detail))
    return outcomes


def expired_threads(
    reports: list[dict], skip_threads: set[str], now: datetime
) -> list[EmailVerdict]:
    """Every message of each earlier conversation whose rule expiry has passed (STU-2070).

    A run's fetch reaches back to its floor and no further, so a code that expires
    tomorrow is in no report tomorrow's run writes. Its expiry lives in the report
    that decided it; each conversation is taken from the report that holds its most
    recent message, and `skip_threads` (the current report's) wins over all of them.
    """
    def latest(messages: list[EmailVerdict]) -> EmailVerdict:
        return max(messages, key=lambda m: (m.date, m.message_id))

    newest: dict[str, list[EmailVerdict]] = {}
    for report in reports:
        verdicts = [EmailVerdict(**v) for v in report["verdicts"]]
        for thread_id, messages in conversation.group(verdicts).items():
            held = newest.get(thread_id)
            if thread_id not in skip_threads and (
                held is None or latest(messages).date > latest(held).date
            ):
                newest[thread_id] = messages
    return [
        v
        for messages in newest.values()
        if (expiry := latest(messages).expires_at) and datetime.fromisoformat(expiry) <= now
        for v in messages
    ]


def render_summary(outcomes: list[ApplyOutcome], dry_run: bool) -> str:
    def count(*, status: str, operation: str | None = None) -> int:
        return sum(
            1
            for o in outcomes
            if o.status == status and (operation is None or o.operation == operation)
        )

    acted = "planned" if dry_run else "applied"
    header = "=== Apply plan (dry-run, no mutation) ===" if dry_run else "=== Apply run ==="
    return "\n".join(
        [
            header,
            f"{len(outcomes)} verdicts | {count(status=acted)} {acted} | "
            f"{count(status='error')} errors",
            f"  archived: {count(status=acted, operation='archive')}  "
            f"unarchived: {count(status=acted, operation='unarchive')}  "
            f"trashed: {count(status=acted, operation='trash')}  "
            f"untrashed: {count(status=acted, operation='untrash')}  "
            f"keep: {count(status=acted, operation='none')}",
            f"  skipped (policy): {count(status='skipped_policy')}  "
            f"skipped (conversation): {count(status='skipped_conversation')}  "
            f"skipped (already processed): {count(status='skipped_processed')}",
            f"  skipped (deleted by a human): {count(status='skipped_human_trash')}  "
            f"skipped (no longer live mail): {count(status='skipped_elsewhere')}",
        ]
    )
