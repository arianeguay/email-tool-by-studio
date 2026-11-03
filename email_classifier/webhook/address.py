"""Where the webhook listens — and therefore where the health check looks (STU-978).

The server binds `$WEBHOOK_HOST:$WEBHOOK_PORT`; `scripts/check_webhook_health.py`
polls a URL. Held apart, the two disagree the first time anyone moves the port —
and on a host that has already spent 8080 on something else, moving it is the
install. A check aimed at a port nothing listens on exits 2, its "endpoint
unreachable" code, on every hourly run forever: the same signal a genuinely dead
webhook produces, from the one job that exists to notice silence. Nothing else
reports it either, the webhook answering perfectly on its real port.

So both read the port here, and setting `WEBHOOK_PORT` alone is enough.
`WEBHOOK_HEALTH_URL` stays for the case the two really are different addresses —
a check running from another machine.

The pinned default survived that removal as the *fallback*, which is the same lie
one level down: `configured_health_url()` now refuses rather than compose an
address nobody claimed (STU-1087). `listen_port()` keeps it — a bind fails loudly,
only the asking side can be silently wrong.

That refusal names the wrong condition on the serving host since STU-1116 put
`/etc/email-classifier` at `700 email-classifier` (STU-1122): the port *is* set,
in the file `.env` links to, and the caller simply cannot open it. Make's
`-include .env` cannot tell that from a missing file — both arrive as nothing —
so the refusal below asks the filesystem which of the two it is looking at.
"""

import os
import pwd
from pathlib import Path

from email_classifier.webhook import deploy

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

# Binds that name every interface rather than one to connect to. A wildcard is a
# fine thing to listen on and not an address to poll, so the check falls back to
# loopback; any other bind is the only address the socket answers on, loopback
# included, so it is what the check must ask.
_WILDCARD = {"", "0.0.0.0", "::", "*"}

# The file the Makefile's `-include` reads and the README tells an operator to fill.
# `deploy.REPO_ROOT` rather than a second statement of the same path: the two would
# drift the first time the package moves.
ENV_FILE = deploy.REPO_ROOT / ".env"


def listen_host() -> str:
    """The interface the webhook binds."""
    return os.environ.get("WEBHOOK_HOST", DEFAULT_HOST)


def listen_port() -> int:
    """The port the webhook binds."""
    return int(os.environ.get("WEBHOOK_PORT", str(DEFAULT_PORT)))


def health_url() -> str:
    """The `/healthz` URL of the webhook this environment would start."""
    host = listen_host()
    if host in _WILDCARD:
        host = DEFAULT_HOST
    elif ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{listen_port()}/healthz"


def _owner(path: Path) -> str | None:
    """The account owning `path`, or the nearest ancestor this process can stat.

    On the serving host the unreadable thing is the *directory*: STU-1116 put
    `/etc/email-classifier` at `700 email-classifier`, so the file inside it
    cannot be stat'd at all while the directory itself can. Walking up names the
    account that is actually denying the read, where stopping at the file would
    name nobody.

    `#<uid>` for a uid with no passwd entry, which is what `sudo -u` takes: the
    answer has to be one the repair can be written with. None where nothing up
    the tree can be stat'd, so the caller says it does not know rather than
    putting a name it invented into a command line.
    """
    for candidate in (path, *path.parents):
        try:
            uid = candidate.stat().st_uid
        except OSError:
            continue
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return f"#{uid}"
    return None


def _unreadable_env() -> str | None:
    """Why the environment is empty, when the answer is a `.env` we cannot open.

    Make's `-include .env` swallows the difference: a missing file and an
    unreadable one both produce nothing, so the port arrives unset either way and
    the refusal below names the condition the *check* can see rather than the one
    the host is in. Measured on the serving host 2026-08-30, after the STU-1116 cutover:
    `make check-webhook` as the operator printed "Neither WEBHOOK_HEALTH_URL nor
    WEBHOOK_PORT is set" while `WEBHOOK_PORT=18080` sat in
    `/etc/email-classifier/env`, which `.env` links to. The repair that message
    names is the wrong one — an operator following it exports a port into their own
    shell, plausibly the 8080 the message puts in front of them, which on that host
    is the docker-proxy STU-1087 was written against.

    `lstat` and not `exists()`: the link on that host resolves into a directory
    this process may not enter, so following it answers "no such file" for a file
    that is plainly there. Only `PermissionError` counts — a dangling link raises
    `FileNotFoundError`, which really is "nothing said the address", and claiming
    an ownership problem there would be the same wrong cause one door over.
    """
    try:
        ENV_FILE.lstat()
    except OSError:
        return None
    target = Path(os.readlink(ENV_FILE)) if ENV_FILE.is_symlink() else ENV_FILE
    try:
        ENV_FILE.read_bytes()
    except PermissionError:
        pass  # the one condition this function exists to name
    except OSError:
        return None
    else:
        return None
    named = str(ENV_FILE) if target == ENV_FILE else f"{ENV_FILE} -> {target}"
    owner = _owner(target)
    whose = (
        f"it belongs to {owner}"
        if owner
        else "it belongs to an account this process cannot name either"
    )
    repair = (
        f"`sudo -u {owner} make check-webhook`"
        if owner
        else "`sudo -u <that account> make check-webhook`"
    )
    return (
        f"WEBHOOK_PORT would come from {named}, which is there and this process "
        f"cannot read: {whose}, and `make`'s `-include .env` drops a file it cannot "
        "open without a word, so every key in it arrives unset. Run the target under "
        f"that account — {repair} — rather than exporting a port into your own shell."
    )


def configured_health_url() -> str:
    """The `/healthz` to ask, override included.

    `WEBHOOK_HEALTH_URL` names a webhook this environment does not start: the
    staging host, for a check or a delivery round run from a laptop. `or`, not a
    default — .env.example carries the key empty and Make exports it that way,
    so an unset override arrives as "" rather than absent.

    Naming a host to forward *through* and no address to forward *to* is the one
    combination that cannot mean anything (STU-1027): the fallback describes the
    webhook this machine would start, and forwarding that to another host dials a
    port chosen by the wrong side. It fails as "no webhook answering", which is
    the misreading the forward exists to end, so it is refused instead.

    Naming no port at all is the same shape, and it is how STU-978 came back
    through another door (STU-1087). `WEBHOOK_PORT` reaches a Python process only
    through the Makefile's `-include .env`, so every other invocation — a bare
    `python scripts/check_webhook_health.py`, and any worktree, which has no
    `.env` by design — composed the port out of `DEFAULT_PORT`. Measured on
    the serving host 2026-08-29: 8080 there is a docker-proxy, which answers 404, so the
    check reported "webhook unreachable" about a webhook answering perfectly on
    18080 — and a 404 is *positive* evidence that something is listening and it is
    not us, reported as absence. A check that has not been told where to look must
    say so, never invent an address and then blame the webhook for not being at it.

    `listen_port()` keeps the default: the two sides are not symmetric. A bind
    either succeeds or fails loudly on `EADDRINUSE`, so only the asking side can
    be silently wrong.

    Which of the two ways the environment came up empty is `_unreadable_env`'s
    answer, and it stays under the same exit code rather than taking a new one
    (STU-1122). The family's rule is that the dedup keys on the code alone, so a
    condition folded into another goes unreported behind it — but that rule is
    about faults a cron run has to page for, and this one no cron path can reach:
    on the serving host cron already runs as the account that can read the file.
    What is left is a hand-run diagnostic read by whoever typed it, where the
    message is the whole of what they act on and a second number buys nothing.
    Both conditions are the same verdict — the invocation never got as far as
    asking the webhook anything — repaired at the invocation, not on the host.
    """
    override = os.environ.get("WEBHOOK_HEALTH_URL")
    if not override and os.environ.get("WEBHOOK_SSH_HOST"):
        raise RuntimeError(
            "WEBHOOK_SSH_HOST names a host to reach /healthz through, but "
            "WEBHOOK_HEALTH_URL does not say which address to reach there. Set "
            "it to the URL as that host sees it, e.g. "
            "http://127.0.0.1:18080/healthz."
        )
    # Empty counts as absent for the same reason the override does: .env carries
    # keys empty and Make exports them that way. It is also the value that would
    # otherwise reach `int()` in `listen_port()` and die as a ValueError.
    if not override and not os.environ.get("WEBHOOK_PORT"):
        raise RuntimeError(
            _unreadable_env()
            or "Neither WEBHOOK_HEALTH_URL nor WEBHOOK_PORT is set, so nothing says "
            f"which address to ask; {DEFAULT_PORT} is a guess, and a host that "
            "answers it with something else turns the guess into a false "
            "'webhook unreachable'. Set WEBHOOK_PORT to the port this machine's "
            "webhook binds, or WEBHOOK_HEALTH_URL to the /healthz of a webhook it "
            "does not start."
        )
    return override or health_url()
