"""Webhook endpoint tests: fake Gmail service, fake classifier, fake verifier."""

import base64
import json
import logging
import signal
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")

from email_classifier.audit import UNREVIEWED
from email_classifier.anonymize import Tokenizer
from email_classifier.fetch.gmail.client import GrantAware
from email_classifier.learn.store import load_observations
from email_classifier.orchestrate.provenance import resolved_provider
from email_classifier.webhook import server as webhook_server
from email_classifier.webhook import state
from email_classifier.webhook.server import create_app
from tests.conftest import RULES_DIR, FakeDrafts, FakeWriter, pin_gliner, webhook_paths, served_policy
from tests.test_gmail_fetch import _HttpError
from tests.test_gmail_push import FakeService, _Call
from tests.test_orchestrate_pipeline import FakeClassifier
from email_classifier import deployment


class FakeMessages:
    def __init__(self, raw_by_id):
        self.raw_by_id = raw_by_id

    def get(self, *, id, **_kwargs):
        return _Call(self.raw_by_id[id])


class FakeThreads:
    """Serves `threads().get` from the same canned messages (STU-963)."""

    def __init__(self, raw_by_id):
        self.raw_by_id = raw_by_id

    def get(self, *, id, **_kwargs):
        return _Call({"messages": [
            {"id": raw["id"], "labelIds": raw["labelIds"]}
            for raw in self.raw_by_id.values()
            if raw["threadId"] == id
        ]})


class FakeLabels:
    """`labels().list`, the id -> name map the trash exemption reads the marker
    through (STU-983). This fake has no `ec/deleted`, so every trashed message it
    serves is one a human threw out."""

    def list(self, **_kwargs):
        return _Call({"labels": [{"id": "Label_7", "name": "ec/processed"}]})


def _raw_message(message_id, from_address="stranger@unknown-domain.test"):
    return {
        "id": message_id,
        "threadId": f"t-{message_id}",
        "internalDate": "1755500000000",
        "snippet": "hello",
        "labelIds": ["INBOX"],
        "payload": {"headers": [
            {"name": "From", "value": from_address},
            {"name": "Subject", "value": "S"},
        ]},
    }


def _service(history_pages, raw_by_id=None):
    service = FakeService(history_pages=history_pages)
    service.users()._messages = FakeMessages(raw_by_id or {})
    service.users().messages = lambda: service.users()._messages
    service.users()._threads = FakeThreads(raw_by_id or {})
    service.users().threads = lambda: service.users()._threads
    service.users()._labels = FakeLabels()
    service.users().labels = lambda: service.users()._labels
    return service


def _envelope(history_id):
    data = base64.b64encode(
        json.dumps({"emailAddress": "me@example.com", "historyId": history_id}).encode()
    ).decode()
    return {"message": {"data": data, "messageId": "1"}, "subscription": "s"}


def _client(tmp_path, service, stage2=None, verify_token=None, learned_path=None,
            writer=None, policy_path=None, drafts=None, **kwargs):
    paths = webhook_paths(tmp_path)
    if learned_path:
        paths["learned_path"] = learned_path
    app = create_app(
        service=service,
        stage2=stage2 or FakeClassifier(),
        writer=writer or FakeWriter(),
        drafts=drafts or FakeDrafts(),
        verify_token=verify_token or (lambda token: None),
        rules_path=RULES_DIR / "rules.yaml",
        policy_path=policy_path or served_policy(tmp_path),
        # `rules/policy.yaml` ships `scope: full`, so every run through this client
        # tokenizes. Format regexes only: the `ner` extra stays optional (STU-857), and
        # the person half has its own tests.
        tokenizer_factory=lambda: Tokenizer(persons=False),
        retry_backoff_seconds=0,
        debounce_seconds=0,
        **paths,
        **kwargs,
    )
    return app.test_client()


def _post(client, envelope, token="Bearer good"):
    """Post, then wait for the out-of-band worker: the route only acks (STU-927)."""
    headers = {"Authorization": token} if token else {}
    response = client.post("/pubsub", json=envelope, headers=headers)
    assert client.application.extensions["webhook_queue"].wait_empty(timeout=10)
    return response


def test_new_messages_are_classified_and_state_advances(tmp_path):
    service = _service(
        history_pages=[{"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}],
        raw_by_id={"m1": _raw_message("m1")},
    )
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    stage2 = FakeClassifier()
    client = _client(tmp_path, service, stage2=stage2)

    response = _post(client, _envelope("200"))

    assert response.status_code == 204
    assert stage2.seen == ["m1"]  # catch-all escalated to the fake LLM
    assert state.load_history_id(tmp_path / "state" / "gmail-history.json") == "200"
    reports = list((tmp_path / "reports").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report["totals"]["emails"] == 1
    assert (tmp_path / "logs" / "frontier.jsonl").read_text().count("\n") == 1
    # The run files its own audit entry, so `audit-gate` no longer holds the next
    # one on a report nobody transcribed (STU-1084) — with the reading still owed.
    entry, = json.loads((tmp_path / "audits" / "audit_log.json").read_text())
    assert entry["run_id"] == report["run_id"]
    assert entry["verdict"].startswith(UNREVIEWED)


def test_a_message_the_mailbox_no_longer_holds_as_incoming_gets_no_verdict(tmp_path):
    """The queue is drained off-band, and history.list reports every label (STU-993)."""
    service = _service(
        history_pages=[{"history": [{"messagesAdded": [
            {"message": {"id": "m1"}}, {"message": {"id": "m2"}}, {"message": {"id": "m3"}},
        ]}]}],
        raw_by_id={
            "m1": {**_raw_message("m1"), "labelIds": ["TRASH"]},
            "m2": {**_raw_message("m2"), "labelIds": ["SENT"]},
            "m3": _raw_message("m3"),
        },
    )
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    client = _client(tmp_path, service)

    _post(client, _envelope("200"))

    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text())
    assert [v["message_id"] for v in report["verdicts"]] == ["m3"]


def test_no_new_messages_still_advances_state(tmp_path):
    service = _service(history_pages=[{"historyId": "150"}])
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    client = _client(tmp_path, service)

    assert _post(client, _envelope("150")).status_code == 204
    assert state.load_history_id(tmp_path / "state" / "gmail-history.json") == "150"
    assert not (tmp_path / "reports").exists()


def test_a_redelivered_notification_counts_once_as_a_notification_and_not_as_a_run(tmp_path):
    """The two counters are only comparable if a replay shows up in one of them (STU-959).

    Redelivery is exactly what the phase-one gate is watching for, and the stored
    historyId is what makes it visible: the second envelope names a point Gmail has
    nothing new past, so it is acked and counted with no run behind it.
    """
    service = _service(
        history_pages=[
            {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]},
            {"historyId": "200"},
        ],
        raw_by_id={"m1": _raw_message("m1")},
    )
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    client = _client(tmp_path, service)

    _post(client, _envelope("200"))
    _post(client, _envelope("200"))

    health = client.get("/healthz").get_json()
    assert health["notifications_acked"] == 2
    assert health["runs_completed"] == 1
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1


def test_missing_state_bootstraps_baseline_without_fetching(tmp_path):
    service = _service(history_pages=[])
    client = _client(tmp_path, service)

    assert _post(client, _envelope("300")).status_code == 204
    assert state.load_history_id(tmp_path / "state" / "gmail-history.json") == "300"


def test_expired_history_id_rebaselines_and_acks(tmp_path):
    error = Exception("expired")
    error.resp = SimpleNamespace(status=404)
    service = _service(history_pages=[error])
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "old")
    client = _client(tmp_path, service)

    assert _post(client, _envelope("500")).status_code == 204
    assert state.load_history_id(tmp_path / "state" / "gmail-history.json") == "500"


def test_processing_error_keeps_state_so_the_next_notification_covers_the_gap(tmp_path):
    """The notification is already acked, so the messages it announced only come back
    in the next history.list window — which needs the stored historyId held back."""
    service = _service(history_pages=[Exception("boom")])
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    client = _client(tmp_path, service, max_attempts=1)

    assert _post(client, _envelope("200")).status_code == 204
    assert state.load_history_id(tmp_path / "state" / "gmail-history.json") == "100"
    assert (tmp_path / "state" / "notifications.jsonl").read_text() == ""


def test_rejects_missing_or_invalid_token(tmp_path):
    def deny(token):
        raise ValueError("bad token")

    service = _service(history_pages=[])
    client = _client(tmp_path, service, verify_token=deny)

    assert _post(client, _envelope("1"), token=None).status_code == 403
    assert _post(client, _envelope("1")).status_code == 403


def test_malformed_envelope_is_acked_and_dropped(tmp_path):
    service = _service(history_pages=[])
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    client = _client(tmp_path, service)

    assert _post(client, {"message": {"data": "not-base64-json"}}).status_code == 204
    assert _post(client, {"nope": True}).status_code == 204
    assert state.load_history_id(tmp_path / "state" / "gmail-history.json") == "100"


def test_a_promoted_rule_is_picked_up_without_a_restart(tmp_path):
    """`make promote` runs out-of-process; a webhook that loaded rules once at startup
    would escalate the same sender forever (STU-822)."""
    sender = "stranger@unknown-domain.test"
    service = _service(
        history_pages=[
            {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]},
            {"history": [{"messagesAdded": [{"message": {"id": "m2"}}]}]},
        ],
        raw_by_id={"m1": _raw_message("m1", sender), "m2": _raw_message("m2", sender)},
    )
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    learned = tmp_path / "rules" / "learned.yaml"
    stage2 = FakeClassifier()
    client = _client(tmp_path, service, stage2=stage2, learned_path=learned)

    assert _post(client, _envelope("200")).status_code == 204
    assert stage2.seen == ["m1"]

    learned.parent.mkdir(parents=True, exist_ok=True)
    learned.write_text(
        json.dumps({"rules": [{
            "id": "learned_stranger", "conditions": {"from_address": sender},
            "bucket": "C", "confidence": "medium", "action": "label_archive",
        }]}),
        encoding="utf-8",
    )

    assert _post(client, _envelope("300")).status_code == 204
    assert stage2.seen == ["m1"]  # m2 was decided deterministically


def test_escalated_verdicts_are_recorded_in_the_verdict_store(tmp_path):
    service = _service(
        history_pages=[{"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}],
        raw_by_id={"m1": _raw_message("m1")},
    )
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    client = _client(tmp_path, service)

    assert _post(client, _envelope("200")).status_code == 204

    observations = load_observations(tmp_path / "logs" / "verdicts.jsonl")
    assert [o.from_address for o in observations] == ["stranger@unknown-domain.test"]


def test_create_app_refuses_a_studio_ignoring_the_scope(monkeypatch, tmp_path):
    """STU-834: the same version-skew guard as run_email, at webhook boot."""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/studio")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="0.15.1\n", stderr=""),
    )
    monkeypatch.setenv("PUBSUB_AUDIENCE", "https://example.test/push")
    # The scope preflight refuses on the absent extra before it ever reads the
    # version this test is about (STU-908).
    pin_gliner(monkeypatch)

    with pytest.raises(RuntimeError, match="0.15.1"):
        create_app(state_file=tmp_path / "state.json")


# --- a Gmail grant that dies mid-run (STU-953) ---------------------------------


class _RefusingMessages:
    def get(self, **_kwargs):
        return self

    def execute(self, **_kwargs):
        raise _HttpError(401)


def _grant_lost_client(tmp_path, monkeypatch):
    """A webhook whose next Gmail call finds the grant gone, with SIGTERM stubbed."""
    signals = []
    alerted = []
    monkeypatch.setattr(webhook_server.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(webhook_server.alerts, "notify", alerted.append)
    service = _service(
        history_pages=[{"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}],
    )
    service.users()._messages = _RefusingMessages()
    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    return _client(tmp_path, GrantAware(service)), signals, alerted


def _wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _post_unacked(client):
    """`_post` waits for a drained queue; a halted worker never drains one."""
    return client.post(
        "/pubsub", json=_envelope("200"), headers={"Authorization": "Bearer good"}
    )


def test_a_lost_gmail_grant_alerts_and_stops_the_worker(tmp_path, monkeypatch):
    """STU-953: a 401 mid-run is the grant, so it takes the halt path Graph
    already takes instead of being dropped as one more transient failure."""
    client, signals, alerted = _grant_lost_client(tmp_path, monkeypatch)

    response = _post_unacked(client)

    assert response.status_code == 204
    assert _wait_for(lambda: signals)
    assert signals == [signal.SIGTERM]
    assert "make gmail-consent" in alerted[0]


def test_a_lost_gmail_grant_leaves_the_notification_queued(tmp_path, monkeypatch):
    """The ack promised no redelivery, so the item has to outlive the halt."""
    client, signals, _ = _grant_lost_client(tmp_path, monkeypatch)

    _post_unacked(client)

    assert _wait_for(lambda: signals)
    queue_file = tmp_path / "state" / "notifications.jsonl"
    assert "200" in queue_file.read_text(encoding="utf-8")


# --- which provider stage 2 runs through (STU-1005) -----------------------------


def _boot_with_provider(monkeypatch, tmp_path, provider, studio_version="0.17.0",
                        api_key="test-key"):
    """Boot far enough to build the real classifier, without a Studio on the host."""
    import shutil
    import subprocess

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/studio")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kwargs: SimpleNamespace(
            returncode=0, stdout=f"{studio_version}\n", stderr=""
        ),
    )
    monkeypatch.setenv("PUBSUB_AUDIENCE", "https://example.test/push")
    # Same preflight, same reason: without this the boot stops on the missing extra
    # rather than reaching the provider warning under test.
    pin_gliner(monkeypatch)
    # And the same again for the credential (STU-1128): a boot whose resolved provider
    # has none refuses. What is under test here is which provider a host runs, not
    # whether it can — except in the one case that passes `api_key=None`.
    if api_key is None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    else:
        monkeypatch.setenv("ANTHROPIC_API_KEY", api_key)
    if provider is None:
        monkeypatch.delenv("STUDIO_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("STUDIO_PROVIDER", provider)

    return create_app(
        service=_service([]),
        writer=FakeWriter(),
        drafts=FakeDrafts(),
        verify_token=lambda token: None,
        rules_path=RULES_DIR / "rules.yaml",
        policy_path=served_policy(tmp_path),
        **webhook_paths(tmp_path),
    )


def test_a_sealed_studio_stops_the_inheritance_warning(monkeypatch, tmp_path, caplog):
    """STU-1118: from 0.17.1 the spawn carries `--setting-sources \'\'`, so the four files
    the warning names reach no agent — and a warning that outlives what it describes is
    read as the reason a leak is still possible."""
    with caplog.at_level(logging.INFO, logger="email_classifier.webhook"):
        _boot_with_provider(monkeypatch, tmp_path, "claude-code", studio_version="0.17.1")

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not [r for r in warnings if "inherits" in r.getMessage()]
    # Silent would read like nobody looked: which provider a host runs is still one line.
    assert "sealed" in caplog.text and "claude-code" in caplog.text


@pytest.mark.parametrize(
    "stdout, sealed",
    [("0.17.0\n", False), ("0.17.1\n", True), ("1.0.0\n", True), ("", False)],
)
def test_only_a_readable_version_at_or_past_the_fix_counts_as_sealed(
    monkeypatch, stdout, sealed
):
    """A version that cannot be read fails toward the loud answer: boot only reaches
    this on a policy with no scope, where nothing else has already refused."""
    import subprocess

    from email_classifier.orchestrate import stage2_client

    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )
    assert (stage2_client.claude_code_inherits_host_config() is None) is sealed


def test_the_claude_code_provider_says_what_it_inherits(monkeypatch, tmp_path, caplog):
    """STU-1005: opting back in hands the host's own CLAUDE.md to every classification,
    and an env line set in silence is how that comes back unnoticed."""
    with caplog.at_level(logging.WARNING, logger="email_classifier.webhook"):
        _boot_with_provider(monkeypatch, tmp_path, "claude-code")

    assert "claude-code" in caplog.text
    assert "CLAUDE.md" in caplog.text
    assert "STUDIO_PROVIDER=anthropic" in caplog.text


def test_a_provider_without_its_credential_refuses_at_startup(monkeypatch, tmp_path):
    """STU-1128: a provider missing its key fails every call the same way, so the server
    would sit there accepting notifications it can only drop — the silence `/healthz`
    cannot distinguish from a quiet mailbox. The refusal names the stage and the
    provider, since both stage-2 passes run under the one STUDIO_PROVIDER."""
    with pytest.raises(RuntimeError, match="classify: provider `anthropic`"):
        _boot_with_provider(monkeypatch, tmp_path, "anthropic", api_key=None)


def test_the_provider_that_needs_no_credential_boots(monkeypatch, tmp_path):
    """The other half, and why the guard is not just `is the key set`: `claude-code`
    interpolates nothing, so a host riding the subscription starts with no key at all."""
    _boot_with_provider(monkeypatch, tmp_path, "claude-code", api_key=None)


def test_the_anthropic_provider_inherits_nothing_to_warn_about(monkeypatch, tmp_path, caplog):
    """It spawns no CLI, so there is no host user and no cwd in the prompt."""
    with caplog.at_level(logging.WARNING, logger="email_classifier.webhook"):
        _boot_with_provider(monkeypatch, tmp_path, "anthropic")

    assert "CLAUDE.md" not in caplog.text


def test_an_unset_env_var_leaves_both_stages_on_the_config_default(
    monkeypatch, tmp_path, caplog
):
    """STU-1013 kept STUDIO_PROVIDER free of a default, because the flag it becomes
    beats the provider an agent names itself — defaulting it re-pinned the classifier
    to the CLI on every host that never set the variable. That still holds; what
    changed is that STU-1133 removed the classifier's own pin, so with the variable
    unset both stages now fall to `defaults.provider` together."""
    with caplog.at_level(logging.WARNING, logger="email_classifier.webhook"):
        _boot_with_provider(monkeypatch, tmp_path, None)

    studio = webhook_server.REPO_ROOT / ".studio"
    assert resolved_provider(studio) == "claude-code"
    assert resolved_provider(studio, agent="drafter") == "claude-code"
    # Named together only because both were resolved from the config rather than from
    # a constant: the line is per stage, so a regression to one hardcoded answer for
    # the run would still have to name which stage it meant.
    assert "classify and drafts runs through the `claude-code` provider" in caplog.text


def test_the_env_var_still_overrides_what_the_config_defaults_to(
    monkeypatch, tmp_path, caplog
):
    """The half of STU-1013 that the removed pin used to demonstrate. With no agent
    naming a provider, `defaults.provider` is the only thing left for an unset variable
    to resolve to, so the override direction is what still distinguishes a real
    resolution from a hardcoded one."""
    with caplog.at_level(logging.WARNING, logger="email_classifier.webhook"):
        _boot_with_provider(monkeypatch, tmp_path, "anthropic")

    studio = webhook_server.REPO_ROOT / ".studio"
    assert resolved_provider(studio, "anthropic") == "anthropic"
    assert resolved_provider(studio, "anthropic", agent="drafter") == "anthropic"
    assert "claude-code" not in caplog.text
