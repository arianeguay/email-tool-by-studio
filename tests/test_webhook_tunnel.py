"""Tests for the ssh forward that makes a loopback-only /healthz dialable (STU-1027).

Real sockets, no real ssh: what is under test is the URL the block hands back and
what happens when the forward never comes up, and both are answerable against a
stand-in that either listens on the port it was asked for or does not.
"""

import socket
import subprocess
import threading

import pytest

from email_classifier.webhook import tunnel


class FakeSsh:
    """A `Popen` stand-in that listens on the forwarded port, or refuses to.

    The forward is only ever detected by connecting, so a plain listener is the
    whole of what ssh contributes on the happy path.
    """

    def __init__(self, argv, listen=True, exit_code=None, stderr_text="",
                 stubborn=False):
        self.argv = argv
        self.returncode = exit_code
        self.stderr = _Reader(stderr_text)
        self.terminated = False
        self.killed = False
        self._stubborn = stubborn
        self._server = None
        if listen:
            port = int(argv[argv.index("-L") + 1].split(":")[0])
            self._server = socket.socket()
            self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server.bind(("127.0.0.1", port))
            self._server.listen(1)
            threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        try:
            self._server.accept()[0].close()
        except OSError:
            pass

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self._server:
            self._server.close()

    def kill(self):
        self.killed = True
        self._stubborn = False

    def wait(self, timeout=None):
        if self._stubborn:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.returncode or 0


class _Reader:
    def __init__(self, text):
        self.text = text

    def read(self):
        return self.text


@pytest.fixture
def spawned(monkeypatch):
    """Capture the ssh argv, and decide what that ssh does."""
    captured = {}

    def spawn(**kwargs):
        def popen(argv, **_):
            captured["argv"] = argv
            captured["process"] = FakeSsh(argv, **kwargs)
            return captured["process"]

        monkeypatch.setattr(tunnel.subprocess, "Popen", popen)
        return captured

    return spawn


def test_without_a_named_host_the_url_is_handed_back_untouched(monkeypatch):
    """The cron on the serving host: the URL already resolves, so nothing is opened."""
    monkeypatch.delenv("WEBHOOK_SSH_HOST", raising=False)
    monkeypatch.setattr(tunnel.subprocess, "Popen", _refuse_to_spawn)

    with tunnel.reachable("http://127.0.0.1:18080/healthz") as url:
        assert url == "http://127.0.0.1:18080/healthz"


def test_an_empty_host_is_no_host(monkeypatch):
    """.env.example carries the key empty and Make exports it that way."""
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "")
    monkeypatch.setattr(tunnel.subprocess, "Popen", _refuse_to_spawn)

    with tunnel.reachable("http://127.0.0.1:18080/healthz") as url:
        assert url == "http://127.0.0.1:18080/healthz"


def test_the_forward_hands_back_a_local_address_for_the_remote_one(monkeypatch, spawned):
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    captured = spawned()

    with tunnel.reachable("http://127.0.0.1:18080/healthz") as url:
        host, _, port = url.removeprefix("http://").partition("/")[0].partition(":")
        assert (host, url.endswith("/healthz")) == ("127.0.0.1", True)
        forward = captured["argv"][captured["argv"].index("-L") + 1]
        assert forward == f"{port}:127.0.0.1:18080"
        assert captured["argv"][-1] == "serving-host"


def test_the_forward_names_the_address_the_serving_host_sees(monkeypatch, spawned):
    """`WEBHOOK_HEALTH_URL` keeps describing the webhook; only the dialing moves."""
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    captured = spawned()

    with tunnel.reachable("http://webhook.internal/healthz"):
        forward = captured["argv"][captured["argv"].index("-L") + 1]

    assert forward.endswith(":webhook.internal:80")


def test_ssh_is_never_allowed_to_prompt_or_to_stay_up_without_the_forward(
    monkeypatch, spawned
):
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    captured = spawned()

    with tunnel.reachable("http://127.0.0.1:18080/healthz"):
        pass

    assert "BatchMode=yes" in captured["argv"]
    assert "ExitOnForwardFailure=yes" in captured["argv"]


def test_the_forward_is_closed_with_the_block(monkeypatch, spawned):
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    captured = spawned()

    with tunnel.reachable("http://127.0.0.1:18080/healthz"):
        assert not captured["process"].terminated

    assert captured["process"].terminated


def test_the_forward_is_closed_when_the_block_raises(monkeypatch, spawned):
    """A delivery round that dies must not leave an ssh behind."""
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    captured = spawned()

    with pytest.raises(ValueError):
        with tunnel.reachable("http://127.0.0.1:18080/healthz"):
            raise ValueError("the round blew up")

    assert captured["process"].terminated


def test_an_ssh_that_outlives_terminate_is_killed_without_masking_the_round(
    monkeypatch, spawned
):
    """`wait` raising out of the `finally` would replace the round's own failure."""
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    captured = spawned(stubborn=True)

    with pytest.raises(ValueError):
        with tunnel.reachable("http://127.0.0.1:18080/healthz"):
            raise ValueError("the round blew up")

    assert captured["process"].killed


def test_a_forward_that_never_opens_names_ssh_rather_than_blaming_the_webhook(
    monkeypatch, spawned
):
    """Handed back, the un-forwarded URL earns every caller's `no webhook answering`."""
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    spawned(listen=False, exit_code=255, stderr_text="serving-host: Name or service not known\n")

    with pytest.raises(tunnel.ForwardFailed) as raised:
        with tunnel.reachable("http://127.0.0.1:18080/healthz"):
            pytest.fail("the block ran without a forward")

    assert "no forward to http://127.0.0.1:18080/healthz through serving-host" in str(raised.value)
    assert "Name or service not known" in str(raised.value)


def test_a_silent_ssh_failure_still_says_something(monkeypatch, spawned):
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    spawned(listen=False, exit_code=255, stderr_text="")

    with pytest.raises(tunnel.ForwardFailed, match="ssh exited 255"):
        with tunnel.reachable("http://127.0.0.1:18080/healthz"):
            pass


def test_an_ssh_that_hangs_without_forwarding_is_given_up_on(monkeypatch, spawned):
    """Alive but not listening: the wait is bounded, or the round never finishes."""
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    monkeypatch.setattr(tunnel, "FORWARD_TIMEOUT", 0.3)
    spawned(listen=False)

    with pytest.raises(tunnel.ForwardFailed, match="still no listener"):
        with tunnel.reachable("http://127.0.0.1:18080/healthz"):
            pass


def test_a_forward_that_never_opens_leaves_no_ssh_behind(monkeypatch, spawned):
    """The refusal travels out through the same `finally` a working block does."""
    monkeypatch.setenv("WEBHOOK_SSH_HOST", "serving-host")
    captured = spawned(listen=False, exit_code=255)

    with pytest.raises(tunnel.ForwardFailed):
        with tunnel.reachable("http://127.0.0.1:18080/healthz"):
            pass

    assert captured["process"].terminated


def _refuse_to_spawn(*args, **kwargs):
    raise AssertionError("no ssh may be spawned without WEBHOOK_SSH_HOST")
