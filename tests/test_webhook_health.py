"""/healthz liveness reporting and the cron check that alerts on it (STU-931)."""

import json
import os
import pwd
import subprocess
import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("flask")
pytest.importorskip("waitress")

from email_classifier import audit_expiry
from email_classifier.anonymize import Tokenizer
from email_classifier.webhook import address, credential, deploy, tunnel
from email_classifier.webhook.server import create_app
from scripts import check_webhook_health
from tests.conftest import BURST_WINDOW, RULES_DIR, FakeWriter, webhook_paths, served_policy
from tests.test_orchestrate_pipeline import FakeClassifier
from tests.test_webhook_graph import (
    CLIENT_STATE,
    NOTIFICATION_URL,
    FakeGraphSession,
    _drain,
    _notification,
    _raw_message,
)
from tests.test_webhook_tunnel import FakeSsh

# Captured before `_audit_expiry_unasked_by_default` replaces it: the fixture patches
# the attribute on the module itself, so a test opting back in cannot read the real
# function off it afterwards.
_MEASURE_AUDIT_WINDOW = audit_expiry.measure


@pytest.fixture(autouse=True)
def _drift_unmeasured_by_default(monkeypatch):
    """Keep the drift measurement out of every test that is not about it.

    `deploy.measure` fetches from `origin`, and a suite that reaches the network
    once reaches it on the machine that has none. Tests that are about drift stub
    a verdict with `_serve_drift`; the ones below exercise the real git mechanics
    against a repo they build themselves.

    The stub is a *measured* verdict rather than an unmeasured one, which the
    unverified streak (STU-1126) is what made matter: a leg that answers nothing is
    a fault now, so stubbing one would have every test that runs the check three
    times exit 8 for a reason it is not about.
    """
    monkeypatch.setattr(
        check_webhook_health, "deploy",
        SimpleNamespace(measure=lambda *a, **k: deploy.Drift("a" * 40, "a" * 40, 0)),
    )


@pytest.fixture(autouse=True)
def _audit_expiry_unasked_by_default(monkeypatch):
    """Keep the host's own audit backlog out of every test that is not about it.

    `audit_expiry.measure()` reads `reports/` and `audits/audit_log.json` in whatever
    checkout the suite runs from, so on the machine that files entries the check would
    exit 10 for a reason the test is not about — the ambient-state failure STU-1059
    closed for `.env`. None is what a tree with no reports answers anyway.
    """
    monkeypatch.setattr(check_webhook_health.audit_expiry, "measure", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _quiet_reporting(tmp_path, monkeypatch):
    """Keep the check's own reporting out of the repo and off the phone.

    Both are real side effects on the machine running the suite: the journal is a
    file under `state/`, and `notify` reaches whatever channel the host's env
    configures — a suite that alerts is a suite nobody can run twice.
    """
    monkeypatch.setattr(
        check_webhook_health, "JOURNAL", tmp_path / "state" / "health-checks.jsonl"
    )
    monkeypatch.setattr(check_webhook_health.alerts, "notify", _alerted.append)
    _alerted.clear()


@pytest.fixture(autouse=True)
def _an_address_to_ask(monkeypatch):
    """Give every test a port, since the check refuses without one (STU-1087).

    The sibling in `tests/conftest.py` clears the two variables that would make
    the suite dial the operator's own webhook; this one fills the third for the
    opposite reason. `WEBHOOK_PORT` reaches a Python process only through the
    Makefile's `-include .env`, so it is set under `make test` and absent in CI —
    and since STU-1087 that difference is the difference between a check that runs
    and a check that refuses. Pinned here, a test that is about the address says so
    itself; the ones that are not get one and never depend on which machine ran them.
    """
    monkeypatch.setenv("WEBHOOK_PORT", "18080")


_alerted: list[str] = []


def _journal_entries():
    lines = check_webhook_health.JOURNAL.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _client(tmp_path, *args, **kwargs):
    return _app(tmp_path, *args, **kwargs).test_client()


def _app(tmp_path, session=None, stale_after_seconds=None, debounce_seconds=0):
    return create_app(
        stage2=FakeClassifier(),
        writer=FakeWriter(),
        graph_session=session or FakeGraphSession(),
        graph_client_state=CLIENT_STATE,
        graph_notification_url=NOTIFICATION_URL,
        rules_path=RULES_DIR / "rules.yaml",
        policy_path=served_policy(tmp_path),
        tokenizer_factory=lambda: Tokenizer(persons=False),
        **webhook_paths(tmp_path),
        stale_after_seconds=stale_after_seconds,
        retry_backoff_seconds=0,
        debounce_seconds=debounce_seconds,
    )


def _write_last_notification(tmp_path, age_seconds):
    path = tmp_path / "state" / "last-notification.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    path.write_text(json.dumps({"at": stamp.isoformat()}), encoding="utf-8")


def _write_subscription(tmp_path, expires_at="2026-08-25T00:00:00+00:00"):
    path = tmp_path / "state" / "outlook-subscription.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"subscription_id": "sub1", "expires_at": expires_at}), encoding="utf-8"
    )


def test_never_notified_measures_the_age_from_process_start(tmp_path):
    health = _client(tmp_path).get("/healthz").get_json()

    assert health["status"] == "ok"
    assert health["last_notification_at"] is None
    assert health["last_notification_age_seconds"] == pytest.approx(health["uptime_seconds"], abs=1)


def test_a_notification_stamps_the_liveness_file(tmp_path):
    session = FakeGraphSession(by_id={"m1": _raw_message("m1")})
    client = _client(tmp_path, session)

    client.post("/graph", json={"value": [_notification("m1")]})

    health = client.get("/healthz").get_json()
    assert health["last_notification_age_seconds"] < 5
    assert datetime.fromisoformat(health["last_notification_at"]).tzinfo is not None


def test_a_bad_client_state_does_not_count_as_liveness(tmp_path):
    client = _client(tmp_path)

    client.post("/graph", json={"value": [_notification("m1", client_state="wrong")]})

    assert client.get("/healthz").get_json()["last_notification_at"] is None


def test_the_validation_handshake_does_not_count_as_liveness(tmp_path):
    client = _client(tmp_path)

    client.post("/graph?validationToken=abc")

    assert client.get("/healthz").get_json()["last_notification_at"] is None


def test_an_age_past_the_threshold_reads_stale(tmp_path):
    _write_last_notification(tmp_path, age_seconds=7200)

    health = _client(tmp_path, stale_after_seconds=3600).get("/healthz").get_json()

    assert health["status"] == "stale"
    assert health["last_notification_age_seconds"] == pytest.approx(7200, abs=5)
    # The process is alive and answering; only the subscription is suspect.
    assert _client(tmp_path, stale_after_seconds=3600).get("/healthz").status_code == 200


def test_the_recorded_subscription_is_reported(tmp_path):
    _write_subscription(tmp_path)

    subscription = _client(tmp_path).get("/healthz").get_json()["subscription"]

    assert subscription == {"subscription_id": "sub1", "expires_at": "2026-08-25T00:00:00+00:00"}


def test_no_subscription_file_reports_none(tmp_path):
    assert _client(tmp_path).get("/healthz").get_json()["subscription"] is None


def test_the_counters_start_at_zero(tmp_path):
    health = _client(tmp_path).get("/healthz").get_json()

    assert health["notifications_acked"] == 0
    assert health["runs_completed"] == 0


def test_every_notification_is_counted_with_the_run_it_produced(tmp_path):
    session = FakeGraphSession(by_id={
        message_id: _raw_message(message_id) for message_id in ("m1", "m2", "m3")
    })
    client = _client(tmp_path, session)

    for message_id in ("m1", "m2", "m3"):
        client.post("/graph", json={"value": [_notification(message_id)]})
        # Drained between posts on purpose: the counters are one-to-one only when
        # each notification is alone in its batch. A burst is deliberately fewer
        # runs than acks (STU-939), which is what the next test pins.
        _drain(client)

    health = client.get("/healthz").get_json()
    assert health["notifications_acked"] == 3
    assert health["runs_completed"] == 3


def test_a_batched_burst_counts_every_ack_but_one_run(tmp_path):
    """`notifications_acked` answers "is Graph reaching us", `runs_completed`
    "is the mail being classified" — batching is exactly where they diverge."""
    session = FakeGraphSession(by_id={
        message_id: _raw_message(message_id) for message_id in ("m1", "m2", "m3")
    })
    client = _client(tmp_path, session, debounce_seconds=BURST_WINDOW)

    for message_id in ("m1", "m2", "m3"):
        client.post("/graph", json={"value": [_notification(message_id)]})
    _drain(client)

    health = client.get("/healthz").get_json()
    assert health["notifications_acked"] == 3
    assert health["runs_completed"] == 1


def test_an_unauthenticated_notification_counts_as_neither(tmp_path):
    client = _client(tmp_path)

    client.post("/graph", json={"value": [_notification("m1", client_state="wrong")]})
    _drain(client)

    health = client.get("/healthz").get_json()
    assert health["notifications_acked"] == 0
    assert health["runs_completed"] == 0


def test_both_counters_survive_a_restart(tmp_path):
    session = FakeGraphSession(by_id={"m1": _raw_message("m1")})
    client = _client(tmp_path, session)
    client.post("/graph", json={"value": [_notification("m1")]})
    _drain(client)

    # A second server object over the same state dir is what a restart looks like.
    health = _client(tmp_path, session).get("/healthz").get_json()

    assert health["notifications_acked"] == 1
    assert health["runs_completed"] == 1


class _FakeResponse:
    def __init__(self, health):
        self._body = json.dumps(health).encode()

    def read(self, *_args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _serve(monkeypatch, health):
    monkeypatch.setattr(
        check_webhook_health.urllib.request, "urlopen",
        lambda url, timeout=None: _FakeResponse(health),
    )


_OK = {
    "status": "ok",
    "last_notification_age_seconds": 60.0,
    "stale_after_seconds": 86400.0,
    "subscription": {"subscription_id": "sub1", "expires_at": "2026-08-25T00:00:00+00:00"},
}


def test_check_exits_zero_when_fresh(monkeypatch):
    _serve(monkeypatch, _OK)

    assert check_webhook_health.main([]) == 0


def test_check_alerts_when_stale(monkeypatch, capsys):
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})

    assert check_webhook_health.main([]) == 1
    assert "[ALERT] no notification in 25.0h" in capsys.readouterr().err


def test_check_alerts_when_the_endpoint_is_unreachable(monkeypatch, capsys):
    def refuse(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", refuse)

    assert check_webhook_health.main([]) == 2
    assert "[ALERT] webhook unreachable" in capsys.readouterr().err


# --- `--wait`, for the caller that just restarted the unit --------------------
#
# `make pull-webhook` restarts a Type=simple unit, so systemd returns as soon as the
# process forks — seconds before it binds. The check ran there against a closed socket
# and called the deploy dead.


def _fake_clock(monkeypatch):
    """A monotonic clock that only `sleep` advances, so no test waits in real time."""
    now = [0.0]
    monkeypatch.setattr(check_webhook_health.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        check_webhook_health.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds)
    )


def test_wait_retries_a_socket_that_is_still_coming_up(monkeypatch):
    _fake_clock(monkeypatch)
    attempts = []

    def refuse_then_serve(url, timeout=None):
        attempts.append(url)
        if len(attempts) < 3:
            raise OSError("connection refused")
        return _FakeResponse(_OK)

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", refuse_then_serve)

    assert check_webhook_health.main(["--wait", "60"]) == 0
    assert len(attempts) == 3


def test_wait_still_alerts_on_a_socket_that_never_comes_up(monkeypatch, capsys):
    _fake_clock(monkeypatch)

    def refuse(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", refuse)

    assert check_webhook_health.main(["--wait", "60"]) == 2
    assert "[ALERT] webhook unreachable" in capsys.readouterr().err


def test_a_forward_that_will_not_open_is_not_retried(monkeypatch, capsys):
    """Waiting is for a socket that is coming up; ssh refusing is not that."""
    _fake_clock(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://127.0.0.1:18080/healthz")
    monkeypatch.setattr(
        check_webhook_health.tunnel, "reachable",
        lambda url: _raise(check_webhook_health.tunnel.ForwardFailed("ssh exited 255")),
    )

    assert check_webhook_health.main(["--wait", "60"]) == 4


def _raise(exc):
    raise exc


# --- The ssh forward (STU-1037) ----------------------------------------------
#
# `make check-webhook` from a laptop dialed its own loopback and exited 2 — the
# code reserved for a dead webhook — while the serving host answered the same check with 0.


def _forwarding(monkeypatch, **ssh):
    """A webhook reachable only through the serving host, and an ssh that may not oblige.

    Returns what `urlopen` was handed, which is the whole question: the raw URL
    means the forward was skipped, a loopback port means it was used.
    """
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://127.0.0.1:18080/healthz")
    monkeypatch.setattr(
        tunnel.subprocess, "Popen",
        lambda argv, **_: FakeSsh(argv, **ssh),
    )
    polled = {}

    def record(url, timeout=None):
        polled["url"] = url
        return _FakeResponse(_OK)

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", record)
    return polled


def test_the_check_reads_the_webhook_through_the_forward(monkeypatch):
    """Not the address as this machine sees it — nothing listens there."""
    polled = _forwarding(monkeypatch)

    assert check_webhook_health.main([]) == 0
    assert polled["url"].startswith("http://127.0.0.1:")
    assert polled["url"] != "http://127.0.0.1:18080/healthz"
    assert polled["url"].endswith("/healthz")


def test_a_forward_that_will_not_open_is_not_a_dead_webhook(monkeypatch, capsys):
    """2 has to keep meaning the webhook is dead. Here it was never asked."""
    _forwarding(monkeypatch, listen=False, exit_code=255,
                stderr_text="serving-host: Name or service not known\n")

    assert check_webhook_health.main([]) == 4

    err = capsys.readouterr().err
    assert "no forward to http://127.0.0.1:18080/healthz through serving-host" in err
    assert "Name or service not known" in err
    assert "webhook unreachable" not in err


def test_a_webhook_that_is_down_behind_an_open_forward_still_exits_two(monkeypatch, capsys):
    """The forward carries the refusal: ssh accepts locally, the remote does not."""
    _forwarding(monkeypatch)

    def refuse(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", refuse)

    assert check_webhook_health.main([]) == 2
    assert "[ALERT] webhook unreachable" in capsys.readouterr().err


def test_the_cron_on_the_serving_host_opens_nothing(monkeypatch):
    """It names no ssh host and dials its own loopback, exactly as before."""
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://127.0.0.1:18080/healthz")
    monkeypatch.setattr(
        tunnel.subprocess, "Popen",
        lambda *a, **k: pytest.fail("no ssh may be spawned without WEBHOOK_SSH_HOST"),
    )
    polled = {}

    def record(url, timeout=None):
        polled["url"] = url
        return _FakeResponse(_OK)

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", record)

    assert check_webhook_health.main([]) == 0
    assert polled["url"] == "http://127.0.0.1:18080/healthz"


def test_check_survives_a_subscription_file_predating_the_expiry_field(monkeypatch, capsys):
    _serve(monkeypatch, {**_OK, "subscription": {"subscription_id": "sub1"}})

    assert check_webhook_health.main([]) == 0
    assert "expires unknown" in capsys.readouterr().out


def test_the_default_url_names_the_port_the_webhook_binds(monkeypatch):
    """A pinned 8080 polls nothing on a host that already spent it (STU-978)."""
    monkeypatch.setenv("WEBHOOK_PORT", "18080")
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)

    assert address.health_url() == "http://127.0.0.1:18080/healthz"


def test_a_wildcard_bind_is_polled_over_loopback(monkeypatch):
    """0.0.0.0 is a fine thing to listen on and not an address to ask."""
    monkeypatch.setenv("WEBHOOK_HOST", "0.0.0.0")
    monkeypatch.setenv("WEBHOOK_PORT", "18080")

    assert address.health_url() == "http://127.0.0.1:18080/healthz"


def test_a_single_interface_bind_is_the_only_address_that_answers(monkeypatch):
    """Binding one interface leaves loopback silent, so the check must ask it."""
    monkeypatch.setenv("WEBHOOK_HOST", "192.168.1.9")
    monkeypatch.setenv("WEBHOOK_PORT", "18080")

    assert address.health_url() == "http://192.168.1.9:18080/healthz"


def test_an_ipv6_bind_is_bracketed(monkeypatch):
    """Unbracketed, its colons read as the port separator."""
    monkeypatch.setenv("WEBHOOK_HOST", "::1")
    monkeypatch.setenv("WEBHOOK_PORT", "18080")

    assert address.health_url() == "http://[::1]:18080/healthz"


def test_an_explicit_health_url_still_wins(monkeypatch):
    """The check may run from another machine, where the bind says nothing."""
    monkeypatch.setenv("WEBHOOK_PORT", "18080")
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://serving-host.lan:9999/healthz")
    polled = {}

    def record(url, timeout=None):
        polled["url"] = url
        return _FakeResponse(_OK)

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", record)

    assert check_webhook_health.main([]) == 0
    assert polled["url"] == "http://serving-host.lan:9999/healthz"


def test_the_check_reaches_a_webhook_on_a_moved_port(tmp_path, monkeypatch):
    """The whole point, end to end: the port alone, over a real socket."""
    import waitress

    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)
    server = waitress.create_server(_app(tmp_path), host="127.0.0.1", port=0)
    monkeypatch.setenv("WEBHOOK_PORT", str(server.effective_port))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        assert check_webhook_health.main([]) == 0
    finally:
        server.close()
        thread.join(timeout=5)


# --- Refusing to guess the port (STU-1087) -----------------------------------
#
# Measured on the serving host 2026-08-29: `{"exit": 2, "summary": "webhook unreachable at
# http://127.0.0.1:8080/healthz: HTTP Error 404: Not Found"}`, while journald said
# `waitress: Serving on http://127.0.0.1:18080` and the run 16 seconds later got a
# full payload. 8080 there is a docker-proxy, so the guess was answered — by
# someone else — and the answer was reported as the webhook's absence.


def test_an_unnamed_address_is_refused_rather_than_guessed(monkeypatch):
    """No port and no URL is nothing to ask, and 8080 is not a fact about this host."""
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)

    with pytest.raises(RuntimeError, match="Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT"):
        address.configured_health_url()


def test_an_empty_port_is_as_absent_as_a_missing_one(monkeypatch):
    """`.env` carries keys empty and Make exports them set; `int("")` would only crash."""
    monkeypatch.setenv("WEBHOOK_PORT", "")
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)

    with pytest.raises(RuntimeError, match="Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT"):
        address.configured_health_url()


def test_an_override_alone_needs_no_port(monkeypatch):
    """The check may run where no webhook is started at all — a laptop."""
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://serving-host.lan:18080/healthz")

    assert address.configured_health_url() == "http://serving-host.lan:18080/healthz"


def test_the_bind_keeps_its_default_where_the_ask_does_not(monkeypatch):
    """The two sides are not symmetric: a bind fails loudly on EADDRINUSE."""
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)

    assert address.listen_port() == address.DEFAULT_PORT


def test_the_refusal_has_a_code_of_its_own_and_polls_nothing(monkeypatch, capsys):
    """2 has to keep meaning the webhook is dead; here it was never asked."""
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)
    monkeypatch.setattr(
        check_webhook_health.urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("nothing named an address, so nothing may be polled"),
    )

    assert check_webhook_health.main([]) == check_webhook_health.NOT_CONFIGURED

    err = capsys.readouterr().err
    assert "Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT" in err
    # The refusal names the misreading it exists to prevent, so match the verdict
    # line itself: `webhook unreachable at <url>` is what 2 prints.
    assert "webhook unreachable at " not in err
    assert _journal_entries()[-1]["exit"] == check_webhook_health.NOT_CONFIGURED
    assert _alerted and "WEBHOOK_PORT" in _alerted[0]


# --- Which way the environment came up empty (STU-1122) ----------------------
#
# Make's `-include .env` cannot tell a missing file from one this account may not
# open: both arrive as nothing. Measured on the serving host 2026-08-30, after STU-1116 put
# `/etc/email-classifier` at `700 email-classifier`: `make check-webhook` as the
# operator printed STU-1087's refusal while `WEBHOOK_PORT=18080` sat in the file
# `.env` links to. The repair that message names is the wrong one -- it sends an
# operator to export a port by hand, plausibly the 8080 it puts in front of them,
# which on that host is the docker-proxy STU-1087 was written against.
#
# Under the same exit code, deliberately: see `configured_health_url`.

_as_root = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root reads a 000 file, so the condition cannot be staged",
)


@pytest.fixture
def unreadable_env(tmp_path, monkeypatch):
    """A `.env` that is there, carries the port, and cannot be opened."""
    path = tmp_path / ".env"
    path.write_text("WEBHOOK_PORT=18080\n", encoding="utf-8")
    path.chmod(0o000)
    monkeypatch.setattr(address, "ENV_FILE", path)
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)
    return path


@_as_root
def test_an_unreadable_env_is_not_reported_as_an_unset_port(unreadable_env):
    """The variable is set; this process cannot see it. Two conditions, two messages."""
    with pytest.raises(RuntimeError) as refusal:
        address.configured_health_url()

    message = str(refusal.value)
    assert str(unreadable_env) in message
    assert "Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT is set" not in message


@_as_root
def test_the_refusal_names_the_account_and_running_under_it_as_the_repair(unreadable_env):
    """The repair has to be the one that works: `sudo -u <owner> make check-webhook`.

    Exporting a port into the operator's own shell is what STU-1087's message sends
    them to do, and on the serving host it is a guess about a port they have not read.
    """
    owner = pwd.getpwuid(unreadable_env.stat().st_uid).pw_name
    with pytest.raises(RuntimeError, match=f"sudo -u {owner} make check-webhook"):
        address.configured_health_url()


@_as_root
def test_a_link_into_a_directory_this_account_may_not_enter_names_both_ends(
    tmp_path, monkeypatch
):
    """The shape on the serving host: `.env` -> /etc/email-classifier/env, the directory at 700.

    `exists()` answers "no such file" here -- following the link needs the directory
    -- so the file that plainly is there would read as absent. The owner is the
    directory's, which is what is actually denying the read.
    """
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "env").write_text("WEBHOOK_PORT=18080\n", encoding="utf-8")
    etc.chmod(0o000)
    link = tmp_path / ".env"
    link.symlink_to(etc / "env")
    monkeypatch.setattr(address, "ENV_FILE", link)
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)

    try:
        with pytest.raises(RuntimeError) as refusal:
            address.configured_health_url()
    finally:
        etc.chmod(0o700)

    assert f"{link} -> {etc / 'env'}" in str(refusal.value)


def test_no_env_at_all_still_gets_the_original_refusal(tmp_path, monkeypatch):
    """STU-1087's message unchanged: a fresh checkout and a worktree have no `.env`."""
    monkeypatch.setattr(address, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)

    with pytest.raises(RuntimeError, match="Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT"):
        address.configured_health_url()


def test_a_readable_env_without_the_key_gets_the_original_refusal(tmp_path, monkeypatch):
    """Reading the file and finding nothing in it is the unset case, not the denied one."""
    path = tmp_path / ".env"
    path.write_text("OUTLOOK_CLIENT_ID=abc\n", encoding="utf-8")
    monkeypatch.setattr(address, "ENV_FILE", path)
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)

    with pytest.raises(RuntimeError, match="Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT"):
        address.configured_health_url()


def test_a_dangling_link_is_absence_and_not_a_permission_problem(tmp_path, monkeypatch):
    """The name is there and the file is not, which really is "nothing said the address".

    Claiming an ownership problem here would be the same wrong cause one door over.
    """
    link = tmp_path / ".env"
    link.symlink_to(tmp_path / "gone")
    monkeypatch.setattr(address, "ENV_FILE", link)
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)

    with pytest.raises(RuntimeError, match="Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT"):
        address.configured_health_url()


@_as_root
def test_the_check_keeps_the_refusal_code_and_alerts_with_the_file_named(
    unreadable_env, monkeypatch, capsys
):
    """Still 5: both conditions are "the invocation never asked the webhook anything".

    A code of its own would buy nothing here -- the dedup keys on the code because a
    fault has to page once per fault, and no cron path reaches this one: on the serving
    host cron already runs as the account that can read the file. What a hand-run acts
    on is the message.
    """
    monkeypatch.setattr(
        check_webhook_health.urllib.request, "urlopen",
        lambda *a, **k: pytest.fail("nothing was readable, so nothing may be polled"),
    )

    assert check_webhook_health.main([]) == check_webhook_health.NOT_CONFIGURED

    assert str(unreadable_env) in capsys.readouterr().err
    assert _journal_entries()[-1]["exit"] == check_webhook_health.NOT_CONFIGURED
    assert _alerted and str(unreadable_env) in _alerted[0]


def test_a_url_on_the_command_line_answers_for_the_environment(monkeypatch):
    """`--url` is a complete answer, so it is resolved after parsing, not before."""
    monkeypatch.delenv("WEBHOOK_PORT", raising=False)
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)
    polled = {}

    def record(url, timeout=None):
        polled["url"] = url
        return _FakeResponse(_OK)

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", record)

    assert check_webhook_health.main(["--url", "http://serving-host.lan:9999/healthz"]) == 0
    assert polled["url"] == "http://serving-host.lan:9999/healthz"


def test_a_usage_error_does_not_read_as_a_dead_webhook(capsys):
    """argparse exits 2 of its own accord, which is this check's outage code."""
    with pytest.raises(SystemExit) as exit_:
        check_webhook_health.main(["--not-a-flag"])

    assert exit_.value.code == check_webhook_health.NOT_CONFIGURED
    assert "unrecognized arguments" in capsys.readouterr().err


def test_a_forward_host_without_an_address_to_forward_to_is_refused(monkeypatch):
    """The fallback describes this machine, so forwarding it dials the wrong port.

    Measured on 2026-08-28: a round sent from a laptop polled
    `http://127.0.0.1:8080/healthz` and reported no webhook answering, while
    the serving host had stamped the notification 13s after the round left (STU-1027).
    """
    monkeypatch.delenv("WEBHOOK_HEALTH_URL", raising=False)
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")

    with pytest.raises(RuntimeError, match="WEBHOOK_HEALTH_URL"):
        address.configured_health_url()


def test_a_forward_host_with_an_address_asks_that_address(monkeypatch):
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://127.0.0.1:18080/healthz")
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")

    assert address.configured_health_url() == "http://127.0.0.1:18080/healthz"


def test_an_empty_override_falls_back_to_the_derived_url(monkeypatch):
    """`.env` carries the key empty, and Make exports empty as set."""
    monkeypatch.setenv("WEBHOOK_PORT", "18080")
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "")
    polled = {}

    def record(url, timeout=None):
        polled["url"] = url
        return _FakeResponse(_OK)

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", record)

    assert check_webhook_health.main([]) == 0
    assert polled["url"] == "http://127.0.0.1:18080/healthz"


# --- Deploy drift (STU-1017) -------------------------------------------------
#
# The host that classifies live mail ran fifteen commits behind `main` for days —
# two of them privacy fixes — while answering every check above. Nothing surfaced
# it, because the payload carried every axis except which code produced it.


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t.io", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True,
    )


def _commit(repo, message):
    (repo / "file.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _rev(repo, "HEAD")


def _rev(repo, ref):
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _origin_and_clone(tmp_path):
    """A remote with one commit on `main`, and a clone sitting on it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q")
    _git(origin, "checkout", "-q", "-b", "main")
    _commit(origin, "first")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)],
                   check=True, capture_output=True)
    return origin, clone


def test_the_payload_names_the_commit_the_process_serves(tmp_path):
    """Read from the checkout the running code was imported from, so the hourly
    check has something to compare at all."""
    from email_classifier.webhook import server

    health = _client(tmp_path).get("/healthz").get_json()

    assert health["commit"] == _rev(server.REPO_ROOT, "HEAD")


def test_a_checkout_git_cannot_read_reports_no_commit(tmp_path):
    """A tarball deploy has no repo. One missing axis beats an endpoint that 500s
    on its own reporting."""
    assert deploy.head(tmp_path) is None


def test_a_clone_at_the_tip_is_not_behind(tmp_path):
    origin, clone = _origin_and_clone(tmp_path)

    drift = deploy.measure(_rev(clone, "HEAD"), repo_root=clone)

    assert drift.commits_behind == 0
    assert drift.summary() == f"running {_rev(origin, 'HEAD')[:12]}, current with origin/main"


def test_a_clone_behind_main_counts_the_commits(tmp_path):
    origin, clone = _origin_and_clone(tmp_path)
    deployed = _rev(clone, "HEAD")
    _commit(origin, "second")
    tip = _commit(origin, "third")

    drift = deploy.measure(deployed, repo_root=clone)

    assert drift.commits_behind == 2
    assert drift.behind is True
    assert drift.target == tip
    assert drift.summary() == (
        f"running {deployed[:12]}, 2 commits behind origin/main ({tip[:12]})"
    )


def test_one_commit_behind_reads_as_one_commit(tmp_path):
    origin, clone = _origin_and_clone(tmp_path)
    deployed = _rev(clone, "HEAD")
    _commit(origin, "second")

    assert "1 commit behind" in deploy.measure(deployed, repo_root=clone).summary()


def test_a_host_ahead_of_main_is_not_behind(tmp_path):
    """A hotfix applied on the host is a different problem, and `git_dirty` on the
    run's provenance is where it shows. This check answers "is `main` past you"."""
    _origin, clone = _origin_and_clone(tmp_path)
    deployed = _commit(clone, "local hotfix")

    assert deploy.measure(deployed, repo_root=clone).commits_behind == 0


def test_an_unreachable_remote_is_unmeasured_rather_than_current(tmp_path):
    """The direction that matters: answering "in sync" off a `main` this clone last
    saw weeks ago is the same silence the whole check exists to break."""
    _origin, clone = _origin_and_clone(tmp_path)
    deployed = _rev(clone, "HEAD")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))

    drift = deploy.measure(deployed, repo_root=clone)

    assert drift.commits_behind is None
    assert drift.behind is False
    assert drift.summary() == "deploy unverified: origin is unreachable"


def test_a_payload_with_no_commit_is_unmeasured(tmp_path):
    """What a host still serving the payload from before this issue reports."""
    _origin, clone = _origin_and_clone(tmp_path)

    drift = deploy.measure(None, repo_root=clone)

    assert drift.commits_behind is None
    assert drift.summary() == "deploy unverified: the health payload names no commit"


def test_a_commit_this_clone_does_not_have_is_unmeasured(tmp_path):
    _origin, clone = _origin_and_clone(tmp_path)
    absent = "0" * 40

    drift = deploy.measure(absent, repo_root=clone)

    assert drift.commits_behind is None
    assert drift.summary() == f"deploy unverified: {absent[:12]} is not in this clone"


def test_a_commit_id_that_is_not_one_never_reaches_git(tmp_path):
    """Every use of the reported sha is a git argument, and it arrives over HTTP."""
    _origin, clone = _origin_and_clone(tmp_path)

    drift = deploy.measure("--upload-pack=touch /tmp/pwned", repo_root=clone, fetch=False)

    assert drift.commits_behind is None
    assert "is not a commit id" in drift.summary()


def _serve_drift(monkeypatch, drift):
    monkeypatch.setattr(
        check_webhook_health, "deploy", SimpleNamespace(measure=lambda *a, **k: drift)
    )


_BEHIND = deploy.Drift("a" * 40, "b" * 40, 15, None)
_CURRENT = deploy.Drift("a" * 40, "a" * 40, 0, None)


def test_check_reports_drift_under_its_own_exit_code(monkeypatch, capsys):
    """Not 2: that code has to keep meaning "the webhook is dead" and nothing else,
    since it is the only alert a deleted subscription ever produces (STU-978)."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)

    assert check_webhook_health.main([]) == 3

    err = capsys.readouterr().err
    assert "[DRIFT]" in err
    assert "15 commits behind origin/main" in err
    assert "unreachable" not in err


def test_check_exits_zero_when_the_host_runs_what_main_holds(monkeypatch, capsys):
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)

    assert check_webhook_health.main([]) == 0
    assert "current with origin/main" in capsys.readouterr().out


def test_an_unmeasurable_deploy_is_not_an_alert(monkeypatch, capsys):
    """A check running from another machine has no clone to compare against, and a
    cron that mails every hour over that is a cron nobody reads."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))

    assert check_webhook_health.main([]) == 0
    assert "deploy unverified" in capsys.readouterr().out


def test_a_stale_host_alerts_as_stale_even_when_behind(monkeypatch, capsys):
    """Silence outranks staleness: a webhook receiving nothing classifies no mail at
    all, whichever commit it would have classified it with."""
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})
    _serve_drift(monkeypatch, _BEHIND)

    assert check_webhook_health.main([]) == 1

    err = capsys.readouterr().err
    assert "[ALERT] no notification in 25.0h" in err
    assert "15 commits behind" in err


def test_an_unreachable_endpoint_is_never_read_as_drift(monkeypatch, capsys):
    def refuse(url, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(check_webhook_health.urllib.request, "urlopen", refuse)
    _serve_drift(monkeypatch, _BEHIND)

    assert check_webhook_health.main([]) == 2
    assert "[DRIFT]" not in capsys.readouterr().err


# --- Reporting the verdict (STU-1066) ----------------------------------------
#
# The non-zero exit was the whole alert, and its only reader was cron mail on a host
# nobody reads mail on. A check exiting 3 said so to nobody for days.


@pytest.mark.parametrize(
    "health, expected_code",
    [
        ({**_OK, "status": "stale", "last_notification_age_seconds": 90000.0}, 1),
        (_OK, 0),
    ],
)
def test_the_verdict_is_notified_unless_it_is_ok(monkeypatch, health, expected_code):
    _serve(monkeypatch, health)

    assert check_webhook_health.main([]) == expected_code
    assert bool(_alerted) is (expected_code != 0)


def test_an_unreachable_endpoint_notifies(monkeypatch):
    monkeypatch.setattr(
        check_webhook_health.urllib.request, "urlopen",
        lambda url, timeout=None: _raise(OSError("connection refused")),
    )

    assert check_webhook_health.main([]) == 2
    assert "[ALERT] webhook unreachable" in _alerted[0]


def test_drift_notifies_under_its_own_prefix(monkeypatch):
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift("dead", "beef", 1))

    assert check_webhook_health.main([]) == 3
    assert _alerted[0].startswith("[DRIFT] ")


def test_a_forward_that_will_not_open_notifies(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://127.0.0.1:18080/healthz")
    monkeypatch.setattr(
        check_webhook_health.tunnel, "reachable",
        lambda url: _raise(check_webhook_health.tunnel.ForwardFailed("ssh exited 255")),
    )

    assert check_webhook_health.main([]) == 4
    assert _alerted == ["[ALERT] ssh exited 255"]


def test_a_standing_fault_notifies_once_not_every_hour(monkeypatch):
    """A drift left unfixed for a day is one fault, not 24 notifications."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift("dead", "beef", 1))

    assert [check_webhook_health.main([]) for _ in range(5)] == [3, 3, 3, 3, 3]
    assert len(_alerted) == 1
    assert len(_journal_entries()) == 5


def test_a_fault_that_changes_class_notifies_again(monkeypatch):
    """The codes were given separate numbers so they would not collapse."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift("dead", "beef", 1))
    assert check_webhook_health.main([]) == 3

    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})
    assert check_webhook_health.main([]) == 1

    assert [line.split()[0] for line in _alerted] == ["[DRIFT]", "[ALERT]"]


def test_a_fault_that_recurs_after_a_recovery_notifies_again(monkeypatch):
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift("dead", "beef", 1))
    assert check_webhook_health.main([]) == 3

    _serve_drift(monkeypatch, deploy.Drift("beef", "beef", 0))
    assert check_webhook_health.main([]) == 0

    _serve_drift(monkeypatch, deploy.Drift("dead", "beef", 1))
    assert check_webhook_health.main([]) == 3

    assert len(_alerted) == 2


def test_every_run_is_recorded_with_its_code_and_summary(monkeypatch):
    """Criterion 5 of the pilot gate asks what the hourly runs exited over five days."""
    _serve(monkeypatch, _OK)
    assert check_webhook_health.main([]) == 0
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})
    assert check_webhook_health.main([]) == 1

    entries = _journal_entries()
    assert [entry["exit"] for entry in entries] == [0, 1]
    assert entries[0]["summary"].startswith("ok — last notification")
    assert "no notification in 25.0h" in entries[1]["summary"]
    assert all(entry["at"] for entry in entries)


# --- Who ran it, and where it is written (STU-1082) --------------------------
#
# Every caller wrote the same shape, so the file answered "what did the hourly runs
# exit" with every run it had — including a hand-run of the script without the env
# sourced, which polled another service's port and recorded an outage that never was.


def test_a_line_names_what_produced_it(monkeypatch):
    """Criterion 5 asks about the hourly runs, so they have to be readable apart."""
    _serve(monkeypatch, _OK)

    assert check_webhook_health.main(["--source", "cron"]) == 0
    assert check_webhook_health.main(["--source", "deploy"]) == 0
    assert check_webhook_health.main([]) == 0

    assert [entry["source"] for entry in _journal_entries()] == [
        "cron", "deploy", "manual",
    ]


def test_an_unflagged_run_is_not_counted_as_hourly(monkeypatch):
    """The flag can be forgotten; the direction that fails safe is undercounting cron."""
    _serve(monkeypatch, _OK)

    assert check_webhook_health.main([]) == 0
    assert _journal_entries()[0]["source"] == "manual"


def test_a_drill_leaves_the_pilots_evidence_untouched(tmp_path, monkeypatch):
    """Proving the channel works needed a hand-patched constant, which is how the
    false line got written in the first place."""
    drill = tmp_path / "drill.jsonl"
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})

    assert check_webhook_health.main(["--journal", str(drill)]) == 1

    assert _alerted, "a drill that does not notify has drilled nothing"
    assert json.loads(drill.read_text(encoding="utf-8"))["exit"] == 1
    assert not check_webhook_health.JOURNAL.exists()


def test_a_drill_does_not_dedup_the_next_real_alert(tmp_path, monkeypatch):
    """It dedups against its own file, so the host's standing fault is still reported."""
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})
    assert check_webhook_health.main(["--journal", str(tmp_path / "drill.jsonl")]) == 1

    assert check_webhook_health.main(["--source", "cron"]) == 1
    assert len(_alerted) == 2


def test_a_journal_that_cannot_be_written_warns_and_still_alerts(
    tmp_path, monkeypatch, capsys
):
    """A dedup that cannot remember must repeat, not go quiet."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    unwritable = blocked / "health-checks.jsonl"
    monkeypatch.setattr(check_webhook_health, "JOURNAL", unwritable)
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})

    assert [check_webhook_health.main([]) for _ in range(2)] == [1, 1]
    assert len(_alerted) == 2
    assert f"[WARN] could not record this run in {unwritable}" in capsys.readouterr().err


# --- The client secret's expiry, announced before it bites (STU-1095) ---------
#
# The one fault this check can report ahead of time. A client secret cannot be renewed
# unattended, so the alert that matters arrives weeks before the date, not on it.


def _secret(monkeypatch, expires=None):
    """Configure the app-only flow, which is what makes the expiry leg apply at all."""
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "a-secret")
    if expires is not None:
        monkeypatch.setenv("OUTLOOK_SECRET_EXPIRES", expires)


def _in_days(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_the_expiry_leg_does_not_apply_without_a_client_secret(monkeypatch):
    """Every staging host and every worktree runs the interactive flow, which has no
    secret to expire — so the leg has nothing to warn about and must not."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)

    assert check_webhook_health.main([]) == 0
    assert not _alerted


def test_a_secret_expiring_inside_the_window_alerts_under_its_own_code(
    monkeypatch, capsys
):
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)
    _secret(monkeypatch, _in_days(12))

    assert check_webhook_health.main([]) == check_webhook_health.EXPIRING_SOON

    err = capsys.readouterr().err
    assert "[EXPIRING]" in err
    assert "expires in 12 days" in err
    assert _in_days(12) in err, "the date itself, not only the countdown"
    assert _alerted


def test_a_secret_expiring_beyond_the_window_is_reported_but_not_alerted(
    monkeypatch, capsys
):
    """Named on the ok line: a leg that never fires and a leg nobody wired look the
    same from outside, which is the reading STU-912 refuses."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)
    _secret(monkeypatch, _in_days(365))

    assert check_webhook_health.main([]) == 0
    assert "expires in 365 days" in capsys.readouterr().out
    assert not _alerted


def test_an_expired_secret_still_reports_under_the_expiry_code(monkeypatch, capsys):
    """Past the date is the outage itself, repaired by the same visit to the console."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)
    _secret(monkeypatch, _in_days(-3))

    assert check_webhook_health.main([]) == check_webhook_health.EXPIRING_SOON
    assert "expired 3 days ago" in capsys.readouterr().err


def test_a_configured_secret_with_no_expiry_is_not_a_silent_pass(monkeypatch, capsys):
    """The shape STU-1087 gave the port: a probe not told when the credential dies
    says so under its own code rather than answering as though it knew."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)
    _secret(monkeypatch)

    assert check_webhook_health.main([]) == check_webhook_health.EXPIRY_UNKNOWN

    err = capsys.readouterr().err
    assert "OUTLOOK_SECRET_EXPIRES is not" in err
    assert _alerted


def test_an_unparseable_expiry_is_unknown_rather_than_distant(monkeypatch, capsys):
    """`fromisoformat` failing must not fall through to "not expiring soon"."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)
    _secret(monkeypatch, "next August")

    assert check_webhook_health.main([]) == check_webhook_health.EXPIRY_UNKNOWN
    assert "not an ISO date" in capsys.readouterr().err


def test_a_silent_webhook_outranks_an_expiring_secret(monkeypatch, capsys):
    """Same order as drift: a webhook receiving nothing classifies no mail now,
    whichever credential it would have classified it with."""
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})
    _secret(monkeypatch, _in_days(1))

    assert check_webhook_health.main([]) == 1
    assert "[ALERT] no notification in 25.0h" in capsys.readouterr().err


def test_an_expiring_secret_outranks_drift(monkeypatch, capsys):
    """The inverse of the rule above. Drift repairs itself with `make pull-webhook`
    and resurfaces on the next hourly run; the warning window only runs out, so a host
    left behind would otherwise mask the expiry for the whole month it exists to give."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)
    _secret(monkeypatch, _in_days(5))

    assert check_webhook_health.main([]) == check_webhook_health.EXPIRING_SOON
    assert "expires in 5 days" in capsys.readouterr().err


def test_a_standing_expiry_warning_notifies_once(monkeypatch):
    """Dedup is on the code alone (STU-1066), so a month of lead time is a handful of
    notifications rather than thirty. The hourly stderr line and journal still record
    every run."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)
    _secret(monkeypatch, _in_days(20))

    codes = [check_webhook_health.main([]) for _ in range(3)]

    assert codes == [check_webhook_health.EXPIRING_SOON] * 3
    assert len(_alerted) == 1
    assert len(_journal_entries()) == 3


def test_the_expiry_codes_are_distinct_from_the_address_refusal():
    """Both are "nobody told the probe something", and folding them would hide one
    behind the other: 5 returns before the webhook is ever polled, and the dedup key
    is the code alone."""
    assert len({
        check_webhook_health.NOT_CONFIGURED,
        check_webhook_health.EXPIRING_SOON,
        check_webhook_health.EXPIRY_UNKNOWN,
        check_webhook_health.DEPLOY_BLIND,
        check_webhook_health.DRIFT_SUSTAINED,
    }) == 5


# --- A drift leg that has gone blind (STU-1126) ------------------------------
#
# The serving host recorded `deploy unverified: origin is unreachable` at 15:59, 16:23 and
# 17:23 on 30 August 2026 and exited 0 every time. The leg STU-1017 built to catch a
# host running stale code was itself dark for three runs, and nothing counted them:
# exit 0 is the same answer a host that is genuinely current gives.


def _unreachable_remote(tmp_path):
    """A clone whose `origin` is gone — the real shape, not a stubbed verdict."""
    _origin, clone = _origin_and_clone(tmp_path)
    deployed = _rev(clone, "HEAD")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone"))
    return lambda: deploy.measure(deployed, repo_root=clone)


def test_a_single_blind_run_still_exits_zero(tmp_path, monkeypatch, capsys):
    """STU-1017's call stands: comparing against a `main` seen weeks ago answers
    "up to date" for a host that is behind, which is worse than answering nothing."""
    _serve(monkeypatch, _OK)
    measure = _unreachable_remote(tmp_path)
    monkeypatch.setattr(
        check_webhook_health, "deploy", SimpleNamespace(measure=lambda *a, **k: measure())
    )

    assert check_webhook_health.main([]) == 0
    assert "deploy unverified: origin is unreachable" in capsys.readouterr().out
    assert _alerted == []


def test_the_nth_consecutive_blind_run_alerts_under_its_own_code(tmp_path, monkeypatch,
                                                                 capsys):
    _serve(monkeypatch, _OK)
    measure = _unreachable_remote(tmp_path)
    monkeypatch.setattr(
        check_webhook_health, "deploy", SimpleNamespace(measure=lambda *a, **k: measure())
    )
    runs = check_webhook_health.BLIND_AFTER_RUNS

    codes = [check_webhook_health.main([]) for _ in range(runs)]

    assert codes == [0] * (runs - 1) + [check_webhook_health.DEPLOY_BLIND]
    assert len(_alerted) == 1
    assert _alerted[0].startswith("[BLIND] deploy unverified: origin is unreachable")
    assert f"{runs} consecutive runs" in capsys.readouterr().err


def test_a_standing_blind_leg_notifies_once_not_every_hour(monkeypatch):
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))

    codes = [check_webhook_health.main([]) for _ in range(6)]

    assert codes[-1] == check_webhook_health.DEPLOY_BLIND
    assert len(_alerted) == 1


def test_a_run_that_measures_resets_the_streak(monkeypatch):
    """The come-back-after-a-zero rule STU-1066 applies to the other codes: a leg
    that answered once has been looked at, whatever it answered before."""
    _serve(monkeypatch, _OK)
    blind = deploy.Drift(None, None, None, "origin is unreachable")
    _serve_drift(monkeypatch, blind)
    assert [check_webhook_health.main([]) for _ in range(3)][-1] == 8

    _serve_drift(monkeypatch, _CURRENT)
    assert check_webhook_health.main([]) == 0

    _serve_drift(monkeypatch, blind)
    assert [check_webhook_health.main([]) for _ in range(3)] == [0, 0, 8]
    assert len(_alerted) == 2


def test_a_run_that_never_reached_the_leg_neither_counts_nor_resets(monkeypatch):
    """An unreachable endpoint learned nothing about the deploy. Counting it would
    alert on a leg nobody asked; resetting on it would let a dead webhook hide a dead
    remote behind it for as long as both last."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))
    assert [check_webhook_health.main([]) for _ in range(2)] == [0, 0]

    monkeypatch.setattr(
        check_webhook_health.urllib.request, "urlopen",
        lambda url, timeout=None: _raise(OSError("connection refused")),
    )
    assert check_webhook_health.main([]) == 2
    assert "deploy_unverified" not in _journal_entries()[-1]

    _serve(monkeypatch, _OK)
    assert check_webhook_health.main([]) == check_webhook_health.DEPLOY_BLIND


def test_a_silent_webhook_outranks_a_blind_leg(monkeypatch, capsys):
    """Same order as drift: a webhook receiving nothing classifies no mail at all."""
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})
    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))

    assert [check_webhook_health.main([]) for _ in range(3)] == [1, 1, 1]
    assert "no notification in 25.0h" in capsys.readouterr().err


def test_an_expiring_secret_outranks_a_blind_leg(monkeypatch):
    """The warning window is the only thing here that runs out; a blind leg keeps
    reporting itself for as long as it is blind."""
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "a-secret")
    monkeypatch.setenv("OUTLOOK_SECRET_EXPIRES", str(date.today() + timedelta(days=5)))
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))

    codes = [check_webhook_health.main([]) for _ in range(3)]

    assert codes == [check_webhook_health.EXPIRING_SOON] * 3


def test_a_journal_that_cannot_be_read_counts_no_streak(tmp_path, monkeypatch):
    """A streak is a measurement, and one that could not be made is not one to
    invent — the direction STU-912 fixes for every check here."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()

    codes = [check_webhook_health.main(["--journal", str(unreadable)]) for _ in range(4)]

    assert codes == [0, 0, 0, 0]


def test_a_line_predating_the_field_neither_counts_nor_resets(tmp_path, monkeypatch):
    """Lines written before this leg existed say nothing about the deploy, so the
    streak reads through them rather than treating their silence as an answer."""
    journal = tmp_path / "old.jsonl"
    journal.write_text(
        "".join(json.dumps({"at": "2026-08-30T00:00:00+00:00", "exit": 0,
                            "source": "cron", "summary": "ok"}) + "\n"
                for _ in range(9)),
        encoding="utf-8",
    )
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))

    codes = [check_webhook_health.main(["--journal", str(journal)]) for _ in range(3)]

    assert codes == [0, 0, check_webhook_health.DEPLOY_BLIND]


# --- A drift that does not end (STU-1181) ------------------------------------
#
# The serving host exited 3 on 27 consecutive cron runs, 2026-08-30T19:45Z to
# 2026-08-31T23:23Z, the summary walking from 2 commits behind to 22. One Pushover
# notification for the whole window, which is the dedup working as designed — and
# nothing at all separating a drift being repaired from one nobody read. It ended
# because a human happened to read the probe's output in a session about something
# else, after 27 hours of parent mail drafted by a commit `main` had replaced.


def _standing_drift(runs, exit_code=3, behind=True):
    """A journal of runs whose deploy leg measured, written the way the check writes."""
    return "".join(
        json.dumps({
            "at": f"2026-08-30T{19 + n:02d}:45:00+00:00",
            "exit": exit_code,
            "source": "cron",
            "summary": "running aaaaaaaaaaaa, 15 commits behind origin/main (bbbbbbbbbbbb)",
            "deploy_unverified": False,
            "deploy_behind": behind,
        }) + "\n"
        for n in range(runs)
    )


def test_a_fresh_drift_still_exits_three(monkeypatch, capsys):
    """The escalation is a second fault, not a replacement for the first one."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)

    codes = [check_webhook_health.main([])
             for _ in range(check_webhook_health.SUSTAINED_AFTER_RUNS - 1)]

    assert codes == [3] * len(codes)
    assert len(_alerted) == 1
    assert "consecutive runs" not in capsys.readouterr().err


def test_the_nth_standing_drift_escalates_under_its_own_code(monkeypatch, capsys):
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)
    runs = check_webhook_health.SUSTAINED_AFTER_RUNS

    codes = [check_webhook_health.main([]) for _ in range(runs)]

    assert codes == [3] * (runs - 1) + [check_webhook_health.DRIFT_SUSTAINED]
    assert _alerted[-1].startswith("[DRIFT UNFIXED]")
    assert f"{runs} consecutive runs" in capsys.readouterr().err


def test_the_escalation_is_a_second_notification_not_a_repeat(monkeypatch):
    """The whole point of the code being its own: the exit-3 alert already fired and
    was deduped for the rest of the window, so a standing drift had no second voice."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)

    for _ in range(check_webhook_health.SUSTAINED_AFTER_RUNS + 3):
        check_webhook_health.main([])

    assert len(_alerted) == 2
    assert _alerted[0].startswith("[DRIFT]")
    assert _alerted[1].startswith("[DRIFT UNFIXED]")


def test_a_journal_of_standing_drift_escalates_on_the_next_run(tmp_path, monkeypatch):
    """The count survives the process, because the check is an hourly cron that exits:
    every run but the last one below happened in some earlier process."""
    journal = tmp_path / "standing.jsonl"
    journal.write_text(
        _standing_drift(check_webhook_health.SUSTAINED_AFTER_RUNS - 1), encoding="utf-8"
    )
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)

    assert check_webhook_health.main(["--journal", str(journal)]) == (
        check_webhook_health.DRIFT_SUSTAINED
    )


def test_a_run_that_measured_current_clears_the_count(tmp_path, monkeypatch):
    """A host that was brought up to date and drifted again is a fresh drift, and gets
    the fresh drift's alert — the same rule STU-1066 applies to a fault coming back."""
    journal = tmp_path / "cleared.jsonl"
    journal.write_text(
        _standing_drift(9)
        + _standing_drift(1, exit_code=0, behind=False)
        + _standing_drift(2),
        encoding="utf-8",
    )
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)

    assert check_webhook_health.main(["--journal", str(journal)]) == 3


def test_a_line_predating_the_field_neither_counts_nor_clears(tmp_path, monkeypatch):
    """Lines written before this leg existed say nothing about the drift, so the count
    reads through them rather than reading their silence as an answer — in either
    direction. Exit 3 is on every one of them and is not what is being counted."""
    journal = tmp_path / "old.jsonl"
    journal.write_text(
        "".join(json.dumps({"at": "2026-08-30T00:00:00+00:00", "exit": 3,
                            "source": "cron", "summary": "15 commits behind"}) + "\n"
                for _ in range(9))
        + _standing_drift(check_webhook_health.SUSTAINED_AFTER_RUNS - 2),
        encoding="utf-8",
    )
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)

    codes = [check_webhook_health.main(["--journal", str(journal)]) for _ in range(2)]

    assert codes == [3, check_webhook_health.DRIFT_SUSTAINED]


def test_a_blind_run_neither_counts_nor_clears_the_drift(monkeypatch):
    """A leg that answered nothing did not say the host is current, so it must not
    clear the count; nor did it say the host is behind. It is transparent, the way an
    unreachable endpoint is transparent to the blind count (STU-1126)."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)
    assert [check_webhook_health.main([]) for _ in range(4)] == [3] * 4

    _serve_drift(monkeypatch, deploy.Drift(None, None, None, "origin is unreachable"))
    assert check_webhook_health.main([]) == 0

    _serve_drift(monkeypatch, _BEHIND)
    assert [check_webhook_health.main([]) for _ in range(2)] == [
        3, check_webhook_health.DRIFT_SUSTAINED
    ]


def test_a_drift_masked_by_silence_goes_on_standing(monkeypatch):
    """Why the count is a field and not the exit code: a stale age outranks drift, so
    those runs exit 1 while the host stays exactly as far behind as it was."""
    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})
    _serve_drift(monkeypatch, _BEHIND)
    runs = check_webhook_health.SUSTAINED_AFTER_RUNS

    assert [check_webhook_health.main([]) for _ in range(runs)] == [1] * runs
    assert all(entry["deploy_behind"] for entry in _journal_entries())

    _serve(monkeypatch, _OK)
    assert check_webhook_health.main([]) == check_webhook_health.DRIFT_SUSTAINED


def test_a_silent_webhook_outranks_a_standing_drift(monkeypatch, capsys):
    """Precedence is the family's, unchanged: a webhook receiving nothing classifies no
    mail at all, however long the commit it would have classified it with has stood."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)
    for _ in range(check_webhook_health.SUSTAINED_AFTER_RUNS):
        check_webhook_health.main([])

    _serve(monkeypatch, {**_OK, "status": "stale", "last_notification_age_seconds": 90000.0})

    assert check_webhook_health.main([]) == 1
    assert "[ALERT] no notification in 25.0h" in capsys.readouterr().err


def test_an_expiring_secret_outranks_a_standing_drift(monkeypatch):
    """The warning window is the only thing here that runs out; a standing drift keeps
    reporting itself for as long as it stands."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)
    for _ in range(check_webhook_health.SUSTAINED_AFTER_RUNS):
        check_webhook_health.main([])
    _secret(monkeypatch, _in_days(5))

    assert check_webhook_health.main([]) == check_webhook_health.EXPIRING_SOON


def test_a_journal_that_cannot_be_read_never_escalates_a_drift(tmp_path, monkeypatch):
    """A count is a measurement, and one that could not be made is not one to invent —
    the direction STU-912 fixes. The fresh drift is still reported every run."""
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()

    runs = check_webhook_health.SUSTAINED_AFTER_RUNS + 2
    codes = [check_webhook_health.main(["--journal", str(unreadable)])
             for _ in range(runs)]

    assert codes == [3] * runs


def test_the_two_thresholds_are_decided_separately():
    """One sizes a first notification and the other a second, so inheriting either
    from the other is the decision STU-1181 says has to be made rather than assumed."""
    assert check_webhook_health.SUSTAINED_AFTER_RUNS == 6
    assert check_webhook_health.BLIND_AFTER_RUNS == 3


def test_measure_reads_the_expiry_against_a_given_today(monkeypatch):
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "a-secret")
    monkeypatch.setenv("OUTLOOK_SECRET_EXPIRES", "2027-08-29")

    expiry = credential.measure(today=date(2027, 8, 1))

    assert (expiry.expires_on, expiry.days_left, expiry.unknown) == (
        date(2027, 8, 29), 28, None
    )
    assert expiry.soon


def test_an_empty_expiry_counts_as_unset(monkeypatch):
    """`.env.example` carries keys empty and Make exports them that way."""
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "a-secret")
    monkeypatch.setenv("OUTLOOK_SECRET_EXPIRES", "")

    assert credential.measure().unknown


# --- the audit-artifact countdown (STU-1168) -----------------------------------------
#
# A push-path entry protects none of its artifacts (STU-1084), so 30 days after its
# report was written a reviewer loses everything they would have re-derived from and the
# entry becomes counters nobody can check. `coverage` says how much is owed; nothing said
# how long was left.


def _measured_audit_window(monkeypatch, root, now):
    """Point the check's leg at a tree the test built.

    Against `_MEASURE_AUDIT_WINDOW` and not the module attribute, which the autouse
    fixture above has already replaced with the one that answers None.
    """
    monkeypatch.setattr(check_webhook_health.audit_expiry, "measure",
                        lambda *a, **k: _MEASURE_AUDIT_WINDOW(root, now=now))


def _audit_tree(tmp_path, *, ages_days, reviewed=(), now=None):
    """A repo root holding one report per age, and a log filing each as unreviewed."""
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    (tmp_path / "audits").mkdir(exist_ok=True)
    now = now if now is not None else 1_000_000_000.0
    entries = []
    for i, age in enumerate(ages_days):
        run_id = f"{i:08x}-0000-0000-0000-000000000000"
        path = reports / f"{run_id}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (now - age * 86400, now - age * 86400))
        entries.append({"run_id": run_id,
                        "verdict": "reviewed by hand" if run_id in reviewed
                                   else "UNREVIEWED (machine-filed)"})
    (tmp_path / "audits" / "audit_log.json").write_text(
        json.dumps(entries), encoding="utf-8")
    return tmp_path, now


def test_an_entry_inside_the_window_alerts_under_its_own_code(monkeypatch, capsys,
                                                              tmp_path):
    root, now = _audit_tree(tmp_path, ages_days=[26, 3, 2, 1])
    _measured_audit_window(monkeypatch, root, now)
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)

    assert check_webhook_health.main([]) == check_webhook_health.AUDIT_EXPIRING

    err = capsys.readouterr().err
    assert "[AUDIT EXPIRING]" in err
    assert "1 of 4 inside the window" in err
    assert "losing its artifacts in 4 days" in err
    assert _alerted


def test_entries_with_runway_are_reported_on_the_ok_line(monkeypatch, capsys, tmp_path):
    """The serving host on 2026-09-01: 34 filed, the oldest 3 days old, 27 days of runway. The
    quiet leg still says so, or a wired one reads like one nobody wired (STU-912)."""
    root, now = _audit_tree(tmp_path, ages_days=[3, 2, 2, 1])
    _measured_audit_window(monkeypatch, root, now)
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)

    assert check_webhook_health.main([]) == 0
    assert "nearest" in capsys.readouterr().out
    assert not _alerted


def test_the_expiring_secret_still_outranks_the_audit_window(monkeypatch, tmp_path):
    """Two windows, and the one that takes the webhook out goes first."""
    root, now = _audit_tree(tmp_path, ages_days=[28, 3, 2, 1])
    _measured_audit_window(monkeypatch, root, now)
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)
    _secret(monkeypatch, _in_days(12))

    assert check_webhook_health.main([]) == check_webhook_health.EXPIRING_SOON


def test_the_audit_window_outranks_a_standing_drift(monkeypatch, tmp_path):
    """A drift repairs itself with `make pull-webhook` and is caught next hour; the
    window only runs out, so a drift in front of it would mask it for the month."""
    root, now = _audit_tree(tmp_path, ages_days=[28, 3, 2, 1])
    _measured_audit_window(monkeypatch, root, now)
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _BEHIND)

    assert check_webhook_health.main([]) == check_webhook_health.AUDIT_EXPIRING


def test_the_shelf_floor_is_not_counted_down_against(monkeypatch, tmp_path):
    """The newest few reports survive any age (`keep_last`), so a host holding three
    of them has no deadline at all — counting one down would be an alarm that never
    clears and never could."""
    root, now = _audit_tree(tmp_path, ages_days=[400, 399, 398])
    _measured_audit_window(monkeypatch, root, now)
    _serve(monkeypatch, _OK)
    _serve_drift(monkeypatch, _CURRENT)

    assert check_webhook_health.main([]) == 0
    assert not _alerted
