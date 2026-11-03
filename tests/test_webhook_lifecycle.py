"""Webhook /graph/lifecycle: handshake, and recovery per lifecycle event (STU-937)."""

import json

import pytest
import yaml

pytest.importorskip("flask")

from email_classifier.webhook import server as webhook_server
from email_classifier.webhook import state
from tests.conftest import RULES_DIR
from tests.test_webhook_graph import (
    CLIENT_STATE,
    FakeGraphSession,
    _client,
    _drain,
    _notification,
    _raw_message,
)

NEW_SUBSCRIPTION = {"id": "sub-new", "expirationDateTime": "2026-08-25T00:00:00Z"}


class LifecycleSession(FakeGraphSession):
    """FakeGraphSession that also answers the subscription and catch-up calls."""

    def __init__(self, by_id=None, since_ids=None):
        super().__init__(by_id)
        self.since_ids = since_ids or []
        self.requests = []
        self.filters = []

    def get(self, url, params=None, prefer=None):
        if url.endswith("mailFolders('inbox')/messages"):
            self.filters.append(params["$filter"])
            return {"value": [{"id": message_id} for message_id in self.since_ids]}
        return super().get(url, params, prefer)

    def request(self, method, url, params=None, payload=None, prefer=None):
        self.requests.append((method, url))
        return NEW_SUBSCRIPTION


def _lifecycle(event, client_state=CLIENT_STATE, subscription_id="sub1"):
    return {"value": [{
        "clientState": client_state,
        "lifecycleEvent": event,
        "subscriptionId": subscription_id,
    }]}


def _subscription_file(tmp_path):
    return tmp_path / "state" / "outlook-subscription.json"


def _record(tmp_path, **fields):
    path = _subscription_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields), encoding="utf-8")


def test_lifecycle_handshake_echoes_token(tmp_path):
    client = _client(tmp_path, LifecycleSession())

    response = client.post("/graph/lifecycle?validationToken=abc%20123")

    assert response.status_code == 200
    assert response.text == "abc 123"


def test_lifecycle_handshake_refuses_to_echo_an_unbounded_token(tmp_path):
    client = _client(tmp_path, LifecycleSession())

    response = client.post(
        "/graph/lifecycle", query_string={"validationToken": "<script>alert(1)</script>"}
    )

    assert response.status_code == 400


def test_reauthorization_required_reauthorizes_then_renews(tmp_path):
    session = LifecycleSession()
    _record(tmp_path, subscription_id="sub1", expires_at="2026-08-22T00:00:00Z")
    client = _client(tmp_path, session)

    assert client.post("/graph/lifecycle", json=_lifecycle("reauthorizationRequired")).status_code == 202
    _drain(client)

    assert session.requests == [
        ("POST", "/subscriptions/sub1/reauthorize"),
        ("PATCH", "/subscriptions/sub1"),
    ]
    assert state.load_subscription(_subscription_file(tmp_path))["expires_at"] == (
        NEW_SUBSCRIPTION["expirationDateTime"]
    )


def test_subscription_removed_recreates_and_records_the_new_id(tmp_path):
    session = LifecycleSession()
    _record(tmp_path, subscription_id="sub1")
    client = _client(tmp_path, session)

    client.post("/graph/lifecycle", json=_lifecycle("subscriptionRemoved"))
    _drain(client)

    assert session.requests == [("POST", "/subscriptions")]
    assert state.load_subscription(_subscription_file(tmp_path))["subscription_id"] == "sub-new"


def test_missed_classifies_everything_since_the_watermark(tmp_path):
    session = LifecycleSession(by_id={"m1": _raw_message("m1")}, since_ids=["m1"])
    _record(tmp_path, subscription_id="sub1", watermark="2026-08-17T00:00:00+00:00")
    client = _client(tmp_path, session)

    client.post("/graph/lifecycle", json=_lifecycle("missed"))
    _drain(client)

    assert session.filters == ["receivedDateTime gt 2026-08-17T00:00:00Z"]
    assert session.get_ids == ["m1"]
    reports = list((tmp_path / "reports").glob("*.json"))
    assert len(reports) == 1
    assert [v["message_id"] for v in json.loads(reports[0].read_text())["verdicts"]] == ["m1"]


def test_a_caught_up_batch_moves_the_watermark(tmp_path):
    session = LifecycleSession(by_id={"m1": _raw_message("m1")}, since_ids=["m1"])
    _record(tmp_path, subscription_id="sub1", watermark="2026-08-17T00:00:00+00:00")
    client = _client(tmp_path, session)

    client.post("/graph/lifecycle", json=_lifecycle("missed"))
    _drain(client)

    assert state.load_subscription(_subscription_file(tmp_path))["watermark"] == (
        "2026-08-18T00:00:00+00:00"
    )


def test_missed_without_a_watermark_fetches_nothing(tmp_path, caplog):
    """Nothing to resume from is reported, not guessed at from an empty gap."""
    session = LifecycleSession()
    client = _client(tmp_path, session)

    client.post("/graph/lifecycle", json=_lifecycle("missed"))
    _drain(client)

    assert session.filters == []
    assert "no watermark" in caplog.text


def test_a_delivered_notification_moves_the_watermark(tmp_path):
    session = LifecycleSession(by_id={"m1": _raw_message("m1")})
    client = _client(tmp_path, session)

    client.post("/graph", json={"value": [_notification("m1")]})
    _drain(client)

    assert state.load_subscription(_subscription_file(tmp_path))["watermark"] == (
        "2026-08-18T00:00:00+00:00"
    )


def test_a_newer_thread_sibling_does_not_move_the_watermark(tmp_path):
    """A conversation can hold a message newer than the one Graph just announced — a
    notification we never got. Letting the expansion date the watermark would put that
    message below the resume point of the catch-up that exists to find it (STU-963)."""
    session = LifecycleSession(by_id={
        "m1": _raw_message("m1", conversation="t-conv"),
        "m2": _raw_message(
            "m2", conversation="t-conv", received="2026-08-20T00:00:00Z"
        ),
    })
    client = _client(tmp_path, session)

    client.post("/graph", json={"value": [_notification("m1")]})
    _drain(client)

    assert state.load_subscription(_subscription_file(tmp_path))["watermark"] == (
        "2026-08-18T00:00:00+00:00"
    )


def test_bad_client_state_lifecycle_is_dropped(tmp_path):
    session = LifecycleSession()
    client = _client(tmp_path, session)

    response = client.post("/graph/lifecycle", json=_lifecycle("subscriptionRemoved", "wrong"))
    _drain(client)

    assert response.status_code == 202
    assert session.requests == []


def test_an_unknown_lifecycle_event_is_not_acted_on(tmp_path, caplog):
    session = LifecycleSession()
    client = _client(tmp_path, session)

    client.post("/graph/lifecycle", json=_lifecycle("somethingNew"))
    _drain(client)

    assert session.requests == []
    assert "unknown Graph lifecycle event" in caplog.text


def test_a_lifecycle_event_does_not_count_as_liveness(tmp_path):
    """It proves Graph can reach us, not that mail is being delivered."""
    client = _client(tmp_path, LifecycleSession())

    client.post("/graph/lifecycle", json=_lifecycle("reauthorizationRequired"))
    _drain(client)

    assert not (tmp_path / "state" / "last-notification.json").exists()


def _already_classified(tmp_path, *message_ids):
    """Seed the verdict store with what an earlier run escalated (STU-954)."""
    path = tmp_path / "logs" / "verdicts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps({
                "run_id": "earlier",
                "timestamp": "2026-08-18T00:00:00+00:00",
                "message_id": message_id,
                "from_address": "stranger@unknown-domain.test",
                "bucket": "question",
                "suggested_action": "needs_reply",
                "confidence": "high",
            }) + "\n"
            for message_id in message_ids
        ),
        encoding="utf-8",
    )


def _catch_up_over(tmp_path, monkeypatch, message_ids, conversation=None):
    session = LifecycleSession(
        by_id={
            message_id: _raw_message(message_id, conversation=conversation)
            for message_id in message_ids
        },
        since_ids=list(message_ids),
    )
    _record(tmp_path, subscription_id="sub1", watermark="2026-08-17T00:00:00+00:00")
    alerted = []
    monkeypatch.setattr(webhook_server.alerts, "notify", alerted.append)
    client = _client(tmp_path, session)

    client.post("/graph/lifecycle", json=_lifecycle("missed"))
    _drain(client)
    return alerted


def _policy_from(tmp_path, start_date):
    body = yaml.safe_load((RULES_DIR / "policy.yaml").read_text(encoding="utf-8"))
    body["start_date"] = start_date
    path = tmp_path / "rules" / "floor-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return path


def _classified_ids(tmp_path):
    return [
        verdict["message_id"]
        for report in (tmp_path / "reports").glob("*.json")
        for verdict in json.loads(report.read_text())["verdicts"]
    ]


def test_a_catch_up_skips_what_the_verdict_store_already_ruled_on(tmp_path, monkeypatch):
    """A manual `make run` never moves the watermark, so its mail sits in the gap."""
    _already_classified(tmp_path, "m1")

    _catch_up_over(tmp_path, monkeypatch, ["m1", "m2"])

    assert _classified_ids(tmp_path) == ["m2"]


def test_a_catch_up_alerts_with_what_it_is_about_to_spend(tmp_path, monkeypatch):
    _already_classified(tmp_path, "m1")

    alerted = _catch_up_over(tmp_path, monkeypatch, ["m1", "m2"])

    assert len(alerted) == 1
    assert "classifying 1 message(s)" in alerted[0]
    assert "1 already in the verdict store" in alerted[0]


def test_a_catch_up_counts_a_resurfaced_sibling_as_paid_for(tmp_path, monkeypatch):
    """A store-known message the expansion pulls back in is spending, not a saving."""
    _already_classified(tmp_path, "m1")

    alerted = _catch_up_over(
        tmp_path, monkeypatch, ["m1", "m2"], conversation="t-shared"
    )

    assert "classifying 2 message(s)" in alerted[0]
    assert "1 thread sibling(s)" in alerted[0]
    assert "0 already in the verdict store" in alerted[0]
    assert sorted(_classified_ids(tmp_path)) == ["m1", "m2"]


def test_a_catch_up_names_what_the_date_floor_put_out_of_scope(tmp_path, monkeypatch):
    """A gap reaching below the floor is mail set aside, not mail missed — and the
    sibling count is against what the floor left, not against the raw gap, which read
    negative at exactly the moment mail was being dropped (STU-1123)."""
    session = LifecycleSession(
        by_id={
            "old": _raw_message("old", received="2026-08-10T00:00:00Z"),
            "m2": _raw_message("m2"),
        },
        since_ids=["old", "m2"],
    )
    _record(tmp_path, subscription_id="sub1", watermark="2026-08-01T00:00:00+00:00")
    alerted = []
    monkeypatch.setattr(webhook_server.alerts, "notify", alerted.append)
    client = _client(tmp_path, session, policy_path=_policy_from(tmp_path, "2026-08-15"))

    client.post("/graph/lifecycle", json=_lifecycle("missed"))
    _drain(client)

    assert "classifying 1 message(s)" in alerted[0]
    assert "0 thread sibling(s)" in alerted[0]
    assert "1 out of scope before 2026-08-15" in alerted[0]
    assert _classified_ids(tmp_path) == ["m2"]


def test_a_fully_deduped_catch_up_neither_alerts_nor_runs(tmp_path, monkeypatch, caplog):
    """Nothing is being paid for, so there is nothing to raise an operator for."""
    _already_classified(tmp_path, "m1")

    alerted = _catch_up_over(tmp_path, monkeypatch, ["m1"])

    assert alerted == []
    assert not list((tmp_path / "reports").glob("*.json"))
    assert "already classified" in caplog.text


def test_a_deduped_message_still_moves_the_watermark(tmp_path, monkeypatch):
    """Leaving the watermark behind it would re-fetch the gap on every catch-up."""
    _already_classified(tmp_path, "m1")

    _catch_up_over(tmp_path, monkeypatch, ["m1"])

    assert state.load_subscription(_subscription_file(tmp_path))["watermark"] == (
        "2026-08-18T00:00:00+00:00"
    )
