"""The shared detector (STU-938): one loaded model for the processes a run spawns."""

import json
import os
import socket
import threading
import time

import pytest

from email_classifier import anonymize, detector


@pytest.fixture
def served(monkeypatch):
    """A running detector service, with the local half stubbed to a known answer.

    It records the thread it ran on. In production the client and the server are
    different processes; in one test process the thread is what tells them apart, and
    "the detector was loaded by the server, not by the caller" is the claim under test.
    """
    ran_on = []

    def spans(text, from_address=None):
        ran_on.append(threading.current_thread())
        return [(0, len(text), "person")]

    monkeypatch.setattr(anonymize, "_local_person_spans", spans)
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    with detector.Detector() as service:
        service.ran_on = ran_on
        yield service


def _ask(path, payload):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    with client, client.makefile("rwb") as stream:
        stream.write(payload)
        stream.flush()
        return stream.readline()


def test_serves_the_spans_the_local_detector_would_have_found(served):
    assert detector.remote_spans("Caroline") == [(0, 8, "person")]


def test_person_spans_prefers_the_service(served):
    assert anonymize._person_spans("Caroline") == [(0, 8, "person")]
    # Answered by the server, so the caller never touched the weights itself.
    assert served.ran_on and threading.main_thread() not in served.ran_on


def test_person_spans_loads_locally_when_nobody_serves(monkeypatch):
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    monkeypatch.setattr(anonymize, "_local_person_spans", lambda text, from_address=None: [(1, 2, "person")])
    assert anonymize._person_spans("Caroline") == [(1, 2, "person")]


def test_a_stale_socket_falls_back_instead_of_failing(monkeypatch, tmp_path):
    monkeypatch.setenv(detector.SOCKET_ENV, str(tmp_path / "gone.sock"))
    monkeypatch.setattr(anonymize, "_local_person_spans", lambda text, from_address=None: [(1, 2, "person")])
    assert anonymize._person_spans("Caroline") == [(1, 2, "person")]


def test_a_failing_detector_raises_rather_than_reporting_no_names(monkeypatch, served):
    """The failure mode STU-890 exists to end: an empty list means "nobody is here"."""

    def boom(text, from_address=None):
        raise RuntimeError("weights are gone")

    monkeypatch.setattr(anonymize, "_local_person_spans", boom)
    with pytest.raises(RuntimeError, match="weights are gone"):
        detector.remote_spans("Caroline")


def test_a_malformed_request_is_answered_with_an_error_not_a_crash(served):
    reply = json.loads(_ask(served.path, b"not json\n"))
    assert "error" in reply and "spans" not in reply
    # And the server is still serving.
    assert detector.remote_spans("Caroline") == [(0, 8, "person")]


def test_a_server_that_dies_mid_request_falls_back(monkeypatch, tmp_path):
    """Closing without answering is no answer, not "this text holds nobody"."""
    path = tmp_path / "mute.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def hang_up():
        listener.accept()[0].close()

    threading.Thread(target=hang_up, daemon=True).start()
    monkeypatch.setenv(detector.SOCKET_ENV, str(path))
    monkeypatch.setattr(anonymize, "_local_person_spans", lambda text, from_address=None: [(1, 2, "person")])
    with listener:
        assert anonymize._person_spans("Caroline") == [(1, 2, "person")]


def test_a_wedged_server_does_not_hold_the_caller_forever(monkeypatch, served):
    monkeypatch.setattr(detector, "_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(anonymize, "_local_person_spans", lambda text, from_address=None: time.sleep(10))
    started = time.monotonic()
    assert detector.remote_spans("Caroline") is None
    assert time.monotonic() - started < 5


def test_concurrent_callers_are_served_one_at_a_time(monkeypatch, served):
    """The fan-out is parallel; one detector cannot run two forward passes at once."""
    concurrent, peak = [], []

    def one_at_a_time(text, from_address=None):
        concurrent.append(1)
        peak.append(len(concurrent))
        try:
            return [(0, len(text), "person")]
        finally:
            concurrent.pop()

    monkeypatch.setattr(anonymize, "_local_person_spans", one_at_a_time)
    threads = [
        threading.Thread(target=lambda: detector.remote_spans("Caroline"))
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(peak) == 1


def test_the_socket_and_its_env_var_are_gone_once_the_service_exits(monkeypatch):
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    with detector.Detector() as service:
        path = service.path
        assert os.environ[detector.SOCKET_ENV] == str(path)
    assert detector.SOCKET_ENV not in os.environ
    assert not path.exists() and not path.parent.exists()


def test_the_tokenizer_reaches_the_service(served):
    """The whole point: no caller of `Tokenizer` knows the detector moved."""
    assert anonymize.Tokenizer().anonymize("Caroline") == "PERSON_X"
    assert served.ran_on and threading.main_thread() not in served.ran_on


def test_the_sender_domain_crosses_the_socket(monkeypatch):
    """STU-1273: the push path shares this server, so the veto must ride the wire too."""
    seen = []

    def spans(text, from_address=None):
        seen.append(from_address)
        return [(0, len(text), "person")]

    monkeypatch.setattr(anonymize, "_local_person_spans", spans)
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    with detector.Detector():
        detector.remote_spans("emagine", from_address="no_reply@emagine.org")
    assert seen == ["no_reply@emagine.org"]
