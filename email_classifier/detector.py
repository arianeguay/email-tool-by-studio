"""The name detector, held once and shared by the processes a push run spawns.

`anonymize._model()` already caches the detector for the life of a process, which is
the whole batch on `make run`. The push path has no such process: a notification that
owes a reply spawns `scripts/drafts_pre.py`, then a `org_memory-read` and a
`mail_precedents-search_mail` per drafting agent, and each is a short-lived Python that
loads GLiNER from scratch to do a fraction of a second of work.

Measured on the serving host (FX-8350, warm Hugging Face cache, `.[ner]` installed), per process:

    import torch                 1.6 s
    import gliner                3.4 s
    GLiNER.from_pretrained      19.9 s
    anonymize() a 600-char window 0.33 s
    peak RSS during the load     2.8 GB, settling to 1.76 GB resident

So a single-message notification pays 20-25 s and a 2.8 GB allocation spike once per
process, one to three times, for under a second of detection.

This module holds one loaded detector behind a Unix socket. The client is
`anonymize._person_spans`, so every caller in the tree reaches it without knowing:
the tokenizer, the club-memory redaction and the precedent sweep all funnel there.

Two properties it must have, and both shape the code below:

- **A server that cannot answer must not answer "no names".** An empty span list is a
  valid reply meaning the text holds nobody, and returning it for a failure would put
  every name in that text through to the model in the clear — the failure STU-890
  exists to end. So the client raises on anything it did not understand, and its one
  caller falls back to loading the detector in-process. Slow is the failure mode; a
  leak is not one.
- **The socket is per-server, not a well-known path.** A directory `mkdtemp` creates at
  0700 holds it, so two webhooks on one host cannot collide and nothing else on the
  box can talk to it.

The model itself lives in a child process that exits after `_IDLE_SECONDS` without a
request (HOM-1684). Held in the webhook's own process it never left: on apollon the
webhook sat at 790 MB resident for days to classify a few dozen emails, and dropping the
cached model would not have given that back, since `import torch` alone stays mapped.
A child that exits returns all of it. The next request after an idle exit pays the
~25 s load again, once, which is what every request after a restart already paid.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import shutil
import socket
import socketserver
import tempfile
import threading
import time
from pathlib import Path

_log = logging.getLogger("email_classifier.detector")

# Inherited by every `studio` child the webhook spawns, and by the tool subprocesses
# those children spawn in turn — which is the whole point: they are the processes
# paying the load today.
SOCKET_ENV = "EMAIL_CLASSIFIER_DETECTOR_SOCKET"

# A window is at most `anonymize._WINDOW` characters, so a request is small. The cap is
# here to bound a client that is not one of ours, not to fit the traffic.
_MAX_REQUEST = 1 << 20

# Generous, because the honest slow cases are real: the first caller after a restart
# waits out the ~25 s load, and inference is serialized, so a caller behind a full
# fan-out waits its turn. What this bounds is the dishonest case — a server wedged
# mid-predict, which without a deadline holds every drafting agent open forever.
_TIMEOUT_SECONDS = 120.0

# A drafting run's fan-out reaches the detector within a few minutes of its first call,
# so ten idle minutes keeps one load per notification without holding it between them.
_IDLE_SECONDS = 600.0


def _work(conn, spans) -> None:
    """The child's loop: one request in, one reply out, until the parent hangs up."""
    while True:
        try:
            text, from_address = conn.recv()
        except EOFError:
            return
        try:
            reply = (True, spans(text, from_address))
        except Exception as exc:
            reply = (False, f"{type(exc).__name__}: {exc}")
        conn.send(reply)


class _Worker:
    """The child holding the model: started on the first request, stopped once idle.

    `spans` is what the child runs, else `anonymize._local_person_spans` as the parent
    sees it when the child starts, so a test that replaces it there reaches the child. It
    crosses by pickle, so it is a module-level function.
    """

    def __init__(self, spans=None, idle_seconds: float = _IDLE_SECONDS) -> None:
        self._spans = spans
        self._idle = idle_seconds
        # One forward pass at a time, and one request on the pipe at a time: the fan-out
        # is parallel, and two callers sharing the pipe would read each other's replies.
        self._lock = threading.Lock()
        self._process = None
        self._conn = None
        self._last = 0.0
        self._timer = None

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def __call__(self, text: str, from_address: str | None) -> list:
        with self._lock:
            if not self.alive:
                self._start()
            self._conn.send((text, from_address))
            if not self._conn.poll(_TIMEOUT_SECONDS):
                # Wedged mid-predict: the caller has given up already, and a child that
                # never answers would hold every later request behind it.
                self._stop("did not answer within %ss" % _TIMEOUT_SECONDS)
                raise TimeoutError("detector did not answer")
            try:
                ok, value = self._conn.recv()
            except EOFError:
                self._stop("exited mid-request")
                raise
            self._last = time.monotonic()
            self._arm()
        if not ok:
            raise RuntimeError(value)
        return value

    def _start(self) -> None:
        # Spawned, not forked: the webhook is threaded by then, and a fork copies
        # whatever lock another thread held at that instant.
        # Imported here and not at module scope: `anonymize` reaches back into this
        # module for its client half.
        from email_classifier import anonymize

        spans = self._spans or anonymize._local_person_spans
        context = multiprocessing.get_context("spawn")
        self._conn, child = context.Pipe()
        self._process = context.Process(
            target=_work, args=(child, spans), name="detector", daemon=True
        )
        self._process.start()
        child.close()
        _log.info("detector loading in child process %d", self._process.pid)

    def _arm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._idle, self._release)
        self._timer.daemon = True
        self._timer.start()

    def _release(self) -> None:
        with self._lock:
            if self.alive and time.monotonic() - self._last >= self._idle:
                self._stop("idle for %ds" % self._idle)

    def _stop(self, why: str) -> None:
        if self._process is None:
            return
        _log.info("detector child process %d released: %s", self._process.pid, why)
        self._conn.close()
        self._process.join(5)
        if self._process.is_alive():
            self._process.kill()
            self._process.join()
        self._process = None

    def close(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._stop("service exiting")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        line = self.rfile.readline(_MAX_REQUEST)
        if not line:
            return
        try:
            request = json.loads(line)
            text = request["text"]
            spans = self.server.worker(text, request.get("from_address"))
            reply = {"spans": spans}
        except Exception as exc:
            # Reported as a failure, never as an empty span list: the client raises on
            # this, and what it must never do is read it as "this text holds nobody".
            _log.exception("detector request failed")
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Detector:
    """A loaded detector reachable over a Unix socket, for the life of this process.

    Lazy on purpose, in both directions: nothing loads until a name must be found, and
    the child holding it exits once nothing has asked for `idle_seconds`.
    """

    def __init__(self, spans=None, idle_seconds: float = _IDLE_SECONDS) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="email-classifier-detector-"))
        self.path = self._dir / "detector.sock"
        self._server = _Server(str(self.path), _Handler)
        self._server.worker = self.worker = _Worker(spans, idle_seconds)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="detector", daemon=True
        )

    def __enter__(self) -> "Detector":
        self._thread.start()
        os.environ[SOCKET_ENV] = str(self.path)
        _log.info("detector service listening on %s", self.path)
        return self

    def __exit__(self, *exc) -> None:
        os.environ.pop(SOCKET_ENV, None)
        self._server.shutdown()
        self._server.server_close()
        self.worker.close()
        shutil.rmtree(self._dir, ignore_errors=True)


def remote_spans(
    text: str, from_address: str | None = None
) -> list[tuple[int, int, str]] | None:
    """The server's spans, or None when there is no server to ask.

    None means "nobody is serving" — the caller loads the detector itself. Anything
    else that goes wrong raises, because a detector that answered badly is not the same
    as no detector, and the difference is worth a line in the log.
    """
    path = os.environ.get(SOCKET_ENV)
    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_TIMEOUT_SECONDS)
            client.connect(path)
            with client.makefile("rwb") as stream:
                request = {"text": text, "from_address": from_address}
                stream.write(json.dumps(request).encode() + b"\n")
                stream.flush()
                line = stream.readline(_MAX_REQUEST)
        if not line:
            raise ConnectionError("closed without answering")
    except OSError as exc:
        # A stale variable outliving its server, a server wedged past the deadline, or
        # one that died mid-request: the run is correct either way, and saying so once
        # beats a run that is mysteriously back to its old speed.
        _log.warning("detector service at %s is not answering (%s)", path, exc)
        return None
    reply = json.loads(line)
    if "error" in reply:
        raise RuntimeError(f"detector service failed: {reply['error']}")
    return [(start, end, label) for start, end, label in reply["spans"]]
