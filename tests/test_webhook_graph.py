"""Webhook /graph route: handshake, clientState auth, classify-on-notification."""

import json
import logging
import signal
import threading
import time

import pytest

pytest.importorskip("flask")

from email_classifier.anonymize import _FORMAT_PATTERNS, Tokenizer
from email_classifier.fetch.outlook.client import GrantLost
from email_classifier.learn.store import load_observations
from email_classifier.webhook import server as webhook_server
from email_classifier.webhook.server import create_app
from tests.conftest import BURST_WINDOW, RULES_DIR, FakeDrafts, FakeWriter, webhook_paths, served_policy
from tests.test_orchestrate_pipeline import FakeClassifier

CLIENT_STATE = "s3cret"
NOTIFICATION_URL = "https://host/graph"


class FakeGraphSession:
    """Fake GraphSession serving canned `GET /me/messages/{id}` resources.

    Also answers the two reads the conversation expansion makes (STU-963): the
    well-known folder ids it drops, and the conversation listing. Neither is
    recorded in `get_ids`, which stays what it was — the messages actually read.
    """

    mailbox_path = "/me"

    def __init__(self, by_id=None, error=None):
        self.by_id = by_id or {}
        self.error = error
        self.get_ids = []

    def get(self, url, params=None, prefer=None):
        if "/mailFolders(" in url:
            return {"id": url.split("'")[1]}
        if url.endswith("/messages"):
            conversation = (params or {}).get("$filter", "").split("'")[1]
            return {"value": [
                {"id": raw["id"], "receivedDateTime": raw["receivedDateTime"],
                 "parentFolderId": "inbox"}
                for raw in self.by_id.values()
                if raw["conversationId"] == conversation
            ]}
        message_id = url.rsplit("/", 1)[-1]
        self.get_ids.append(message_id)
        if self.error:
            raise self.error
        return self.by_id[message_id]


def _raw_message(
    message_id,
    from_address="stranger@unknown-domain.test",
    conversation=None,
    received="2026-08-18T00:00:00Z",
    body="hello",
):
    return {
        "id": message_id,
        "conversationId": conversation or f"t-{message_id}",
        "from": {"emailAddress": {"address": from_address}},
        "subject": "S",
        "uniqueBody": {"contentType": "text", "content": body},
        "receivedDateTime": received,
        "inferenceClassification": "focused",
    }


def _client(tmp_path, session, stage2=None, writer=None, policy_path=None, drafts=None,
            **kwargs):
    kwargs.setdefault("debounce_seconds", 0)
    app = create_app(
        stage2=stage2 or FakeClassifier(),
        writer=writer or FakeWriter(),
        drafts=drafts or FakeDrafts(),
        graph_session=session,
        graph_client_state=CLIENT_STATE,
        graph_notification_url=NOTIFICATION_URL,
        rules_path=RULES_DIR / "rules.yaml",
        policy_path=policy_path or served_policy(tmp_path),
        tokenizer_factory=lambda: Tokenizer(persons=False),
        retry_backoff_seconds=0,
        **webhook_paths(tmp_path),
        **kwargs,
    )
    return app.test_client()


def _notification(message_id, client_state=CLIENT_STATE):
    return {"clientState": client_state, "changeType": "created",
            "resourceData": {"id": message_id}}


def _drain(client):
    """Wait for the out-of-band worker to finish what the ack promised."""
    assert client.application.extensions["webhook_queue"].wait_empty(timeout=10)


def test_validation_handshake_echoes_token(tmp_path):
    client = _client(tmp_path, FakeGraphSession())

    response = client.post("/graph?validationToken=abc%20123")

    assert response.status_code == 200
    assert response.text == "abc 123"
    assert response.content_type.startswith("text/plain")


@pytest.mark.parametrize(
    "token",
    ["<script>alert(1)</script>", "x" * 257, "", "café", "line\nbreak"],
    ids=["markup", "too-long", "empty", "non-ascii", "control-char"],
)
def test_handshake_refuses_to_echo_an_unbounded_token(tmp_path, token):
    client = _client(tmp_path, FakeGraphSession())

    response = client.post("/graph", query_string={"validationToken": token})

    assert response.status_code == 400
    assert response.text == ""


def test_notification_classifies_and_writes_report(tmp_path):
    session = FakeGraphSession(by_id={"m1": _raw_message("m1")})
    stage2 = FakeClassifier()
    client = _client(tmp_path, session, stage2=stage2)

    response = client.post("/graph", json={"value": [_notification("m1")]})
    _drain(client)

    assert response.status_code == 202
    assert session.get_ids == ["m1"]
    assert stage2.seen == ["m1"]  # catch-all escalated to the fake LLM
    reports = list((tmp_path / "reports").glob("*.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["totals"]["emails"] == 1
    assert (tmp_path / "logs" / "frontier.jsonl").read_text().count("\n") == 1


def test_the_report_names_the_mailbox_the_notification_came_from(tmp_path, monkeypatch):
    """STU-1183: the push path is where two mailboxes have been served, and
    `input_path: webhook:push-notification` is the same string on either."""
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")
    client = _client(tmp_path, FakeGraphSession(by_id={"m1": _raw_message("m1")}))

    client.post("/graph", json={"value": [_notification("m1")]})
    _drain(client)

    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text())
    assert report["provenance"]["mailbox"] == "inbox@club.example"


def test_a_message_trashed_before_the_worker_drains_gets_no_verdict(tmp_path):
    """The queue is drained off-band, so the notified mail can be gone by now (STU-988)."""
    session = FakeGraphSession(by_id={
        "m1": {**_raw_message("m1"), "parentFolderId": "deleteditems"},
        "m2": {**_raw_message("m2"), "parentFolderId": "junkemail"},
        "m3": {**_raw_message("m3"), "parentFolderId": "inbox"},
    })
    client = _client(tmp_path, session)

    client.post("/graph", json={"value": [
        _notification("m1"), _notification("m2"), _notification("m3"),
    ]})
    _drain(client)

    report = json.loads(next((tmp_path / "reports").glob("*.json")).read_text())
    assert [v["message_id"] for v in report["verdicts"]] == ["m3"]


def test_bad_client_state_is_dropped_without_fetching(tmp_path):
    session = FakeGraphSession()
    client = _client(tmp_path, session)

    response = client.post(
        "/graph", json={"value": [_notification("m1", client_state="wrong")]}
    )

    assert response.status_code == 202
    assert session.get_ids == []


def test_duplicate_ids_in_batch_fetched_once(tmp_path):
    session = FakeGraphSession(by_id={"m1": _raw_message("m1")})
    client = _client(tmp_path, session)

    client.post("/graph", json={"value": [_notification("m1"), _notification("m1")]})
    _drain(client)

    assert session.get_ids == ["m1"]


def test_fetch_error_is_retried_then_dropped_without_wedging_the_queue(tmp_path, caplog):
    """Past the ack there is no redelivery to ask for, so the worker retries — but
    a permanently failing run must not stay queued, or every later notification
    stacks behind it (STU-947)."""
    session = FakeGraphSession(error=RuntimeError("graph down"))
    client = _client(tmp_path, session, max_attempts=3)

    with caplog.at_level(logging.ERROR, logger="email_classifier.webhook"):
        response = client.post("/graph", json={"value": [_notification("m1")]})
        _drain(client)

    assert response.status_code == 202
    assert session.get_ids == ["m1", "m1", "m1"]
    assert (tmp_path / "state" / "notifications.jsonl").read_text() == ""
    assert "m1" in caplog.text
    assert "after 3 failed attempts" in caplog.text


def test_a_transient_failure_is_retried_until_it_succeeds(tmp_path):
    """A thirty-second upstream blip must not cost the mail the ack promised."""
    class Recovering(FakeGraphSession):
        def get(self, url, params=None, prefer=None):
            if len(self.get_ids) >= 2:
                self.error = None
            return super().get(url, params=params, prefer=prefer)

    session = Recovering(by_id={"m1": _raw_message("m1")}, error=RuntimeError("503"))
    stage2 = FakeClassifier()
    client = _client(tmp_path, session, stage2=stage2)

    client.post("/graph", json={"value": [_notification("m1")]})
    _drain(client)

    assert session.get_ids == ["m1", "m1", "m1"]
    assert stage2.seen == ["m1"]
    assert (tmp_path / "state" / "notifications.jsonl").read_text() == ""


def test_a_dropped_item_lets_the_next_one_run(tmp_path):
    """The bound exists so one poison item cannot hold the queue behind it."""

    class FailsOnlyOnM1(FakeGraphSession):
        def get(self, url, params=None, prefer=None):
            self.error = RuntimeError("graph down") if url.endswith("m1") else None
            return super().get(url, params=params, prefer=prefer)

    session = FailsOnlyOnM1(by_id={"m2": _raw_message("m2")})
    stage2 = FakeClassifier()
    client = _client(tmp_path, session, stage2=stage2, max_attempts=2)

    client.post("/graph", json={"value": [_notification("m1")]})
    client.post("/graph", json={"value": [_notification("m2")]})
    _drain(client)

    assert session.get_ids == ["m1", "m1", "m2"]
    assert stage2.seen == ["m2"]
    assert (tmp_path / "state" / "notifications.jsonl").read_text() == ""


def test_the_attempt_count_survives_a_restart(tmp_path):
    """Counted on the entry, so a run that takes the process down is not replayed
    forever by the restart."""
    queue_file = tmp_path / "state" / "notifications.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps({"provider": "outlook", "message_ids": ["m1"], "attempts": 2}) + "\n",
        encoding="utf-8",
    )
    session = FakeGraphSession(error=RuntimeError("graph down"))

    client = _client(tmp_path, session, max_attempts=3)
    _drain(client)

    assert session.get_ids == ["m1"]  # the third attempt, not a fresh bound
    assert queue_file.read_text() == ""


def test_notification_is_acked_before_the_run_finishes(tmp_path):
    """Graph redelivers a notification it has not been answered within seconds, so
    the ack cannot wait on a `studio` subprocess and a live LLM pass (STU-927)."""
    started, release = threading.Event(), threading.Event()

    class BlockingClassifier(FakeClassifier):
        def classify(self, email, thread_history=""):
            started.set()
            release.wait(10)
            return super().classify(email)

    session = FakeGraphSession(by_id={"m1": _raw_message("m1")})
    client = _client(tmp_path, session, stage2=BlockingClassifier())

    response = client.post("/graph", json={"value": [_notification("m1")]})

    assert response.status_code == 202
    assert started.wait(10)
    assert not (tmp_path / "reports").exists()  # acked with the run still in flight

    release.set()
    _drain(client)
    assert len(list((tmp_path / "reports").glob("*.json"))) == 1


def test_a_queued_notification_survives_a_restart(tmp_path):
    """A crash between the ack and the run replays the work: the provider was told
    the notification landed and will not send it again."""
    queue_file = tmp_path / "state" / "notifications.jsonl"
    queue_file.parent.mkdir(parents=True)
    queue_file.write_text(
        json.dumps({"provider": "outlook", "message_ids": ["m1"]}) + "\n", encoding="utf-8"
    )
    session = FakeGraphSession(by_id={"m1": _raw_message("m1")})

    client = _client(tmp_path, session)
    _drain(client)

    assert session.get_ids == ["m1"]


def test_gmail_route_absent_when_only_outlook_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBSUB_AUDIENCE", raising=False)
    client = _client(tmp_path, FakeGraphSession())

    assert client.post("/pubsub", json={}).status_code == 404


def test_both_routes_coexist(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBSUB_AUDIENCE", raising=False)
    from email_classifier.webhook import state
    from tests.test_webhook_server import _envelope, _service

    state.save_history_id(tmp_path / "state" / "gmail-history.json", "100")
    app = create_app(
        service=_service(history_pages=[{"historyId": "150"}]),
        stage2=FakeClassifier(),
        writer=FakeWriter(),
        verify_token=lambda token: None,
        graph_session=FakeGraphSession(),
        graph_client_state=CLIENT_STATE,
        graph_notification_url=NOTIFICATION_URL,
        rules_path=RULES_DIR / "rules.yaml",
        policy_path=served_policy(tmp_path),
        tokenizer_factory=lambda: Tokenizer(persons=False),
        **webhook_paths(tmp_path),
    )
    client = app.test_client()

    assert client.post("/pubsub", json=_envelope("150"),
                       headers={"Authorization": "Bearer x"}).status_code == 204
    assert client.post("/graph", json={"value": []}).status_code == 202


def test_no_provider_enabled_fails_loud(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBSUB_AUDIENCE", raising=False)
    monkeypatch.delenv("GRAPH_CLIENT_STATE", raising=False)

    with pytest.raises(RuntimeError, match="no provider enabled"):
        create_app(
            stage2=FakeClassifier(),
            rules_path=RULES_DIR / "rules.yaml",
            learned_path=tmp_path / "rules" / "learned.yaml",
            policy_path=served_policy(tmp_path),
        )


def test_missing_client_state_fails_loud(tmp_path, monkeypatch):
    monkeypatch.delenv("GRAPH_CLIENT_STATE", raising=False)

    with pytest.raises(RuntimeError, match="GRAPH_CLIENT_STATE"):
        create_app(
            stage2=FakeClassifier(),
            graph_session=FakeGraphSession(),
            rules_path=RULES_DIR / "rules.yaml",
            learned_path=tmp_path / "rules" / "learned.yaml",
            policy_path=served_policy(tmp_path),
        )


def _grant_lost_client(tmp_path, monkeypatch):
    """A webhook whose next Graph call finds the grant gone, with SIGTERM stubbed."""
    signals = []
    alerted = []
    monkeypatch.setattr(webhook_server.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(webhook_server.alerts, "notify", alerted.append)
    session = FakeGraphSession(error=GrantLost("the grant is gone. Run `make outlook-consent`."))
    return _client(tmp_path, session), signals, alerted


def _wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_lost_grant_alerts_and_stops_the_worker(tmp_path, monkeypatch):
    """STU-930: no notification can be served until a human re-consents, so the
    process ends instead of rediscovering the refusal on every one."""
    client, signals, alerted = _grant_lost_client(tmp_path, monkeypatch)

    response = client.post("/graph", json={"value": [_notification("m1")]})

    assert response.status_code == 202
    assert _wait_for(lambda: signals)
    assert signals == [signal.SIGTERM]
    assert len(alerted) == 1
    assert "make outlook-consent" in alerted[0]


def test_a_lost_grant_leaves_the_notification_queued(tmp_path, monkeypatch):
    """The ack promised no redelivery, so the item has to outlive the halt —
    it is the only record of the mail a restored grant still owes a run."""
    client, signals, _ = _grant_lost_client(tmp_path, monkeypatch)

    client.post("/graph", json={"value": [_notification("m1")]})

    assert _wait_for(lambda: signals)
    queue_file = tmp_path / "state" / "notifications.jsonl"
    assert "m1" in queue_file.read_text(encoding="utf-8")


def test_the_alert_says_mail_is_going_unclassified(tmp_path, monkeypatch):
    """The recovery command alone does not say what it costs to leave it undone."""
    client, signals, alerted = _grant_lost_client(tmp_path, monkeypatch)

    client.post("/graph", json={"value": [_notification("m1")]})

    assert _wait_for(lambda: signals)
    assert "not classified" in alerted[0]


def test_a_grant_already_gone_at_startup_alerts_and_exits(monkeypatch):
    """A reboot that cannot sign in is the same outage as one that dies later,
    and the process never reaches a request to report it from."""
    alerted = []
    monkeypatch.setattr(webhook_server.alerts, "notify", alerted.append)
    monkeypatch.setattr(
        webhook_server, "create_app",
        lambda: (_ for _ in ()).throw(GrantLost("gone. Run `make outlook-consent`.")),
    )

    assert webhook_server.main() == 1
    assert "make outlook-consent" in alerted[0]


def test_a_burst_inside_the_window_is_one_run(tmp_path):
    """Five messages announced together are one run, one report, one keymap — not
    five of each, with the same person tokenized differently in every one (STU-939)."""
    message_ids = [f"m{n}" for n in range(1, 6)]
    session = FakeGraphSession(by_id={mid: _raw_message(mid) for mid in message_ids})
    stage2 = FakeClassifier()
    client = _client(tmp_path, session, stage2=stage2, debounce_seconds=BURST_WINDOW)

    for message_id in message_ids:
        assert client.post("/graph", json={"value": [_notification(message_id)]}).status_code == 202
    _drain(client)

    reports = list((tmp_path / "reports").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert {verdict["message_id"] for verdict in report["verdicts"]} == set(message_ids)
    assert sorted(stage2.seen) == sorted(message_ids)


def test_the_window_does_not_merge_across_providers(tmp_path):
    """Apply is bound to the one provider `email.provider` names, and the queue is
    FIFO because its order carries meaning."""
    queued = [
        {"provider": "outlook", "message_ids": ["m1"]},
        {"provider": "outlook", "message_ids": ["m2"]},
        {"provider": "gmail", "history_id": "9"},
    ]

    work, covered = webhook_server._coalesce(queued)

    assert work == {"provider": "outlook", "message_ids": ["m1", "m2"]}
    assert covered == queued[:2]


def test_a_merged_gmail_batch_leaves_the_watermark_at_the_highest_id(tmp_path):
    """One `history.list` from the stored id already covers the burst; the merge only
    picks where to leave the watermark, and Pub/Sub does not promise order."""
    queued = [
        {"provider": "gmail", "history_id": "11"},
        {"provider": "gmail", "history_id": "9"},
    ]

    work, covered = webhook_server._coalesce(queued)

    assert work == {"provider": "gmail", "history_id": "11"}
    assert covered == queued


def test_lifecycle_events_never_merge(tmp_path):
    """Each names a different repair, so folding two into one would drop one."""
    queued = [
        {"provider": "outlook-lifecycle", "event": "missed", "subscription_id": "s1"},
        {"provider": "outlook-lifecycle", "event": "subscriptionRemoved",
         "subscription_id": "s1"},
    ]

    work, covered = webhook_server._coalesce(queued)

    assert work == queued[0]
    assert covered == queued[:1]


def test_a_message_that_rode_along_with_a_poison_one_is_still_classified(tmp_path):
    """A batch is one fetch, so an unreachable message must not take the mail queued
    beside it down with it (STU-947 under STU-939)."""

    class FailsOnlyOnM1(FakeGraphSession):
        def get(self, url, params=None, prefer=None):
            self.error = RuntimeError("graph down") if url.endswith("m1") else None
            return super().get(url, params=params, prefer=prefer)

    session = FailsOnlyOnM1(by_id={"m2": _raw_message("m2")})
    stage2 = FakeClassifier()
    client = _client(
        tmp_path, session, stage2=stage2, max_attempts=2, debounce_seconds=BURST_WINDOW,
    )

    client.post("/graph", json={"value": [_notification("m1")]})
    client.post("/graph", json={"value": [_notification("m2")]})
    _drain(client)

    assert stage2.seen == ["m2"]
    assert (tmp_path / "state" / "notifications.jsonl").read_text() == ""


def test_batching_resumes_after_a_failed_run(tmp_path):
    """The narrowing to one item answers a failure; it is not the shape the worker
    keeps. Left latched, the first failed run ends batching for the life of the
    process — every later burst is one run per message again, which is what STU-939
    exists to remove, and the only symptom is a report count nobody watches."""
    burst_ids = [f"m{n}" for n in range(1, 6)]

    class FailsOnlyOnPoison(FakeGraphSession):
        def get(self, url, params=None, prefer=None):
            self.error = RuntimeError("graph down") if url.endswith("/poison") else None
            return super().get(url, params=params, prefer=prefer)

    started, release = threading.Event(), threading.Event()

    class HoldsTheNarrowedPass(FakeClassifier):
        def classify(self, email, thread_history=""):
            if email.message_id == "recovered":
                started.set()
                release.wait(10)
            return super().classify(email)

    session = FailsOnlyOnPoison(
        by_id={mid: _raw_message(mid) for mid in ("recovered", *burst_ids)}
    )
    client = _client(tmp_path, session, stage2=HoldsTheNarrowedPass(), max_attempts=1)

    # A run that fails, giving up on the message it could not reach.
    client.post("/graph", json={"value": [_notification("poison")]})
    _drain(client)
    # Then the one narrowed pass the failure bought: a single message, alone.
    client.post("/graph", json={"value": [_notification("recovered")]})
    assert started.wait(10)

    # The burst is queued while that pass is held, so the next take sees all of it
    # however long the posts take: what decides the split is the latch, not a window
    # racing the posts (STU-2030).
    for message_id in burst_ids:
        assert client.post(
            "/graph", json={"value": [_notification(message_id)]}
        ).status_code == 202
    release.set()
    _drain(client)

    reports = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "reports").glob("*.json")
    ]
    of_the_burst = [
        report for report in reports
        if {verdict["message_id"] for verdict in report["verdicts"]} & set(burst_ids)
    ]
    assert len(of_the_burst) == 1
    assert {
        verdict["message_id"] for verdict in of_the_burst[0]["verdicts"]
    } == set(burst_ids)


def _in_the_clear(*texts):
    """Every format category that survived into what stage 2 was handed.

    Read off `_FORMAT_PATTERNS` itself rather than a copy: a category added to the
    tokenizer must widen this check, not slip past a list frozen beside it.
    """
    return sorted(
        category for text in texts
        for category, pattern in _FORMAT_PATTERNS.items() if pattern.search(text)
    )


def test_the_conversation_reaching_stage_2_is_tokenized(tmp_path):
    """The push path tokenizes what it sends; the engine's middleware never did (STU-1009).

    `pipeline.run` used to hand Studio the cleartext conversation and declare
    `anonymization.scope` for the middleware to honour — whose detector anchors a name
    on a salutation, the one place French mail never puts one (STU-890). So a push run
    sent the mailbox as fetched while its report claimed six tokenized fields.

    Asserted on the item, not on the report or the verdict store: those are restored to
    cleartext by design, so neither can show what the prompt received. Both halves of
    the conversation are checked — `thread_history` is the wider surface, and only the
    head travels as fields.
    """
    session = FakeGraphSession(by_id={
        "m1": _raw_message("m1", from_address="parent@exemple.ca", conversation="c1",
                           received="2026-08-18T09:00:00Z",
                           body="Bonjour, rappelez-moi au 514-555-0182."),
        "m2": _raw_message("m2", from_address="autre@exemple.ca", conversation="c1",
                           received="2026-08-18T10:00:00Z",
                           body="Merci. Ma carte est 4111 1111 1111 1111."),
    })
    stage2 = FakeClassifier()
    client = _client(tmp_path, session, stage2=stage2)

    client.post("/graph", json={"value": [_notification("m2")]})
    _drain(client)

    # One call for the conversation (STU-944): the most recent escalated message is the
    # head, and the notified message's sibling is its history.
    [head], [history] = stage2.inputs, stage2.histories
    assert _in_the_clear(
        head.from_address, head.subject, head.body_snippet, *head.headers.values(), history
    ) == []
    assert "EMAIL_" in head.from_address and "CREDIT_CARD_" in head.body_snippet
    assert "EMAIL_" in history and "PHONE_" in history


def test_the_run_keeps_the_cleartext_it_tokenized(tmp_path):
    """The tokens go to the LLM; everything this side of it reads in the clear.

    The keymap is what makes that reversible, and it is written per run as well as
    shared, so an audit can say what this run minted rather than what the machine has
    accumulated (STU-924).
    """
    session = FakeGraphSession(by_id={
        "m1": _raw_message("m1", from_address="parent@exemple.ca",
                           body="Rappelez-moi au 514-555-0182."),
    })
    client = _client(tmp_path, session, stage2=FakeClassifier())

    client.post("/graph", json={"value": [_notification("m1")]})
    _drain(client)

    assert load_observations(tmp_path / "logs" / "verdicts.jsonl")[0].from_address == (
        "parent@exemple.ca"
    )
    keymap = tmp_path / "logs" / "anonymization" / "keymap.json"
    minted = json.loads((keymap.parent / f"{_run_id(tmp_path)}.json").read_text())
    assert sorted(minted.values()) == ["514-555-0182", "parent@exemple.ca"]
    assert json.loads(keymap.read_text()) == minted


def _run_id(tmp_path):
    [report] = (tmp_path / "reports").glob("*.json")
    return report.stem
