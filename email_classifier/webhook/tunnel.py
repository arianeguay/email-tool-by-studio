"""Reach a webhook that only answers on its own host's loopback (STU-1027).

The staging webhook binds `127.0.0.1`, and the remote-managed cloudflared ingress
routes `/graph` and nothing else, so `/healthz` has no address a laptop can dial:
`<serving-host>:18080` refuses the connection and the public host 404s. `make deliver`
asks that endpoint whether the round it just sent produced a notification
(STU-1019), so from the machine a round is actually sent from the question could
not be asked at all — every round printed the `no webhook answering` line, which
is the line reserved for a round that failed to notify.

`WEBHOOK_SSH_HOST` names the host to reach it through, and the forward is opened
for the round and closed with it. The alternative was serving `/healthz` on a path
the ingress carries, which publishes the subscription id, its expiry and the
deployed commit to anyone who asks; a forward discloses nothing and needs no
change outside this repo.

`WEBHOOK_HEALTH_URL` keeps naming the address as the *serving* host sees it —
what moves is only how this machine gets there. Unset `WEBHOOK_SSH_HOST` and
nothing is opened, which is the cron on the host itself, where the URL already
resolves.
"""

import os
import socket
import subprocess
import time
from contextlib import contextmanager
from typing import Iterator
from urllib.parse import urlsplit, urlunsplit

FORWARD_TIMEOUT = 15.0


class ForwardFailed(RuntimeError):
    """A forward was asked for and did not come up.

    Raised rather than degraded to the un-forwarded URL, which this module has
    just established nothing here can dial: every caller then reports its own
    unreachable case, blaming the webhook for ssh. What each caller does about it
    differs — a delivery round says so and carries on, the health check has an
    exit code for it — and only the caller knows which.
    """


@contextmanager
def reachable(url: str) -> Iterator[str]:
    """`url` as an address this machine can dial, for the life of the block."""
    ssh_host = os.environ.get("WEBHOOK_SSH_HOST")
    if not ssh_host:
        yield url
        return

    parts = urlsplit(url)
    local_port = _free_port()
    process = subprocess.Popen(
        [
            "ssh", "-N",
            # Never prompt: a delivery round is unattended past this point, and a
            # password prompt on a hidden ssh is indistinguishable from a hang.
            "-o", "BatchMode=yes",
            # Without it ssh happily stays up with no forward, and the failure
            # would surface as "no webhook answering" — the wrong diagnosis.
            "-o", "ExitOnForwardFailure=yes",
            "-L", f"{local_port}:{parts.hostname}:{parts.port or 80}",
            ssh_host,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        if not _await_forward(local_port, process):
            raise ForwardFailed(
                f"no forward to {url} through {ssh_host}: {_why(process)}"
            )
        yield urlunsplit(parts._replace(netloc=f"127.0.0.1:{local_port}"))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Raised out of a `finally`, it would replace whatever the round was
            # already failing with — an ssh that ignores SIGTERM is not the news.
            process.kill()
            process.wait()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _await_forward(port: int, process: subprocess.Popen) -> bool:
    """Whether ssh brought the forward up. It says so only by accepting."""
    deadline = time.monotonic() + FORWARD_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _why(process: subprocess.Popen) -> str:
    if process.poll() is None:
        return f"still no listener after {FORWARD_TIMEOUT:.0f}s"
    stderr = process.stderr.read().strip()
    return stderr.splitlines()[-1] if stderr else f"ssh exited {process.returncode}"
