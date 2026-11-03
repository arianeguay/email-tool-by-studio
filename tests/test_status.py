"""Status categories on the latest inbound message of each conversation (STU-2007)."""

from dataclasses import asdict

import pytest

from email_classifier.apply import status
from email_classifier.apply.executor import apply_verdicts
from email_classifier.apply.journal import ApplyJournal, read_entries
from email_classifier.apply.undo import undo_entries
from email_classifier.placeholder import PLACEHOLDER_CATEGORY
from email_classifier.stage1.policy import load_policy
from tests.conftest import FakeWriter, inboxed
from tests.test_executor import TAXONOMY, make_policy, verdict

EARLY = "2026-01-05T10:00:00+00:00"
LATE = "2026-01-05T12:00:00+00:00"
DRAFT = {"id": "d1", "isDraft": True, "lastModifiedDateTime": LATE, "categories": []}


def report(*verdicts):
    return {"verdicts": [asdict(v) for v in verdicts]}


def state(hold=None, draft=None, last_sent=None):
    return lambda _verdict: status.ReplyState(hold, draft, last_sent)


def derive(*verdicts, look=state()):
    return status.derive(report(*verdicts), TAXONOMY, look)


@pytest.mark.parametrize("reply_state, expected", [
    (state(draft=DRAFT), status.DRAFTED),
    (state(draft={**DRAFT, "categories": [PLACEHOLDER_CATEGORY]}), status.TO_COMPLETE),
    (state(last_sent=LATE), status.ANSWERED),
    (state(draft={**DRAFT, "lastModifiedDateTime": EARLY}, last_sent=LATE), status.ANSWERED),
    (state(hold="thanks", draft=DRAFT), status.YOURS),
    (state(), status.ERROR),
])
def test_a_reply_owed_reads_its_status_off_the_mailbox(reply_state, expected):
    v = verdict(bucket="A", action="needs_reply", date=EARLY)
    assert derive(v, look=reply_state) == {"m1": expected}


def test_no_reply_due_is_said_and_the_mailbox_is_not_asked():
    def look(_verdict):
        raise AssertionError("asked about a conversation owed nothing")

    assert derive(verdict(bucket="B", action="label_archive"), look=look) == {
        "m1": status.NO_REPLY}


def test_an_errored_message_marks_its_conversation_as_an_error():
    v = verdict(status="error", bucket=None, action=None, error="stage2_timeout")
    assert derive(v) == {"m1": status.ERROR}


def test_one_status_per_conversation_on_its_latest_message():
    older = verdict("m1", bucket="A", action="needs_reply", thread_id="t", date=EARLY)
    newer = verdict("m2", bucket="A", action="keep_visible", thread_id="t", date=LATE)
    assert derive(older, newer) == {"m2": status.NO_REPLY}


def test_a_reply_the_clamp_dropped_is_the_reviewers():
    asked = verdict("m1", bucket="A", action="needs_reply", thread_id="t", date=EARLY)
    notice = verdict("m2", bucket="B", action="label_archive", thread_id="t", date=LATE)
    assert derive(asked, notice) == {"m2": status.YOURS}


def _write(verdicts, statuses, writer, tmp_path):
    journal = ApplyJournal(tmp_path / "run.jsonl", "run")
    failed = status.write(report(*verdicts), statuses, writer, journal)
    return failed, journal.path


def test_a_new_status_replaces_ours_and_leaves_a_human_category(tmp_path):
    writer = inboxed(FakeWriter(), "m1")
    writer.add_label("m1", writer.ensure_label("Voicemail"))
    writer.add_label("m1", writer.ensure_label(status.DRAFTED))

    _write([verdict()], {"m1": status.ANSWERED}, writer, tmp_path)

    assert writer.labels_of("m1") == {"INBOX", "Voicemail", status.ANSWERED}


def test_an_earlier_message_loses_its_status_when_a_later_one_takes_it(tmp_path):
    writer = inboxed(FakeWriter(), "m1", "m2")
    writer.add_label("m1", writer.ensure_label(status.DRAFTED))
    verdicts = [verdict("m1", thread_id="t"), verdict("m2", thread_id="t")]

    _write(verdicts, {"m2": status.NO_REPLY}, writer, tmp_path)

    assert writer.labels_of("m1") == {"INBOX"}
    assert writer.labels_of("m2") == {"INBOX", status.NO_REPLY}


def test_a_converged_message_writes_nothing(tmp_path):
    writer = inboxed(FakeWriter(), "m1")
    writer.add_label("m1", writer.ensure_label(status.DRAFTED))

    _, path = _write([verdict()], {"m1": status.DRAFTED}, writer, tmp_path)

    assert not path.exists()


def test_a_message_in_the_trash_is_left_alone(tmp_path):
    writer = FakeWriter()
    writer.message_labels["m1"] = {"TRASH"}

    _write([verdict()], {"m1": status.ERROR}, writer, tmp_path)

    assert writer.labels_of("m1") == {"TRASH"}


def test_undo_takes_the_statuses_back_off(tmp_path):
    writer = inboxed(FakeWriter(), "m1")
    writer.add_label("m1", writer.ensure_label(status.DRAFTED))
    _, path = _write([verdict()], {"m1": status.ANSWERED}, writer, tmp_path)

    undo_entries(read_entries(path), writer, ApplyJournal(tmp_path / "undo.jsonl", "u"))

    assert writer.labels_of("m1") == {"INBOX", status.DRAFTED}


def test_a_failing_message_is_journaled_and_the_rest_continue(tmp_path):
    writer = inboxed(FakeWriter(fail_on={"m1"}), "m1", "m2")

    failed, path = _write([verdict("m1"), verdict("m2")],
                          {"m1": status.ERROR, "m2": status.ERROR}, writer, tmp_path)

    assert failed == 1
    assert writer.labels_of("m2") == {"INBOX", status.ERROR}
    assert [e["status"] for e in read_entries(path)] == ["error", "ok"]


@pytest.mark.parametrize("name", status.STATUS_LABELS)
def test_apply_never_reads_a_status_as_a_filing(name, tmp_path):
    """The executor's state diff resolves only `ec/<bucket>`: a status on an archived
    message must not read as the bucket it was filed under, nor block a refiling."""
    writer = FakeWriter()
    writer.message_labels["m1"] = set()
    writer.add_label("m1", writer.ensure_label(name))

    apply_verdicts([verdict(bucket="B")], writer, make_policy(),
                   ApplyJournal(tmp_path / "apply.jsonl", "r"), TAXONOMY, dry_run=False)

    assert {"ec/B", name} <= writer.labels_of("m1")


def _policy_file(tmp_path, body):
    path = tmp_path / "policy.yaml"
    path.write_text("deterministic_can_finalize: true\n" + body, encoding="utf-8")
    return path


def test_the_flag_defaults_off_and_reads_on_for_outlook(tmp_path):
    assert load_policy(_policy_file(tmp_path, "")).status_labels is False
    on = _policy_file(tmp_path, "status_labels: true\nemail:\n  provider: outlook\n")
    assert load_policy(on).status_labels is True


@pytest.mark.parametrize("body, match", [
    ("status_labels: 'true'\nemail:\n  provider: outlook\n", "true or false"),
    ("status_labels: true\nemail:\n  provider: imap\n", "needs email.provider outlook"),
    ("status_labels: true\n", "needs email.provider outlook"),
])
def test_the_flag_is_refused_where_it_cannot_hold(tmp_path, body, match):
    with pytest.raises(ValueError, match=match):
        load_policy(_policy_file(tmp_path, body))
