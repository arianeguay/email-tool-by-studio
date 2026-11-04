"""Tests for the dry-run default and the apply journal (STU-407)."""

from email_classifier.apply.executor import PROCESSED_LABEL, apply_verdicts, render_summary
from email_classifier.apply.journal import read_entries
from tests.conftest import FakeWriter, archived, inboxed
from tests.test_executor import TAXONOMY, make_policy, verdict


def test_dry_run_is_the_default_and_mutates_nothing(journal):
    writer = inboxed(FakeWriter(), "m1", "m2")
    verdicts = [verdict("m1", bucket="B"), verdict("m2", bucket="D", action="mark_for_delete")]

    outcomes = apply_verdicts(verdicts, writer, make_policy(), journal, TAXONOMY)

    assert [(o.status, o.operation) for o in outcomes] == [
        ("planned", "archive"),
        ("planned", "trash"),
    ]
    assert writer.trashed == []
    assert writer.ensure_calls == []
    assert writer.labels_of("m1") == {"INBOX"}
    assert writer.labels_of("m2") == {"INBOX"}


def test_dry_run_journals_the_plan_without_ok_entries(journal):
    writer = inboxed(FakeWriter(), "m1")

    apply_verdicts([verdict("m1", bucket="B")], writer, make_policy(), journal, TAXONOMY)

    entries = read_entries(journal.path)
    assert [(e["op"], e["label"], e["status"]) for e in entries] == [
        ("remove_label", "INBOX", "planned"),
        ("add_label", "email-tool/B", "planned"),
        ("add_label", PROCESSED_LABEL, "planned"),
    ]
    assert {e["run_id"] for e in entries} == {"run"}
    assert {e["rule_id"] for e in entries} == {"r1"}
    assert all(e["timestamp"] for e in entries)


def test_dry_run_skips_already_processed_messages(journal):
    writer = archived(FakeWriter(), "m1", "B")

    outcomes = apply_verdicts([verdict("m1", bucket="B")], writer, make_policy(), journal, TAXONOMY)

    assert [o.status for o in outcomes] == ["skipped_processed"]
    assert not journal.path.exists()


def test_dry_run_summary_reports_no_mutation(journal):
    writer = inboxed(FakeWriter(), "m1")

    summary = render_summary(
        apply_verdicts([verdict("m1", bucket="B")], writer, make_policy(), journal, TAXONOMY),
        dry_run=True,
    )

    assert "dry-run, no mutation" in summary
    assert "1 verdicts | 1 planned | 0 errors" in summary


def test_applied_operations_are_journaled_in_order(journal):
    writer = inboxed(FakeWriter(), "m1")

    apply_verdicts([verdict("m1", bucket="B")], writer, make_policy(), journal, TAXONOMY, dry_run=False)

    assert [(e["op"], e["label"], e["status"]) for e in read_entries(journal.path)] == [
        ("remove_label", "INBOX", "ok"),
        ("add_label", "email-tool/B", "ok"),
        ("add_label", PROCESSED_LABEL, "ok"),
    ]


def test_failed_operation_is_journaled_as_error(journal):
    # The trash move alone: the marker is written ahead of it (STU-983), so a
    # blanket failure would journal the marker instead of the operation in view.
    writer = inboxed(FakeWriter(trash_fails_on={"m1"}), "m1")

    apply_verdicts(
        [verdict("m1", bucket="D", action="mark_for_delete")],
        writer, make_policy(), journal, TAXONOMY, dry_run=False,
    )

    entries = read_entries(journal.path)
    assert [(e["op"], e["status"], e["error"]) for e in entries] == [
        ("add_label", "ok", None),
        ("trash", "error", "RuntimeError: boom"),
    ]
