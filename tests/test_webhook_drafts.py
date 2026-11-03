"""Webhook -> drafts (STU-946): what a notification is allowed to answer.

Driven through /graph, like the apply loop it follows: the drafting step reads
the report, not the route.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

import yaml

from email_classifier.core.types import Classification
from email_classifier.orchestrate.drafts_runner import StudioCliDrafter
from email_classifier.orchestrate.stage2_client import Stage2Result
from email_classifier.providers import HEADLESS_ENV
from email_classifier.webhook import state
from tests.conftest import FakeDrafts, FakeWriter, inboxed
from tests.test_orchestrate_pipeline import FakeClassifier
from tests.test_webhook_apply import _policy
from tests.test_webhook_graph import (
    FakeGraphSession,
    _client,
    _drain,
    _notification,
    _raw_message,
)
from tests.test_webhook_server import _client as _gmail_client
from tests.test_webhook_server import _envelope, _post, _service
from tests.test_webhook_server import _raw_message as _gmail_raw_message


def _classifier(action):
    return FakeClassifier(results={"m1": Stage2Result(classification=Classification(
        thread_id="t-m1", bucket="A", subtype="parent_question", confidence="medium",
        suggested_action=action, suggested_label=None, reasoning="fake",
    ))})


def _notify(tmp_path, drafts, *, action="needs_reply", policy_path=None):
    client = _client(
        tmp_path,
        FakeGraphSession(by_id={"m1": _raw_message("m1")}),
        stage2=_classifier(action),
        writer=inboxed(FakeWriter(), "m1"),
        drafts=drafts,
        policy_path=policy_path,
    )
    assert client.post("/graph", json={"value": [_notification("m1")]}).status_code == 202
    _drain(client)


def _run_id(tmp_path):
    (report,) = (tmp_path / "reports").iterdir()
    return json.loads(report.read_text(encoding="utf-8"))["run_id"]


def test_a_needs_reply_notification_drafts_from_its_own_report(tmp_path):
    drafts = FakeDrafts()

    _notify(tmp_path, drafts)

    report_path = tmp_path / "reports" / f"{_run_id(tmp_path)}.json"
    assert drafts.calls == [(report_path, ["m1"])]


def test_nothing_owed_a_reply_spends_no_drafting_run(tmp_path, caplog):
    drafts = FakeDrafts()

    with caplog.at_level("INFO"):
        _notify(tmp_path, drafts, action="label_archive")

    assert drafts.calls == []
    assert "nothing owes a reply" in caplog.text


def test_drafts_run_under_the_default_policy_that_only_plans_the_apply(tmp_path):
    """A draft is never sent, so the classify-only phase of the pilot still gets one —
    the batch path gates drafting on nothing either."""
    drafts = FakeDrafts()

    _notify(tmp_path, drafts, policy_path=_policy(tmp_path, auto_apply=False))

    assert [ids for _, ids in drafts.calls] == [["m1"]]


def test_a_failed_drafting_run_leaves_the_classification_standing(tmp_path, caplog):
    drafts = FakeDrafts(error=RuntimeError("`studio run drafts` exited 1"))

    _notify(tmp_path, drafts)

    # No retry: the queue is empty and the run counted once, so the worker did not
    # re-pay a stage-2 pass for a report already on disk.
    assert (tmp_path / "reports" / f"{_run_id(tmp_path)}.json").exists()
    assert len(drafts.calls) == 1
    assert "drafting 1 reply target(s) failed" in caplog.text


def test_a_notification_off_the_apply_provider_does_not_draft_either(tmp_path):
    """The drafter writes into the mailbox `email.provider` names, like the writer."""
    drafts = FakeDrafts()

    _notify(tmp_path, drafts, policy_path=_policy(tmp_path, email={"provider": "gmail"}))

    assert drafts.calls == []


def test_a_provider_with_no_drafting_path_spends_no_drafting_run(tmp_path, caplog):
    """Gmail has a drafter but no body fetcher, so `drafts-pre` would raise — and the
    push path would re-pay a subprocess to rediscover it on every notification."""
    drafts = FakeDrafts()
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    client = _gmail_client(
        tmp_path,
        _service(
            history_pages=[{"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}],
            raw_by_id={"m1": _gmail_raw_message("m1")},
        ),
        stage2=_classifier("needs_reply"),
        writer=inboxed(FakeWriter(), "m1"),
        drafts=drafts,
        policy_path=_policy(tmp_path, email={"provider": "gmail"}),
    )

    with caplog.at_level("INFO"):
        _post(client, _envelope("200"))

    assert drafts.calls == []
    assert "1 reply target(s) not drafted" in caplog.text
    assert "has no drafting path" in caplog.text


# ── the CLI runner itself ────────────────────────────────────────────────────


def _spawn(monkeypatch, returncode=0):
    """Record what `studio` was called with — the input file is gone once draft returns."""
    recorded = {}

    def fake_run(cmd, **kwargs):
        input_file = Path(cmd[cmd.index("--input-file") + 1])
        recorded["cmd"] = cmd
        recorded["env"] = kwargs["env"]
        recorded["input"] = yaml.safe_load(input_file.read_text(encoding="utf-8"))
        recorded["corpus"] = json.loads(
            Path(recorded["input"]["corpus"]).read_text(encoding="utf-8")
        )
        return subprocess.CompletedProcess(cmd, returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return recorded


def test_the_runner_hands_studio_the_report_and_a_corpus_of_the_run(tmp_path, monkeypatch):
    recorded = _spawn(monkeypatch)
    report = tmp_path / "reports" / "r1.json"

    StudioCliDrafter(provider="claude-code", cwd=tmp_path).draft(
        report, [{"message_id": "m1"}]
    )

    assert recorded["cmd"][:3] == ["studio", "run", "drafts"]
    assert recorded["cmd"][-2:] == ["--provider", "claude-code"]
    assert recorded["input"]["report"] == str(report)
    assert recorded["corpus"] == [{"message_id": "m1"}]


def test_the_child_is_told_there_is_nobody_to_consent(tmp_path, monkeypatch):
    """`drafts-pre` builds its own drafter and thread reader, out of reach of the
    `interactive=False` the webhook passes its own clients (STU-930)."""
    recorded = _spawn(monkeypatch)

    StudioCliDrafter(cwd=tmp_path).draft(tmp_path / "r1.json", [])

    assert recorded["env"][HEADLESS_ENV] == "1"
    assert HEADLESS_ENV not in os.environ


def test_the_corpus_does_not_outlive_the_run(tmp_path, monkeypatch):
    """A push run's mail never touched disk and its provenance says so (STU-859)."""
    recorded = _spawn(monkeypatch)

    StudioCliDrafter(cwd=tmp_path).draft(tmp_path / "r1.json", [{"message_id": "m1"}])

    assert not Path(recorded["input"]["corpus"]).exists()


def test_a_nonzero_studio_exit_raises(tmp_path, monkeypatch):
    _spawn(monkeypatch, returncode=1)

    with pytest.raises(RuntimeError, match="exited 1"):
        StudioCliDrafter(cwd=tmp_path).draft(tmp_path / "r1.json", [])


def test_the_audit_entry_is_filed_after_the_drafting_it_prices(tmp_path):
    """Filed before it, the entry reads a cache hit ratio for the classify leg alone.

    The drafts leg only reaches `logs/drafts/stages.jsonl` once the fan-out has run, and
    it is the leg STU-1057 exists for — it is the one that ran at four times the price
    for weeks. An entry filed early misses it permanently, since the run is covered from
    then on and no later walk re-reads it (STU-1084).
    """
    audit_log = tmp_path / "audits" / "audit_log.json"

    class RecordingDrafts(FakeDrafts):
        def draft(self, report_path, email_dicts):
            self.log_existed_at_draft_time = audit_log.exists()
            return super().draft(report_path, email_dicts)

    drafts = RecordingDrafts()
    _notify(tmp_path, drafts)

    assert drafts.calls, "the drafting step must have run"
    assert drafts.log_existed_at_draft_time is False
    assert audit_log.exists()


def test_a_drafting_failure_still_leaves_the_run_filed(tmp_path):
    """`finally`: drafts is the tolerated-failure stage, and the run still owes an entry."""
    _notify(tmp_path, FakeDrafts(error=RuntimeError("`studio run drafts` exited 1")))

    (entry,) = json.loads((tmp_path / "audits" / "audit_log.json").read_text())
    assert entry["run_id"] == _run_id(tmp_path)
