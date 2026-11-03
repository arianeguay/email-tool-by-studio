"""The daily Graph subscription cron (scripts/renew_graph_subscription.py).

It is the job that meets a grant killed by 90 days of inactivity, and its exit
code goes nowhere a human reads — so a lost grant has to alert (STU-930).
"""

import importlib.util
from pathlib import Path

import pytest

from email_classifier.fetch.outlook.client import GrantLost

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def script(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_CLIENT_STATE", "s3cret")
    monkeypatch.setenv("GRAPH_NOTIFICATION_URL", "https://webhook.test/graph")
    return load_script("renew_graph_subscription")


def _argv(tmp_path):
    return ["--state-file", str(tmp_path / "state" / "outlook-subscription.json")]


def test_a_lost_grant_alerts_and_exits_nonzero(script, monkeypatch, tmp_path):
    alerted = []
    monkeypatch.setattr(script.alerts, "notify", alerted.append)
    monkeypatch.setattr(
        script, "_renew",
        lambda *a: (_ for _ in ()).throw(GrantLost("gone. Run `make outlook-consent`.")),
    )

    assert script.main(_argv(tmp_path)) == 1
    assert len(alerted) == 1
    assert "make outlook-consent" in alerted[0]
    assert "expires within three days" in alerted[0]


def test_any_other_failure_alerts_and_exits_nonzero(script, monkeypatch, tmp_path):
    alerted = []
    monkeypatch.setattr(script.alerts, "notify", alerted.append)
    monkeypatch.setattr(
        script, "_renew",
        lambda *a: (_ for _ in ()).throw(ConnectionError("graph unreachable")),
    )

    assert script.main(_argv(tmp_path)) == 1
    assert len(alerted) == 1
    assert "ConnectionError: graph unreachable" in alerted[0]
    assert "expires within three days" in alerted[0]


class _FakeOutlook:
    """The three subscription calls `_renew` can make, recorded in order."""

    def __init__(self, renewed):
        self.renewed = renewed
        self.calls = []

    def build_session(self, interactive=True):
        return object()

    def renew_subscription(self, session, subscription_id):
        self.calls.append(("renew", subscription_id))
        return self.renewed

    def delete_subscription(self, session, subscription_id):
        self.calls.append(("delete", subscription_id))

    def subscribe(self, session, notification_url, client_state):
        self.calls.append(("subscribe", notification_url))
        return {"id": "sub2", "expirationDateTime": "2026-08-25T00:00:00Z",
                "lifecycleNotificationUrl": f"{notification_url}/lifecycle"}


def test_a_subscription_without_a_lifecycle_url_is_replaced(script, monkeypatch, tmp_path):
    """Graph cannot PATCH one in, so the cron is where a pre-STU-937 subscription goes."""
    from email_classifier.webhook import state

    state_file = tmp_path / "state" / "outlook-subscription.json"
    state.record_subscription(state_file, {"id": "sub1", "expirationDateTime": "old"})
    fake = _FakeOutlook({"id": "sub1", "expirationDateTime": "2026-08-25T00:00:00Z"})
    monkeypatch.setattr(script, "outlook", fake)

    assert script.main(_argv(tmp_path)) == 0
    assert fake.calls == [
        ("renew", "sub1"),
        ("delete", "sub1"),
        ("subscribe", "https://webhook.test/graph"),
    ]
    assert state.load_subscription(state_file)["subscription_id"] == "sub2"


def test_a_subscription_with_a_lifecycle_url_is_only_renewed(script, monkeypatch, tmp_path):
    from email_classifier.webhook import state

    state_file = tmp_path / "state" / "outlook-subscription.json"
    state.record_subscription(state_file, {"id": "sub1", "expirationDateTime": "old"})
    fake = _FakeOutlook({
        "id": "sub1",
        "expirationDateTime": "2026-08-25T00:00:00Z",
        "lifecycleNotificationUrl": "https://webhook.test/graph/lifecycle",
    })
    monkeypatch.setattr(script, "outlook", fake)

    assert script.main(_argv(tmp_path)) == 0
    assert fake.calls == [("renew", "sub1")]


def test_recreate_deletes_then_subscribes(script, monkeypatch, tmp_path):
    """A rotated GRAPH_CLIENT_STATE reaches Graph no other way: PATCH ignores it."""
    from email_classifier.webhook import state

    state_file = tmp_path / "state" / "outlook-subscription.json"
    state.record_subscription(state_file, {"id": "sub1", "expirationDateTime": "old"})
    fake = _FakeOutlook({"id": "sub1", "expirationDateTime": "2026-08-25T00:00:00Z"})
    monkeypatch.setattr(script, "outlook", fake)

    assert script.main(_argv(tmp_path) + ["--recreate"]) == 0
    assert fake.calls == [("delete", "sub1"), ("subscribe", "https://webhook.test/graph")]
    assert state.load_subscription(state_file)["subscription_id"] == "sub2"


def test_a_renewed_subscription_alerts_nothing(script, monkeypatch, tmp_path):
    alerted = []
    monkeypatch.setattr(script.alerts, "notify", alerted.append)
    monkeypatch.setattr(
        script, "_renew",
        lambda *a: {"id": "sub1", "expirationDateTime": "2026-08-25T00:00:00Z"},
    )

    assert script.main(_argv(tmp_path)) == 0
    assert alerted == []
