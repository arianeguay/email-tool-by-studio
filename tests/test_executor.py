"""Tests for the apply executor (STU-406).

The writer is faked at the GmailWriter interface (see tests.conftest): it keeps
per-message label state so idempotence can be exercised by running the same
verdicts twice against the same fake mailbox. These tests drive the real-apply
path (dry_run=False); the dry-run default is covered in test_apply_journal.py.
"""

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from email_classifier.apply.executor import (
    PROCESSED_LABEL,
    apply_verdicts,
    expired_threads,
    render_summary,
)
from email_classifier.core.contract import load_taxonomy
from email_classifier.providers import DELETED_LABEL
from email_classifier.apply.journal import ApplyJournal, read_entries
from email_classifier.apply.undo import undo_entries
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.stage1.policy import Policy
from tests.conftest import FakeWriter, inboxed
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")


def verdict(message_id="m1", path="deterministic", status="ok", bucket="B",
            action="label_archive", confidence="high", error=None, thread_id=None,
            date="2026-08-18T12:00:00+00:00", rule_id="r1", expires_at=""):
    """One message in a conversation of its own, unless a `thread_id` is shared.

    Apply reconciles per conversation (STU-940), so a shared thread id here would make
    every verdict in the list a party to every other one's outcome — which is what
    test_conversation.py is for.
    """
    return EmailVerdict(
        message_id=message_id, thread_id=thread_id or f"t_{message_id}", path=path,
        status=status, bucket=bucket, suggested_action=action, confidence=confidence,
        rule_id=rule_id, subtype=None, reasoning=None, error=error, date=date,
        expires_at=expires_at,
    )


def make_policy(finalize=True, destructive=("mark_for_delete",)):
    return Policy(
        deterministic_can_finalize=finalize,
        destructive_actions_allowed=list(destructive),
        frontier_log_file=False,
        frontier_log_stdout=False,
    )


def apply(verdicts, writer, policy, journal):
    return apply_verdicts(verdicts, writer, policy, journal, TAXONOMY, dry_run=False)


@pytest.mark.parametrize("action", ["keep_visible", "needs_reply"])
def test_keep_visible_and_needs_reply_are_noops_but_marked(action, journal):
    writer = inboxed(FakeWriter(), "m1")

    outcomes = apply([verdict(bucket="A", action=action)], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "none")]
    assert writer.labels_of("m1") == {"INBOX", PROCESSED_LABEL}
    assert writer.trashed == []


def test_label_archive_adds_bucket_label_and_removes_inbox(journal):
    writer = inboxed(FakeWriter(), "m1")

    outcomes = apply([verdict(bucket="B")], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "archive")]
    assert writer.labels_of("m1") == {"ec/B", PROCESSED_LABEL}


def test_mark_for_delete_trashes_when_allowed(journal):
    writer = inboxed(FakeWriter(), "m1")

    outcomes = apply(
        [verdict(bucket="D", action="mark_for_delete")], writer, make_policy(), journal
    )

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "trash")]
    assert writer.trashed == ["m1"]
    assert PROCESSED_LABEL in writer.labels_of("m1")


def test_mark_for_delete_downgrades_to_archive_when_not_allowed(journal):
    writer = inboxed(FakeWriter(), "m1")

    outcomes = apply(
        [verdict(bucket="D", action="mark_for_delete")],
        writer,
        make_policy(destructive=()),
        journal,
    )

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "archive")]
    assert writer.trashed == []
    assert writer.labels_of("m1") == {"ec/D", PROCESSED_LABEL}


def test_rerun_skips_already_processed_messages(journal):
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        verdict("m1", bucket="B"),
        verdict("m2", bucket="D", action="mark_for_delete"),
    ]
    policy = make_policy()

    first = apply(verdicts, writer, policy, journal)
    second = apply(verdicts, writer, policy, journal)

    assert [o.status for o in first] == ["applied", "applied"]
    assert [o.status for o in second] == ["skipped_processed", "skipped_processed"]
    assert writer.trashed == ["m2"]


def test_gating_when_deterministic_cannot_finalize(journal):
    writer = inboxed(FakeWriter(), "m1", "m2", "m3")
    verdicts = [
        verdict("m1", confidence="high"),
        verdict("m2", confidence="medium"),
        verdict("m3", path="llm", confidence="high"),
    ]

    outcomes = apply(verdicts, writer, make_policy(finalize=False), journal)

    assert [o.status for o in outcomes] == ["applied", "skipped_policy", "skipped_policy"]
    assert writer.labels_of("m2") == {"INBOX"}
    assert writer.labels_of("m3") == {"INBOX"}


def test_error_and_escalate_verdicts_are_skipped(journal):
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        verdict("m1", status="error", bucket=None, action=None, confidence=None,
                error="llm_timeout"),
        verdict("m2", action="escalate_to_llm"),
    ]

    outcomes = apply(verdicts, writer, make_policy(), journal)

    assert [o.status for o in outcomes] == ["skipped_policy", "skipped_policy"]
    assert writer.labels_of("m1") == {"INBOX"}
    assert writer.labels_of("m2") == {"INBOX"}


def test_per_message_error_is_isolated_and_leaves_no_marker(journal):
    writer = inboxed(FakeWriter(fail_on={"m1"}), "m1", "m2")
    verdicts = [
        verdict("m1", bucket="D", action="mark_for_delete"),
        verdict("m2", bucket="B"),
    ]

    outcomes = apply(verdicts, writer, make_policy(), journal)

    assert [(o.status, o.error) for o in outcomes] == [
        ("error", "RuntimeError: boom"),
        ("applied", None),
    ]
    assert writer.labels_of("m1") == {"INBOX"}
    assert writer.labels_of("m2") == {"ec/B", PROCESSED_LABEL}


def test_label_archive_without_bucket_is_a_per_message_error(journal):
    writer = inboxed(FakeWriter(), "m1")

    outcomes = apply([verdict(bucket=None)], writer, make_policy(), journal)

    assert outcomes[0].status == "error"
    assert "is not one of A, B, C, D" in outcomes[0].error
    assert writer.labels_of("m1") == {"INBOX"}


def test_label_ids_are_resolved_once_per_name(journal):
    writer = inboxed(FakeWriter(), "m1", "m2", "m3")
    verdicts = [
        verdict("m1", bucket="B"),
        verdict("m2", bucket="B"),
        verdict("m3", bucket="C"),
    ]

    apply(verdicts, writer, make_policy(), journal)

    assert sorted(writer.ensure_calls) == ["ec/B", "ec/C", PROCESSED_LABEL]


def test_summary_counts_each_outcome(journal):
    writer = inboxed(FakeWriter(fail_on={"m4"}), "m1", "m2", "m3", "m4")
    verdicts = [
        verdict("m1", bucket="B"),
        verdict("m2", bucket="D", action="mark_for_delete"),
        verdict("m3", action="escalate_to_llm"),
        verdict("m4", bucket="B"),
    ]

    summary = render_summary(apply(verdicts, writer, make_policy(), journal), dry_run=False)

    assert "=== Apply run ===" in summary
    assert "4 verdicts | 2 applied | 1 errors" in summary
    assert "archived: 1  unarchived: 0  trashed: 1  untrashed: 0  keep: 0" in summary
    assert "skipped (policy): 1" in summary


def thread(message_id, date, bucket, action, **kwargs):
    return verdict(
        message_id, bucket=bucket, action=action, thread_id="conv", date=date, **kwargs
    )


def test_a_conversation_moves_as_one_even_when_its_verdicts_disagree(journal):
    """`infolettre_reponse_parent`: a newsletter opens the thread, a parent asks a real
    question in it. Applied message by message, the opener archives and the question
    stays in the inbox — one conversation in two places."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive"),
        thread("m2", "2026-08-19T09:00:00+00:00", "A", "needs_reply"),
    ]

    outcomes = apply(verdicts, writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "none")] * 2
    assert writer.labels_of("m1") == writer.labels_of("m2") == {"INBOX", PROCESSED_LABEL}


def test_a_conversation_archives_under_one_bucket_label(journal):
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        thread("m1", "2026-08-18T09:00:00+00:00", "B", "label_archive"),
        thread("m2", "2026-08-19T09:00:00+00:00", "C", "label_archive"),
    ]

    apply(verdicts, writer, make_policy(), journal)

    assert writer.labels_of("m1") == writer.labels_of("m2") == {"ec/B", PROCESSED_LABEL}


def test_one_unfinalizable_message_holds_its_whole_conversation(journal):
    """The split the policy can produce on its own: finalizing the deterministic half of
    a thread leaves the escalated half behind in the inbox."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive"),
        thread("m2", "2026-08-19T09:00:00+00:00", "C", "label_archive",
               path="llm", confidence="low"),
    ]

    outcomes = apply(verdicts, writer, make_policy(finalize=False), journal)

    assert [o.status for o in outcomes] == ["skipped_conversation", "skipped_policy"]
    assert writer.labels_of("m1") == writer.labels_of("m2") == {"INBOX"}


def test_the_journal_cites_the_message_whose_action_the_conversation_took(journal):
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive", rule_id="opened"),
        thread("m2", "2026-08-19T09:00:00+00:00", "C", "mark_for_delete", rule_id="closed"),
    ]

    apply(verdicts, writer, make_policy(), journal)

    entries = read_entries(journal.path)
    assert {e["message_id"] for e in entries} == {"m1", "m2"}
    assert {e["rule_id"] for e in entries} == {"closed"}


def test_a_conversation_nothing_new_arrived_on_is_left_alone(journal):
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        thread("m1", "2026-08-18T09:00:00+00:00", "B", "label_archive"),
        thread("m2", "2026-08-19T09:00:00+00:00", "B", "label_archive"),
    ]

    apply(verdicts, writer, make_policy(), journal)
    outcomes = apply(verdicts, writer, make_policy(), journal)

    assert [o.status for o in outcomes] == ["skipped_processed", "skipped_processed"]


def test_a_conversation_apply_undoes_exactly(journal, tmp_path):
    """The issue's second half: one coherent outcome is only worth having if the journal
    puts every message of the conversation back."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    before = {m: set(writer.labels_of(m)) for m in ("m1", "m2")}
    verdicts = [
        thread("m1", "2026-08-18T09:00:00+00:00", "B", "label_archive"),
        thread("m2", "2026-08-19T09:00:00+00:00", "D", "mark_for_delete"),
    ]

    apply(verdicts, writer, make_policy(), journal)
    undo_entries(
        read_entries(journal.path), writer, ApplyJournal(tmp_path / "undo.jsonl", "run")
    )

    assert {m: set(writer.labels_of(m)) for m in ("m1", "m2")} == before
    assert writer.trashed == []


# --- Convergence across runs (STU-963) -----------------------------------------
#
# The fetch now hands a later run the whole thread, so these are two runs over the
# same conversation: the first sees the opener alone, the second sees both messages.
# What the second one may do to a message the first already acted on is decided by
# the `ec/<bucket>` that message carries, never by the marker alone.


def test_a_reply_pulls_its_archived_opener_back_into_the_inbox(journal):
    """The issue's case, reversed: a parent replies to a newsletter the last run
    archived. The thread is now A, and the opener has to come back with it."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive")

    apply([opener], writer, make_policy(), journal)
    assert writer.labels_of("m1") == {"ec/C", PROCESSED_LABEL}

    outcomes = apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "A", "needs_reply")],
        writer, make_policy(), journal,
    )

    assert [(o.status, o.operation) for o in outcomes] == [
        ("applied", "unarchive"),
        ("applied", "none"),
    ]
    assert writer.labels_of("m1") == writer.labels_of("m2") == {"INBOX", PROCESSED_LABEL}


def test_a_reply_archives_the_opener_the_last_run_left_visible(journal):
    """The same split the other way up: the last run left the opener in the inbox, and
    the reply that closes the thread is what the whole conversation archives on."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "B", "keep_visible")

    apply([opener], writer, make_policy(), journal)
    assert writer.labels_of("m1") == {"INBOX", PROCESSED_LABEL}

    outcomes = apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "B", "label_archive")],
        writer, make_policy(), journal,
    )

    assert [o.operation for o in outcomes] == ["archive", "archive"]
    assert writer.labels_of("m1") == writer.labels_of("m2") == {"ec/B", PROCESSED_LABEL}


def test_a_rising_bucket_relabels_without_moving_the_message_twice(journal):
    """Precedence raised C to B across runs. Both messages end under ec/B, and the
    already-archived one is relabelled in place — not archived a second time."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive")

    apply([opener], writer, make_policy(), journal)
    journal.path.unlink()
    apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "B", "label_archive")],
        writer, make_policy(), journal,
    )

    assert writer.labels_of("m1") == writer.labels_of("m2") == {"ec/B", PROCESSED_LABEL}
    # No second `remove_label INBOX`, and no re-stamped marker: only the difference.
    assert [(e["op"], e["label"]) for e in read_entries(journal.path)][:2] == [
        ("remove_label", "ec/C"),
        ("add_label", "ec/B"),
    ]


def test_an_archive_that_died_before_labelling_is_finished_next_run(journal):
    """The bucket label is written last, so a run that dies mid-archive leaves a
    message with no label — unfinished, and completed — rather than a label without
    the move, which the next run would read as converged and strand forever."""
    writer = inboxed(FakeWriter(fail_on=["m1"]), "m1")
    v = verdict("m1", bucket="B")

    assert [o.status for o in apply([v], writer, make_policy(), journal)] == ["error"]
    assert writer.labels_of("m1") == set()  # out of the Inbox, unlabelled, unmarked

    writer.fail_on.clear()
    outcomes = apply([v], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "archive")]
    assert writer.labels_of("m1") == {"ec/B", PROCESSED_LABEL}


def test_a_message_a_human_moved_back_is_left_where_they_put_it(journal):
    """Dragging an archived message to the inbox does not remove its ec/<bucket>, so
    an unchanged verdict still reads as converged. Only a changed one overrides."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive")

    apply([opener], writer, make_policy(), journal)
    writer.message_labels["m1"].add("INBOX")

    outcomes = apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "C", "label_archive")],
        writer, make_policy(), journal,
    )

    assert [o.status for o in outcomes] == ["skipped_processed", "applied"]
    assert "INBOX" in writer.labels_of("m1")


def test_a_changed_verdict_moves_the_message_a_human_moved_back(journal):
    """The other half of the same rule: an unchanged verdict leaves the move alone,
    a changed one re-files the message as well as relabelling it."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive")

    apply([opener], writer, make_policy(), journal)
    writer.message_labels["m1"].add("INBOX")

    apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "B", "label_archive")],
        writer, make_policy(), journal,
    )

    assert writer.labels_of("m1") == {"ec/B", PROCESSED_LABEL}


def test_a_second_run_undoes_only_what_it_changed(journal, tmp_path):
    """The issue's second half across runs: the converging run's journal puts the
    thread back where the first run left it, not where the mailbox started."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive")

    apply([opener], writer, make_policy(), journal)
    after_first = {m: set(writer.labels_of(m)) for m in ("m1", "m2")}

    second = ApplyJournal(tmp_path / "second.jsonl", "run2")
    apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "A", "needs_reply")],
        writer, make_policy(), second,
    )
    undo_entries(
        read_entries(second.path), writer, ApplyJournal(tmp_path / "undo.jsonl", "run2")
    )

    assert {m: set(writer.labels_of(m)) for m in ("m1", "m2")} == after_first


# --- Convergence through the trash (STU-983) -----------------------------------
#
# The trash is the third location on the same axis as the Inbox and the Archive:
# `label_ids` reports it, so a trashed message diffs like any other. What it does
# not have is a bucket label, which is why `mark_for_delete` strips one before
# moving — the marker with no bucket is what says the trash was ours.


def test_a_reply_untrashes_the_opener_the_last_run_threw_out(journal):
    """The issue's case: a run trashed the opener, then a parent replied in the thread.
    The conversation is now A, so both messages have to end up in the Inbox."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "D", "mark_for_delete")

    apply([opener], writer, make_policy(), journal)
    assert writer.labels_of("m1") == {"TRASH", PROCESSED_LABEL, DELETED_LABEL}

    outcomes = apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "A", "needs_reply")],
        writer, make_policy(), journal,
    )

    assert [(o.status, o.operation) for o in outcomes] == [
        ("applied", "untrash"),
        ("applied", "none"),
    ]
    assert writer.labels_of("m1") == writer.labels_of("m2") == {"INBOX", PROCESSED_LABEL}


def test_a_reply_that_archives_the_thread_untrashes_the_opener_into_the_archive(journal):
    """The same reopening, landing on B rather than A: out of the trash, then filed —
    never left in Deleted Items wearing an ec/<bucket>."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "D", "mark_for_delete")

    apply([opener], writer, make_policy(), journal)
    outcomes = apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "B", "label_archive")],
        writer, make_policy(), journal,
    )

    assert [o.operation for o in outcomes] == ["archive", "archive"]
    assert writer.labels_of("m1") == writer.labels_of("m2") == {"ec/B", PROCESSED_LABEL}


def test_a_second_run_undoes_exactly_the_untrash_it_issued(journal, tmp_path):
    """The issue's second half: the reopening run's journal puts the thread back where
    the first run left it — the opener in the trash, not in the mailbox's start state."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "D", "mark_for_delete")

    apply([opener], writer, make_policy(), journal)
    after_first = {m: set(writer.labels_of(m)) for m in ("m1", "m2")}

    second = ApplyJournal(tmp_path / "second.jsonl", "run2")
    apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "A", "needs_reply")],
        writer, make_policy(), second,
    )
    undo_entries(
        read_entries(second.path), writer, ApplyJournal(tmp_path / "undo.jsonl", "undo")
    )

    assert {m: set(writer.labels_of(m)) for m in ("m1", "m2")} == after_first
    assert writer.trashed == ["m1"]


def test_a_still_deleted_thread_leaves_the_trashed_message_where_it_is(journal):
    """Converged is converged in the trash too: nothing to diff means nothing to do,
    and no second trash call on a message already there."""
    writer = inboxed(FakeWriter(), "m1")
    v = verdict("m1", bucket="D", action="mark_for_delete")

    apply([v], writer, make_policy(), journal)
    outcomes = apply([v], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("skipped_processed", None)]
    assert writer.labels_of("m1") == {"TRASH", PROCESSED_LABEL, DELETED_LABEL}


def test_trashing_an_archived_message_strips_its_bucket_label(journal):
    """What makes our own trash recognizable: this app never leaves an ec/<bucket> on a
    message in Deleted Items, so one that carries a bucket got there some other way."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive")

    apply([opener], writer, make_policy(), journal)
    apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "D", "mark_for_delete")],
        writer, make_policy(), journal,
    )

    assert writer.labels_of("m1") == {"TRASH", PROCESSED_LABEL, DELETED_LABEL}
    assert writer.labels_of("m2") == {"TRASH", PROCESSED_LABEL, DELETED_LABEL}


def test_a_trash_that_died_before_moving_is_finished_next_run(journal):
    """The bucket label comes off before the move, so a run that dies mid-trash leaves
    an unlabelled message in the Archive — unfinished, and completed next run."""
    writer = inboxed(FakeWriter(), "m1")
    archive = verdict("m1", bucket="C")
    delete = verdict("m1", bucket="D", action="mark_for_delete")

    apply([archive], writer, make_policy(), journal)
    writer.fail_on.add("m1")
    assert [o.status for o in apply([delete], writer, make_policy(), journal)] == ["error"]
    assert writer.labels_of("m1") == {PROCESSED_LABEL}  # out of the Archive's bucket

    writer.fail_on.clear()
    outcomes = apply([delete], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "trash")]
    assert writer.labels_of("m1") == {"TRASH", PROCESSED_LABEL, DELETED_LABEL}


def test_untrashing_an_archived_message_states_the_inbox_explicitly(journal):
    """Where `untrash` lands is provider-specific: Gmail restores the labels the
    message held before the trash, so one trashed out of the Archive comes back
    archived. The plan says where it goes rather than inheriting a destination."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "C", "label_archive")
    binned = thread("m1", "2026-08-18T09:00:00+00:00", "D", "mark_for_delete")

    apply([opener], writer, make_policy(), journal)
    apply([binned], writer, make_policy(), journal)
    assert writer.labels_of("m1") == {"TRASH", PROCESSED_LABEL, DELETED_LABEL}

    apply(
        [binned, thread("m2", "2026-08-19T09:00:00+00:00", "A", "needs_reply")],
        writer, make_policy(), journal,
    )

    assert writer.labels_of("m1") == writer.labels_of("m2") == {"INBOX", PROCESSED_LABEL}


def test_a_message_a_human_pulled_out_of_the_trash_is_not_thrown_out_again(journal):
    """`ec/deleted` is read out of the trash as well as in it: the message carries our
    record of a deletion and is demonstrably not deleted, so somebody put it back. The
    label stays — it is what keeps the next run, and every run after, from repeating
    a destructive action against their move."""
    writer = inboxed(FakeWriter(), "m1")
    v = verdict("m1", bucket="D", action="mark_for_delete")

    apply([v], writer, make_policy(), journal)
    writer.untrash("m1")  # the human, from the mailbox

    outcomes = apply([v], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("skipped_processed", None)]
    assert writer.labels_of("m1") == {"INBOX", PROCESSED_LABEL, DELETED_LABEL}
    assert writer.trashed == []


def test_a_trash_that_died_on_the_move_is_not_retried(journal):
    """`ec/deleted` goes on before the move, so a run that dies on it leaves a message
    that reads exactly like one a human pulled back out — and is treated as one. The
    cost of that ambiguity is this: the trash is not retried. It is the cheap half,
    since the message stays in the Inbox where it can be seen."""
    writer = inboxed(FakeWriter(trash_fails_on={"m1"}), "m1")
    v = verdict("m1", bucket="D", action="mark_for_delete")

    assert [o.status for o in apply([v], writer, make_policy(), journal)] == ["error"]
    assert writer.labels_of("m1") == {"INBOX", DELETED_LABEL}

    writer.trash_fails_on.clear()
    outcomes = apply([v], writer, make_policy(), journal)

    assert [o.operation for o in outcomes] == ["none"]
    assert writer.trashed == []
    assert writer.labels_of("m1") == {"INBOX", DELETED_LABEL, PROCESSED_LABEL}


def test_a_message_a_human_deleted_after_we_read_it_is_not_resurrected(journal):
    """The state that defeats inference: an applied keep_visible leaves the message
    marked, in the Inbox, with no bucket — exactly what a trash of ours leaves once
    you look for it in the absence of a bucket. A human deleting it afterwards must
    not read as our own deletion, whatever the conversation later reconciles to."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "A", "keep_visible")

    apply([opener], writer, make_policy(), journal)
    writer.trash("m1")  # the human, from the mailbox

    outcomes = apply(
        [opener, thread("m2", "2026-08-19T09:00:00+00:00", "A", "needs_reply")],
        writer, make_policy(), journal,
    )

    assert [o.status for o in outcomes] == ["skipped_human_trash", "applied"]
    assert writer.trashed == ["m1"]


def test_a_message_deleted_between_the_fetch_and_the_apply_is_left_alone(journal):
    """`in_trash` is re-read live at apply time, and a mailbox-full run spends minutes
    in stage 2 between the two. A message thrown out in that window carries no record
    of ours at all — it is not stamped, not moved, and not counted as applied."""
    writer = inboxed(FakeWriter(), "m1")
    writer.trash("m1")  # the human, after the fetch that classified it

    outcomes = apply([verdict("m1", bucket="B")], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("skipped_human_trash", None)]
    assert writer.labels_of("m1") == {"TRASH"}
    assert not journal.path.exists()  # nothing happened, so there is nothing to undo


def test_a_human_restoring_an_archived_message_we_deleted_is_not_overruled(journal):
    """Outlook's own Restore puts a message back where it came from, so a human undoing
    our deletion of an archived message lands it in the Archive, not the Inbox. The
    record travels with the message, so where they put it back does not matter."""
    writer = inboxed(FakeWriter(), "m1")
    archive = verdict("m1", bucket="C")
    delete = verdict("m1", bucket="D", action="mark_for_delete")

    apply([archive], writer, make_policy(), journal)
    apply([delete], writer, make_policy(), journal)
    writer.untrash("m1")  # the human; Gmail and Graph both return it to the Archive
    assert "INBOX" not in writer.labels_of("m1")

    outcomes = apply([delete], writer, make_policy(), journal)

    assert [(o.status, o.operation) for o in outcomes] == [("skipped_processed", None)]
    assert writer.trashed == []


# --- Rule expiry (STU-2070) ----------------------------------------------------
#
# A rule's `expires_after` lands on the verdict as `expires_at`. Once that instant
# passes, the conversation archives, whatever its action was and whatever its bucket
# allows, unless a human already took it out of the Inbox.

NOW = datetime(2026, 8, 18, 15, 0, tzinfo=timezone.utc)
PAST = "2026-08-18T13:00:00+00:00"
FUTURE = "2026-08-18T17:00:00+00:00"


def code(expires_at=PAST, **kwargs):
    return verdict("m1", bucket="A", action="keep_visible", rule_id="code",
                   expires_at=expires_at, **kwargs)


def apply_at(verdicts, writer, journal, dry_run=False):
    return apply_verdicts(
        verdicts, writer, make_policy(), journal, TAXONOMY, dry_run=dry_run, now=NOW
    )


def test_an_expired_inbox_message_is_planned_for_archive(journal):
    writer = inboxed(FakeWriter(), "m1")

    outcomes = apply_at([code()], writer, journal, dry_run=True)

    assert [(o.status, o.operation) for o in outcomes] == [("planned", "archive")]
    assert [(e["op"], e.get("label")) for e in read_entries(journal.path)] == [
        ("remove_label", "INBOX"), ("add_label", "ec/A"), ("add_label", PROCESSED_LABEL),
    ]
    assert writer.labels_of("m1") == {"INBOX"}


def test_an_unexpired_inbox_message_stays_visible(journal):
    writer = inboxed(FakeWriter(), "m1")

    outcomes = apply_at([code(expires_at=FUTURE)], writer, journal)

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "none")]
    assert writer.labels_of("m1") == {"INBOX", PROCESSED_LABEL}


def test_an_expiry_archives_a_message_an_earlier_run_kept_visible(journal):
    writer = inboxed(FakeWriter(), "m1")
    apply_at([code(expires_at=FUTURE)], writer, journal)

    outcomes = apply_at([code()], writer, journal)

    assert [(o.status, o.operation) for o in outcomes] == [("applied", "archive")]
    assert writer.labels_of("m1") == {"ec/A", PROCESSED_LABEL}


def test_an_expired_message_already_archived_is_left_alone(journal):
    writer = inboxed(FakeWriter(), "m1")
    apply_at([code()], writer, journal)

    outcomes = apply_at([code()], writer, journal)

    assert [o.status for o in outcomes] == ["skipped_processed"]
    assert writer.labels_of("m1") == {"ec/A", PROCESSED_LABEL}


def test_a_message_a_human_moved_out_of_the_inbox_is_not_touched(journal):
    writer = inboxed(FakeWriter(), "m1")
    apply_at([code(expires_at=FUTURE)], writer, journal)
    writer.message_labels["m1"].discard("INBOX")  # the human archives it themselves

    outcomes = apply_at([code()], writer, journal)

    assert [o.status for o in outcomes] == ["skipped_processed"]
    assert writer.labels_of("m1") == {PROCESSED_LABEL}


def test_the_expiry_of_the_latest_message_decides_the_conversation(journal):
    """A reply after the code is a claim about now that carries no expiry."""
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [
        thread("m1", "2026-08-18T09:00:00+00:00", "A", "keep_visible", expires_at=PAST),
        thread("m2", "2026-08-18T10:00:00+00:00", "A", "keep_visible"),
    ]

    outcomes = apply_at(verdicts, writer, journal)

    assert [o.operation for o in outcomes] == ["none", "none"]
    assert "INBOX" in writer.labels_of("m1")


def test_undo_restores_an_expiry_archive(journal, tmp_path):
    writer = inboxed(FakeWriter(), "m1")
    apply_at([code(expires_at=FUTURE)], writer, journal)
    before = set(writer.labels_of("m1"))
    expiry = ApplyJournal(tmp_path / "applied" / "later.jsonl", "later")

    apply_at([code()], writer, expiry)
    undo_entries(
        read_entries(expiry.path), writer, ApplyJournal(tmp_path / "undo.jsonl", "later")
    )

    assert writer.labels_of("m1") == before


def report(*verdicts):
    return {"verdicts": [asdict(v) for v in verdicts]}


def test_expired_threads_reads_back_an_earlier_reports_expired_conversation():
    expired = code()
    kept = verdict("m2", bucket="A", action="keep_visible", expires_at=FUTURE)
    plain = verdict("m3", bucket="A", action="keep_visible")

    assert expired_threads([report(expired, kept, plain)], set(), NOW) == [expired]


def test_expired_threads_leaves_the_current_reports_conversations_to_it():
    assert expired_threads([report(code())], {"t_m1"}, NOW) == []


def test_expired_threads_takes_a_conversation_from_its_newest_report():
    """A reply an earlier run classified with the code supersedes the code's expiry."""
    opener = thread("m1", "2026-08-18T09:00:00+00:00", "A", "keep_visible", expires_at=PAST)
    reply = thread("m2", "2026-08-18T10:00:00+00:00", "A", "keep_visible")

    assert expired_threads([report(opener, reply), report(opener)], set(), NOW) == []
    assert expired_threads([report(opener), report(opener, reply)], set(), NOW) == []
