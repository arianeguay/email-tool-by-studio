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
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import socketserver
import tempfile
import threading
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


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        # Imported here and not at module scope: `anonymize` reaches back into this
        # module for its client half.
        from email_classifier import anonymize

        line = self.rfile.readline(_MAX_REQUEST)
        if not line:
            return
        try:
            request = json.loads(line)
            text = request["text"]
            with self.server.lock:
                spans = anonymize._local_person_spans(text, request.get("from_address"))
            reply = {"spans": spans}
        except Exception as exc:
            # Reported as a failure, never as an empty span list: the client raises on
            # this, and what it must never do is read it as "this text holds nobody".
            _log.exception("detector request failed")
            reply = {"error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write(json.dumps(reply).encode() + b"\n")


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # One detector, one forward pass at a time. The fan-out is parallel, so without
        # it two drafting agents share a model mid-predict; serializing costs a caller
        # 0.33 s a turn. Per instance, not per class: a class attribute would make two
        # servers in one process wait on each other, which is only ever the tests.
        self.lock = threading.Lock()


class Detector:
    """A loaded detector reachable over a Unix socket, for the life of this process.

    Lazy on purpose. Loading at startup would put 1.76 GB permanently behind a webhook
    on a host that also runs the media stack, including on the mailboxes that never
    escalate and never draft. Loading on the first request spends the same memory only
    once something actually needed a name found, and costs one notification the 20 s
    that every notification pays today.
    """

    def __init__(self) -> None:
        self._dir = Path(tempfile.mkdtemp(prefix="email-classifier-detector-"))
        self.path = self._dir / "detector.sock"
        self._server = _Server(str(self.path), _Handler)
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
