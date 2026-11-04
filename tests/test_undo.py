"""Tests for undo (STU-407): the apply->undo round-trip restores the mailbox.

Plus the drafts half of the same round-trip (STU-854): a run's reply drafts are
deleted with its labels, unless the mailbox says a human got there first.
"""

import json
from datetime import datetime, timedelta

import pytest

from email_classifier.apply.executor import apply_verdicts
from email_classifier.apply.journal import ApplyJournal, read_entries
from email_classifier.apply.undo import (
    drafts_of_run,
    render_drafts_summary,
    render_summary,
    undo_drafts,
    undo_entries,
)
from tests.conftest import FakeWriter, inboxed
from tests.test_executor import TAXONOMY, make_policy, verdict


@pytest.fixture
def undo_journal(tmp_path):
    return ApplyJournal(tmp_path / "applied" / "run.undo.jsonl", "run")


def test_round_trip_restores_labels_and_trash(journal, undo_journal):
    writer = inboxed(FakeWriter(), "m1", "m2", "m3")
    verdicts = [
        verdict("m1", bucket="B"),
        verdict("m2", bucket="D", action="mark_for_delete"),
        verdict("m3", action="keep_visible"),
    ]
    apply_verdicts(verdicts, writer, make_policy(), journal, TAXONOMY, dry_run=False)

    outcomes = undo_entries(read_entries(journal.path), writer, undo_journal)

    assert {o.status for o in outcomes} == {"undone"}
    assert writer.trashed == []
    assert writer.labels_of("m1") == {"INBOX"}
    assert writer.labels_of("m2") == {"INBOX"}
    assert writer.labels_of("m3") == {"INBOX"}


def test_undone_run_can_be_applied_again(journal, undo_journal, tmp_path):
    writer = inboxed(FakeWriter(), "m1")
    verdicts = [verdict("m1", bucket="B")]
    apply_verdicts(verdicts, writer, make_policy(), journal, TAXONOMY, dry_run=False)
    undo_entries(read_entries(journal.path), writer, undo_journal)

    second = ApplyJournal(tmp_path / "applied" / "second.jsonl", "second")
    outcomes = apply_verdicts(verdicts, writer, make_policy(), second, TAXONOMY, dry_run=False)

    assert [o.status for o in outcomes] == ["applied"]


def test_undo_ignores_planned_and_error_entries(journal, undo_journal):
    """One journal with all three statuses: m1 left the Inbox and then failed to be
    labelled, m2 was only ever planned. Just the one "ok" line is inverted."""
    writer = inboxed(FakeWriter(fail_on={"m1"}), "m1", "m2")
    apply_verdicts([verdict("m1", bucket="B")], writer, make_policy(), journal, TAXONOMY, dry_run=False)
    apply_verdicts([verdict("m2", bucket="B")], writer, make_policy(), journal, TAXONOMY)
    writer.fail_on.clear()

    outcomes = undo_entries(read_entries(journal.path), writer, undo_journal)

    assert [(o.message_id, o.operation, o.status) for o in outcomes] == [
        ("m1", "add_label", "undone")
    ]
    assert writer.labels_of("m1") == {"INBOX"}
    assert writer.labels_of("m2") == {"INBOX"}


def test_undo_is_journaled_and_summarized(journal, undo_journal):
    writer = inboxed(FakeWriter(), "m1")
    apply_verdicts([verdict("m1", bucket="B")], writer, make_policy(), journal, TAXONOMY, dry_run=False)

    outcomes = undo_entries(read_entries(journal.path), writer, undo_journal)

    assert [(e["op"], e["label"], e["status"]) for e in read_entries(undo_journal.path)] == [
        ("remove_label", "email-tool/processed", "ok"),
        ("remove_label", "email-tool/B", "ok"),
        ("add_label", "INBOX", "ok"),
    ]
    assert "3 operations reverted | 0 errors | 1 messages touched" in render_summary(
        outcomes, "run"
    )


def test_undo_error_is_isolated_and_journaled(journal, undo_journal):
    writer = inboxed(FakeWriter(), "m1")
    apply_verdicts([verdict("m1", bucket="B")], writer, make_policy(), journal, TAXONOMY, dry_run=False)
    writer.fail_on.add("m1")  # re-adding INBOX now fails

    outcomes = undo_entries(read_entries(journal.path), writer, undo_journal)

    assert [(o.operation, o.status) for o in outcomes] == [
        ("remove_label", "undone"),
        ("remove_label", "undone"),
        ("add_label", "error"),
    ]
    assert writer.labels_of("m1") == set()


# ── the drafts a run created (STU-854) ───────────────────────────────────────

DRAFTED_AT = "2026-08-20T10:00:00+00:00"


class FakeDrafter:
    """Drafter interface, in memory. Its drafts are what the mailbox would report."""

    def __init__(self, drafts, fail_on=()):
        self.drafts = dict(drafts)
        self.fail_on = set(fail_on)
        self.deleted = []

    def get_draft(self, draft_id):
        if draft_id in self.fail_on:
            raise RuntimeError("boom")
        return self.drafts.get(draft_id)

    def delete_draft(self, draft_id):
        self.deleted.append(draft_id)
        del self.drafts[draft_id]


def untouched(draft_id, last_modified="2026-08-20T09:59:58Z"):
    return {draft_id: {"id": draft_id, "isDraft": True,
                       "lastModifiedDateTime": last_modified}}


def drafts_journal(tmp_path, *entries):
    """A drafts journal holding `(message_id, draft_id, status)` lines, in order.

    Stamped at a fixed `DRAFTED_AT` rather than through `record`: whether a draft
    reads as edited is a comparison against that stamp, and a wall clock would
    decide these tests.
    """
    path = tmp_path / "drafts" / "journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps({"timestamp": DRAFTED_AT, "message_id": message_id,
                    "thread_id": f"t-{message_id}", "draft_id": draft_id,
                    "status": status, "error": None}) + "\n"
        for message_id, draft_id, status in entries
    ), encoding="utf-8")
    return path


def candidates(path, *message_ids):
    return drafts_of_run(read_entries(path), set(message_ids))


def test_only_the_runs_own_needs_reply_drafts_are_candidates(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"), ("m2", "d2", "ok"))

    assert list(candidates(path, "m1")) == ["d1"]


def test_a_draft_the_tool_failed_to_create_is_never_a_candidate(tmp_path):
    path = drafts_journal(tmp_path, ("m1", None, "error"))

    assert candidates(path, "m1") == {}


def test_an_untouched_draft_is_deleted_and_journaled(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"))
    drafter = FakeDrafter(untouched("d1"))

    outcomes = undo_drafts(candidates(path, "m1"), drafter, path, "run-1")

    assert [(o.message_id, o.status) for o in outcomes] == [("m1", "deleted")]
    assert drafter.deleted == ["d1"]
    assert read_entries(path)[-1]["status"] == "deleted"


def test_undoing_twice_does_not_delete_the_draft_twice(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"))
    undo_drafts(candidates(path, "m1"), FakeDrafter(untouched("d1")), path, "run-1")

    assert candidates(path, "m1") == {}


def test_a_draft_edited_since_the_run_is_kept_and_named(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"))
    drafter = FakeDrafter(untouched("d1", last_modified="2026-08-20T11:30:00Z"))

    outcomes = undo_drafts(candidates(path, "m1"), drafter, path, "run-1")

    assert [o.status for o in outcomes] == ["kept"]
    assert drafter.deleted == []
    assert "KEPT m1 draft d1: edited at 2026-08-20T11:30:00Z" in render_drafts_summary(
        outcomes
    )


def test_a_write_lagging_the_journal_stamp_is_still_the_runs_own_draft(tmp_path):
    """The journal line is stamped after Graph answered, so a server clock running a
    minute ahead must not read as a human edit."""
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"))
    ahead = (datetime.fromisoformat(DRAFTED_AT) + timedelta(seconds=60)).isoformat()

    outcomes = undo_drafts(
        candidates(path, "m1"), FakeDrafter(untouched("d1", last_modified=ahead)),
        path, "run-1"
    )

    assert [o.status for o in outcomes] == ["deleted"]


def test_a_draft_the_human_sent_is_never_deleted(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"))
    sent = {"d1": {"id": "d1", "isDraft": False,
                   "lastModifiedDateTime": "2026-08-20T09:59:58Z"}}
    drafter = FakeDrafter(sent)

    outcomes = undo_drafts(candidates(path, "m1"), drafter, path, "run-1")

    assert [(o.status, o.detail) for o in outcomes] == [("kept", "sent since the run")]
    assert drafter.deleted == []


def test_a_draft_already_gone_is_not_an_error(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"))

    outcomes = undo_drafts(candidates(path, "m1"), FakeDrafter({}), path, "run-1")

    assert [o.status for o in outcomes] == ["gone"]


def test_a_draft_error_is_isolated_and_reported(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"), ("m2", "d2", "ok"))
    drafter = FakeDrafter({**untouched("d1"), **untouched("d2")}, fail_on={"d1"})

    outcomes = undo_drafts(candidates(path, "m1", "m2"), drafter, path, "run-1")

    assert sorted(o.status for o in outcomes) == ["deleted", "error"]
    assert drafter.deleted == ["d2"]
    assert "ERROR m1 draft d1: RuntimeError: boom" in render_drafts_summary(outcomes)


def test_the_drafts_summary_counts_every_draft(tmp_path):
    path = drafts_journal(tmp_path, ("m1", "d1", "ok"))

    outcomes = undo_drafts(candidates(path, "m1"), FakeDrafter(untouched("d1")), path, "run-1")

    assert "drafts: 1 deleted | 0 kept | 0 already gone | 0 errors" in (
        render_drafts_summary(outcomes)
    )
