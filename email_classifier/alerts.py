"""Out-of-band alerts for a failure no retry can clear (STU-930).

A headless worker's log is read once someone notices the mailbox went quiet,
which is days after a lost OAuth grant stopped the classification. `notify`
POSTs the message as plain text to `$ALERT_WEBHOOK_URL` — an ntfy topic, a Home
Assistant webhook, a Slack or Discord hook, whichever the host wires up.

Pushover is the second channel (STU-1066), and it is what the serving host actually has:
`ALERT_WEBHOOK_URL` was never set there, so every alert in the system was a
CRITICAL line in journald and nothing else, on the only host that runs
unattended. Its API is form-encoded rather than raw-bodied, so it cannot be
reached by pointing the first channel at it — a raw-body POST is a 400.

Both are tried, and each is independent: a host wiring up both gets both, and a
channel that is down does not take the other with it. Neither configured, the
alert is the CRITICAL log line alone.
"""

import logging
import os
import subprocess

_log = logging.getLogger("email_classifier.alerts")

_TIMEOUT = 10

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
# Pushover 400s a longer message rather than truncating it, which would turn every
# alert carrying a traceback into a silent delivery failure — the exact silence
# this module exists to end.
PUSHOVER_MAX_MESSAGE = 1024
_PUSHOVER_TITLE = "email-classifier"


def notify(message: str) -> None:
    """Log the alert at CRITICAL and send it to every channel this host configures.

    Never raises: every caller is already handling a failure, and an unreachable
    alert endpoint must not replace that failure with a different one.
    """
    _log.critical(message)
    for send in (_post_webhook, _post_pushover):
        try:
            send(message)
        except Exception:
            _log.error(
                "%s failed — the alert above was not delivered that way",
                send.__name__, exc_info=True,
            )


def _post_webhook(message: str) -> None:
    url = os.environ.get("ALERT_WEBHOOK_URL")
    if not url:
        return
    import requests

    requests.post(
        url, data=message.encode("utf-8"), timeout=_TIMEOUT
    ).raise_for_status()


def _post_pushover(message: str) -> None:
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not (token and user):
        return
    import requests

    _journal(f"envoi (priorite 0) : {_PUSHOVER_TITLE}\n{message}")
    try:
        requests.post(
            PUSHOVER_URL,
            data={
                "token": token,
                "user": user,
                "title": _PUSHOVER_TITLE,
                "message": message[:PUSHOVER_MAX_MESSAGE],
            },
            timeout=_TIMEOUT,
        ).raise_for_status()
    except Exception as exc:
        _journal(f"refuse : {_PUSHOVER_TITLE} : {exc}", priority="user.err")
        raise
    _journal(f"accepte : {_PUSHOVER_TITLE}")


def _journal(line: str, priority: str = "user.notice") -> None:
    """Record a Pushover send where a host's own `pushover` wrapper records its sends.

    A host reviewing what it pushed reads one syslog identifier; a direct API call
    that leaves nothing there cannot be told apart from an alert never sent.
    """
    try:
        subprocess.run(
            ["logger", "-t", "pushover", "-p", priority, "--", line],
            check=False, timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        _log.warning("could not record the Pushover send in the system log")
