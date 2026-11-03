"""The weekly Gmail watch cron (scripts/renew_watch.py).

Nobody is at a browser when cron runs it, so a dead grant must not reach the
consent flow: `run_local_server` would bind a random port and block forever,
which is indistinguishable from a cron run still working (STU-952). It alerts
instead, like the Graph twin (STU-930) — see tests/test_renew_graph_cli.py.
"""

import importlib.util
from pathlib import Path

import pytest

from email_classifier.fetch.grant import GrantLost

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def script():
    path = REPO_ROOT / "scripts" / "renew_watch.py"
    spec = importlib.util.spec_from_file_location("scripts_renew_watch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _argv(tmp_path):
    return ["--topic", "projects/p/topics/gmail-inbox",
            "--state-file", str(tmp_path / "state" / "gmail-history.json")]


class _FakeGmail:
    """The two calls `main` makes, plus the exception type it catches."""

    GrantLost = GrantLost

    def __init__(self, credentials_error=None, watch_error=None):
        self.credentials_error = credentials_error
        self.watch_error = watch_error
        self.interactive = None

    def get_credentials(self, interactive=True):
        self.interactive = interactive
        if self.credentials_error:
            raise self.credentials_error
        return object()

    def build_service(self, creds):
        return object()

    def watch(self, service, topic):
        if self.watch_error:
            raise self.watch_error
        return {"historyId": "4242", "expiration": "1787000000000"}


def test_the_grant_is_taken_without_a_consent_flow(script, monkeypatch, tmp_path):
    fake = _FakeGmail()
    monkeypatch.setattr(script, "gmail", fake)

    assert script.main(_argv(tmp_path)) == 0
    assert fake.interactive is False


def test_a_lost_grant_alerts_and_exits_nonzero(script, monkeypatch, tmp_path):
    alerted = []
    monkeypatch.setattr(script.alerts, "notify", alerted.append)
    monkeypatch.setattr(
        script, "gmail",
        _FakeGmail(credentials_error=GrantLost("gone. Run `make gmail-consent`.")),
    )

    assert script.main(_argv(tmp_path)) == 1
    assert len(alerted) == 1
    assert "make gmail-consent" in alerted[0]
    assert "seven days" in alerted[0]


def test_a_grant_refused_by_the_watch_call_alerts_too(script, monkeypatch, tmp_path):
    """A revoked token gets past `get_credentials`: the 401 lands on `watch` (STU-953)."""
    alerted = []
    monkeypatch.setattr(script.alerts, "notify", alerted.append)
    monkeypatch.setattr(
        script, "gmail",
        _FakeGmail(watch_error=GrantLost("401. Run `make gmail-consent`.")),
    )

    assert script.main(_argv(tmp_path)) == 1
    assert len(alerted) == 1


def test_a_renewed_watch_alerts_nothing_and_baselines_the_history_id(
    script, monkeypatch, tmp_path
):
    from email_classifier.webhook import state

    alerted = []
    monkeypatch.setattr(script.alerts, "notify", alerted.append)
    monkeypatch.setattr(script, "gmail", _FakeGmail())

    assert script.main(_argv(tmp_path)) == 0
    assert alerted == []
    assert state.load_history_id(tmp_path / "state" / "gmail-history.json") == "4242"
