"""Webhook -> apply loop (STU-849): what a notification is allowed to move.

Driven through the /graph route, whose notification carries the message ids
directly — the apply step is provider-neutral, so exercising it once is enough.
"""

import json

import pytest

pytest.importorskip("flask")

import yaml

from email_classifier.apply import status
from email_classifier.apply.executor import PROCESSED_LABEL
from email_classifier.apply.journal import ApplyJournal, read_entries
from email_classifier.apply.undo import undo_entries
from email_classifier.core.types import Classification
from email_classifier.orchestrate.stage2_client import Stage2Result
from tests.conftest import FakeWriter, RULES_DIR, inboxed
from tests.test_orchestrate_pipeline import FakeClassifier
from tests.test_webhook_graph import (
    FakeGraphSession,
    _client,
    _drain,
    _notification,
    _raw_message,
)


def _policy(tmp_path, **overrides):
    data = yaml.safe_load((RULES_DIR / "policy.yaml").read_text(encoding="utf-8"))
    # The floor is widened for the reason `conftest.served_policy` states: this mail
    # carries written-down dates so an opener can be ordered against its reply.
    data["start_date"] = "2000-01-01"
    data.update(overrides)
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _classifier(action="label_archive"):
    return FakeClassifier(results={"m1": Stage2Result(classification=Classification(
        thread_id="t-m1", bucket="C", subtype="product_newsletter", confidence="medium",
        suggested_action=action, suggested_label=None, reasoning="fake",
    ))})


def _notify(tmp_path, writer, *, auto_apply, action="label_archive", destructive=None,
            provider=None):
    overrides = {"auto_apply": auto_apply}
    if destructive is not None:
        overrides["destructive_actions_allowed"] = destructive
    if provider is not None:
        overrides["email"] = {"provider": provider}
    client = _client(
        tmp_path,
        FakeGraphSession(by_id={"m1": _raw_message("m1")}),
        stage2=_classifier(action),
        writer=writer,
        policy_path=_policy(tmp_path, **overrides),
    )
    response = client.post("/graph", json={"value": [_notification("m1")]})
    assert response.status_code == 202
    _drain(client)
    return response


def _journals(tmp_path):
    return sorted(p.name for p in (tmp_path / "logs" / "applied").iterdir())


def _run_id(tmp_path):
    (report,) = (tmp_path / "reports").iterdir()
    return json.loads(report.read_text(encoding="utf-8"))["run_id"]


def test_default_policy_plans_without_touching_the_mailbox(tmp_path):
    writer = inboxed(FakeWriter(), "m1")

    _notify(tmp_path, writer, auto_apply=False)

    assert writer.labels_of("m1") == {"INBOX"}
    assert writer.trashed == []
    run_id = _run_id(tmp_path)
    assert _journals(tmp_path) == [f"{run_id}.plan.jsonl"]
    entries = read_entries(tmp_path / "logs" / "applied" / f"{run_id}.plan.jsonl")
    assert {e["status"] for e in entries} == {"planned"}


def test_auto_apply_labels_and_archives_end_to_end(tmp_path):
    writer = inboxed(FakeWriter(), "m1")

    _notify(tmp_path, writer, auto_apply=True)

    assert writer.labels_of("m1") == {"ec/C", PROCESSED_LABEL}
    run_id = _run_id(tmp_path)
    assert _journals(tmp_path) == [f"{run_id}.jsonl"]


def test_an_auto_applied_run_is_undoable_from_its_journal(tmp_path):
    writer = inboxed(FakeWriter(), "m1")
    _notify(tmp_path, writer, auto_apply=True)
    journal_file = tmp_path / "logs" / "applied" / f"{_run_id(tmp_path)}.jsonl"

    outcomes = undo_entries(
        read_entries(journal_file), writer, ApplyJournal(tmp_path / "undo.jsonl", "undo")
    )

    assert {o.status for o in outcomes} == {"undone"}
    assert writer.labels_of("m1") == {"INBOX"}


def test_a_forbidden_destructive_verdict_is_downgraded_not_trashed(tmp_path):
    writer = inboxed(FakeWriter(), "m1")

    _notify(tmp_path, writer, auto_apply=True, action="mark_for_delete", destructive=[])

    assert writer.trashed == []
    assert writer.labels_of("m1") == {"ec/C", PROCESSED_LABEL}


def test_a_redelivered_notification_does_not_act_twice(tmp_path):
    writer = inboxed(FakeWriter(), "m1")
    _notify(tmp_path, writer, auto_apply=True)
    labels_after_first = set(writer.labels_of("m1"))

    _notify(tmp_path, writer, auto_apply=True)

    assert writer.labels_of("m1") == labels_after_first
    assert writer.trashed == []


def test_a_route_the_policy_does_not_name_classifies_without_applying(tmp_path):
    """Both routes can be live; only the one `email.provider` names may write."""
    writer = inboxed(FakeWriter(), "m1")

    _notify(tmp_path, writer, auto_apply=True, provider="gmail")

    assert writer.labels_of("m1") == {"INBOX"}
    assert not (tmp_path / "logs" / "applied").exists()
    assert _run_id(tmp_path)  # the classification still happened


def test_a_failed_operation_is_logged_and_still_acked(tmp_path, caplog):
    writer = inboxed(FakeWriter(fail_on=["m1"]), "m1")

    with caplog.at_level("ERROR"):
        _notify(tmp_path, writer, auto_apply=True)

    assert "failed to apply" in caplog.text
    entries = read_entries(tmp_path / "logs" / "applied" / f"{_run_id(tmp_path)}.jsonl")
    # The message leaves the Inbox, then fails to be labelled — the order the
    # bucket label is written last for (STU-963).
    assert [e["status"] for e in entries] == ["ok", "error"]


# ── two runs over one conversation (STU-963) ─────────────────────────────────


def _thread_classifier():
    """The opener is a newsletter; the reply a parent wrote into it is a question."""
    def _result(bucket, action):
        return Stage2Result(classification=Classification(
            thread_id="t-conv", bucket=bucket, subtype=None, confidence="medium",
            suggested_action=action, suggested_label=None, reasoning="fake",
        ))
    return FakeClassifier(results={
        "opener": _result("C", "label_archive"),
        "reply": _result("A", "needs_reply"),
    })


def _thread_notify(run_dir, writer, session, message_id):
    """One notification, with its own report and journal dirs — the mailbox (writer,
    session) is what the two runs share, exactly as two runs of the server would."""
    run_dir.mkdir()
    client = _client(
        run_dir,
        session,
        stage2=_thread_classifier(),
        writer=writer,
        policy_path=_policy(run_dir, auto_apply=True),
    )
    assert client.post(
        "/graph", json={"value": [_notification(message_id)]}
    ).status_code == 202
    _drain(client)


def test_a_reply_reconciles_the_thread_an_earlier_run_archived(tmp_path):
    """The issue's criterion. Run 1 notifies the opener alone and archives it; run 2
    notifies only the reply, but the fetch hands it the whole conversation — so both
    messages end in one place, and the second journal undoes exactly its own work."""
    writer = inboxed(FakeWriter(), "opener", "reply")
    session = FakeGraphSession(by_id={
        "opener": _raw_message(
            "opener", conversation="t-conv", received="2026-08-18T09:00:00Z"
        )
    })

    _thread_notify(tmp_path / "run1", writer, session, "opener")
    assert writer.labels_of("opener") == {"ec/C", PROCESSED_LABEL}

    # The reply lands after the first run, on the same conversation.
    session.by_id["reply"] = _raw_message(
        "reply", conversation="t-conv", received="2026-08-19T09:00:00Z"
    )
    _thread_notify(tmp_path / "run2", writer, session, "reply")

    assert writer.labels_of("opener") == writer.labels_of("reply") == {
        "INBOX", PROCESSED_LABEL
    }

    run_id = _run_id(tmp_path / "run2")
    undo_entries(
        read_entries(tmp_path / "run2" / "logs" / "applied" / f"{run_id}.jsonl"),
        writer,
        ApplyJournal(tmp_path / "undo.jsonl", "undo"),
    )
    assert writer.labels_of("opener") == {"ec/C", PROCESSED_LABEL}
    assert writer.labels_of("reply") == {"INBOX"}


class _NoDrafts:
    def find_draft(self, thread_id):
        return None

    def get_draft(self, draft_id):
        return None

    def last_sent_at(self, thread_id):
        return None


def test_status_labels_write_with_auto_apply_off_and_undo_removes_them(tmp_path):
    """STU-2007: the flag writes one status even when apply only plans, journaled as a
    real write so `make undo RUN_ID=` takes it off."""
    writer = inboxed(FakeWriter(), "m1")
    client = _client(
        tmp_path,
        FakeGraphSession(by_id={"m1": _raw_message("m1")}),
        stage2=_classifier("label_archive"),
        writer=writer,
        policy_path=_policy(tmp_path, auto_apply=False, status_labels=True),
        reply_drafter=_NoDrafts(),
        thread_reader=_NoDrafts(),
    )
    assert client.post("/graph", json={"value": [_notification("m1")]}).status_code == 202
    _drain(client)

    assert writer.labels_of("m1") == {"INBOX", status.NO_REPLY}
    run_id = _run_id(tmp_path)
    assert _journals(tmp_path) == [f"{run_id}.jsonl", f"{run_id}.plan.jsonl"]

    undo_entries(read_entries(tmp_path / "logs" / "applied" / f"{run_id}.jsonl"), writer,
                 ApplyJournal(tmp_path / "undo.jsonl", "undo"))
    assert writer.labels_of("m1") == {"INBOX"}
