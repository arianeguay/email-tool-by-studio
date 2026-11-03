"""The webhook as a service: one WSGI process, and a unit that matches it (STU-928)."""

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("waitress")

from email_classifier.fetch.grant import GrantLost
from email_classifier.webhook import server as webhook_server

REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"


def test_main_serves_through_waitress(monkeypatch):
    """Flask's dev server is single-threaded and does not survive a crash."""
    import waitress

    served = {}
    monkeypatch.setattr(webhook_server, "create_app", lambda: "the-app")
    monkeypatch.setattr(waitress, "serve", lambda app, **kw: served.update(app=app, **kw))
    monkeypatch.setenv("WEBHOOK_HOST", "0.0.0.0")
    monkeypatch.setenv("WEBHOOK_PORT", "18080")

    assert webhook_server.main() == 0
    assert served == {"app": "the-app", "host": "0.0.0.0", "port": 18080}


def test_the_detector_is_serving_while_the_app_is(monkeypatch):
    """The whole of STU-938 rides on one line in `main()`, and nothing else asserts it.

    Checked from inside `serve`, because that is the only moment it has to be true: the
    service is a context manager, so a socket that exists before or after says nothing.
    """
    import waitress

    from email_classifier import detector

    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    monkeypatch.setattr(webhook_server, "create_app", lambda: "the-app")
    while_serving = {}

    def serve(app, **kw):
        path = os.environ.get(detector.SOCKET_ENV)
        while_serving.update(path=path, answers=detector.remote_spans("Caroline"))

    monkeypatch.setattr(waitress, "serve", serve)
    monkeypatch.setattr(
        "email_classifier.anonymize._local_person_spans",
        lambda text, from_address=None: [(0, 8, "person")],
    )

    assert webhook_server.main() == 0
    # A live socket, not merely a variable: a detector nothing can reach would leave
    # every child loading its own and look identical from the environment alone.
    assert while_serving["answers"] == [(0, 8, "person")]
    assert detector.SOCKET_ENV not in os.environ
    assert not Path(while_serving["path"]).exists()


def test_a_startup_the_network_broke_asks_to_be_restarted(monkeypatch):
    """A host still booting resolves no names; the next start wins that race."""
    import requests

    def unreachable():
        raise requests.exceptions.ConnectionError("Failed to resolve 'login.microsoftonline.com'")

    monkeypatch.setattr(webhook_server, "create_app", unreachable)

    assert webhook_server.main() == 75


def test_a_lost_grant_still_asks_not_to_be_restarted(monkeypatch):
    """No retry restores a revoked token; restarting would bury the alert."""
    alerted = []

    def revoked():
        raise GrantLost("the cached Graph grant no longer redeems silently")

    monkeypatch.setattr(webhook_server, "create_app", revoked)
    monkeypatch.setattr(webhook_server.alerts, "notify", alerted.append)

    assert webhook_server.main() == 1
    assert alerted


def test_the_install_target_names_the_service_user_rather_than_the_caller():
    """`id -un` made whoever ran the install the author of every agent prompt.

    Under `claude-code` each agent call is a `claude` process reading that account's
    ~/.claude into its system prompt, so installing from a login session pointed the
    club's drafter at the operator's notes (STU-1116).
    """
    makefile = MAKEFILE.read_text()
    install = re.search(
        r"^install-webhook-service:.*?(?=^\S)", makefile, re.M | re.S
    ).group(0)

    assert "id -un" not in install
    assert "s|@USER@|$(SERVICE_USER)|" in install
    assert re.search(r"^SERVICE_USER \?= \S+$", makefile, re.M)


def test_the_install_target_refuses_an_account_that_can_log_in():
    """The check is the mechanism; without it the default is a suggestion.

    A login shell is the half of "is this a person's home" a machine can answer, and
    the target refuses on it rather than warning — a unit installed under a human
    account is the failure the unit exists to prevent.
    """
    install = re.search(
        r"^install-webhook-service:.*?(?=^\S)", MAKEFILE.read_text(), re.M | re.S
    ).group(0)

    assert "getent passwd $(SERVICE_USER)" in install
    assert "nologin" in install
    assert "exit 1" in install


def test_every_checkout_write_in_a_deploy_runs_as_the_service_account():
    """A deploy is an operator action, but its writes must land as the serving account.

    Missed on one line, that step leaves a root- or operator-owned file in a checkout the
    service then cannot write — and the failure surfaces at the next run, not at the
    deploy. The systemd lines are the deliberate exception: the service account is given
    no sudo, since a `sudo tee` of a unit file is a path to root (STU-1116).
    """
    makefile = MAKEFILE.read_text()
    recipes = "".join(
        re.search(rf"^{t}:.*?(?=^\S)", makefile, re.M | re.S).group(0)
        for t in ("pull-webhook", "pull-webhook-here", "deploy-webhook")
    )
    # The privileged half, and the handoff into it: systemd, the unit install, and the
    # sub-make that reaches both. Everything else in a deploy touches the checkout.
    # The dispatch is the operator's too, and for the same reason as the ssh it may run:
    # it decides which machine this is and writes nothing on either. What it hands off to
    # is `pull-webhook-here`, whose lines are checked here like any other.
    privileged = ("sudo systemctl", "$(MAKE) install-webhook-service",
                  "$(MAKE) deploy-webhook", "@scripts/pull_webhook.sh")
    # Logical lines, not physical: a backslash continuation is the same command, and the
    # deploy's verification step is several of them (STU-1122). Split physically, its
    # `case` arms would read as commands of their own running unprivileged.
    logical: list[str] = []
    for line in recipes.splitlines():
        if not line.startswith("\t"):
            continue
        if logical and logical[-1].endswith("\\"):
            logical[-1] = logical[-1][:-1] + " " + line.strip()
        else:
            logical.append(line.strip())
    writes = [
        line for line in logical if not line.startswith(("#",) + privileged)
    ]

    assert writes
    assert all(line.startswith("$(AS_SERVICE) ") for line in writes), writes
    assert re.search(r"^AS_SERVICE \?=.*sudo -u \$\(SERVICE_USER\)", makefile, re.M)
