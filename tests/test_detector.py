"""The shared detector (STU-938): one loaded model for the processes a run spawns."""

import json
import os
import socket
import threading
import time

import pytest

from email_classifier import anonymize, detector


# The child runs these, so they cross by pickle and must live at module level.
def whole_text(text, from_address=None):
    return [(0, len(text), "person")]


def labelled_by_pid(text, from_address=None):
    """The process that answered, so a test can tell the child from the caller."""
    return [(0, len(text), str(os.getpid()))]


def echo_sender(text, from_address=None):
    return [(0, len(text), from_address)]


def weights_gone(text, from_address=None):
    raise RuntimeError("weights are gone")


def wedged(text, from_address=None):
    time.sleep(10)


@pytest.fixture
def serve(monkeypatch):
    """Start a detector service whose child runs `spans`."""
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    services = []

    def start(spans=whole_text, **kwargs):
        service = detector.Detector(spans, **kwargs).__enter__()
        services.append(service)
        return service

    yield start
    for service in services:
        service.__exit__(None, None, None)


@pytest.fixture
def served(serve):
    return serve()


def _ask(path, payload):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    with client, client.makefile("rwb") as stream:
        stream.write(payload)
        stream.flush()
        return stream.readline()


def test_serves_the_spans_the_local_detector_would_have_found(served):
    assert detector.remote_spans("Caroline") == [(0, 8, "person")]


def test_person_spans_prefers_the_service(serve):
    serve(labelled_by_pid)
    [(_, _, answered_by)] = anonymize._person_spans("Caroline")
    # Answered by the child, so the caller never touched the weights itself.
    assert answered_by != str(os.getpid())


def test_person_spans_loads_locally_when_nobody_serves(monkeypatch):
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    monkeypatch.setattr(anonymize, "_local_person_spans", lambda text, from_address=None: [(1, 2, "person")])
    assert anonymize._person_spans("Caroline") == [(1, 2, "person")]


def test_a_stale_socket_falls_back_instead_of_failing(monkeypatch, tmp_path):
    monkeypatch.setenv(detector.SOCKET_ENV, str(tmp_path / "gone.sock"))
    monkeypatch.setattr(anonymize, "_local_person_spans", lambda text, from_address=None: [(1, 2, "person")])
    assert anonymize._person_spans("Caroline") == [(1, 2, "person")]


def test_a_failing_detector_raises_rather_than_reporting_no_names(serve):
    """The failure mode STU-890 exists to end: an empty list means "nobody is here"."""
    serve(weights_gone)
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


def test_a_wedged_server_does_not_hold_the_caller_forever(monkeypatch, serve):
    service = serve(wedged)
    monkeypatch.setattr(detector, "_TIMEOUT_SECONDS", 2)
    started = time.monotonic()
    assert detector.remote_spans("Caroline") is None
    assert time.monotonic() - started < 8
    # The wedged child is killed, not left holding every later request behind it.
    deadline = time.monotonic() + 10
    while service.worker.alive and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not service.worker.alive


def test_concurrent_callers_each_get_their_own_answer(served):
    """The fan-out is parallel and shares one pipe to the child: no reply may cross."""
    answers = {}

    def ask(n):
        answers[n] = detector.remote_spans("x" * n)

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(1, 9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert answers == {n: [(0, n, "person")] for n in range(1, 9)}


def test_the_child_exits_once_idle_and_comes_back_on_demand(serve):
    """HOM-1684: the model is held between the calls of one run, not between emails."""
    service = serve(idle_seconds=0.5)
    assert not service.worker.alive
    assert detector.remote_spans("Caroline") == [(0, 8, "person")]
    assert service.worker.alive
    deadline = time.monotonic() + 10
    while service.worker.alive and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not service.worker.alive
    assert detector.remote_spans("Caroline") == [(0, 8, "person")]


def test_the_socket_its_env_var_and_the_child_are_gone_once_the_service_exits(monkeypatch):
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    with detector.Detector(whole_text) as service:
        path = service.path
        assert os.environ[detector.SOCKET_ENV] == str(path)
        detector.remote_spans("Caroline")
        assert service.worker.alive
    assert detector.SOCKET_ENV not in os.environ
    assert not path.exists() and not path.parent.exists()
    assert not service.worker.alive


def test_the_tokenizer_reaches_the_service(monkeypatch, served):
    """The whole point: no caller of `Tokenizer` knows the detector moved."""
    monkeypatch.setattr(anonymize, "_model", lambda: pytest.fail("loaded in the caller"))
    assert anonymize.Tokenizer().anonymize("Caroline") == "PERSON_X"


def test_the_sender_domain_crosses_the_socket(serve):
    """STU-1273: the push path shares this server, so the veto must ride the wire too."""
    serve(echo_sender)
    assert detector.remote_spans("emagine", from_address="no_reply@emagine.org") == [
        (0, 7, "no_reply@emagine.org")
    ]
