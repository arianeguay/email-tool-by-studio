"""What `scripts/fetch_outlook.py` asks for when the grant is dead (STU-1173).

The script built its own session and so defaulted to `interactive=True`, where a
grant that no longer redeems falls back to `acquire_token_interactive` — a local
HTTP server with msal's default `timeout=None`. Measured 2026-09-01 on msal
1.37.0: it waits forever either way, and it is silent whenever `webbrowser.open`
reports success, which is the twin of the Gmail hang STU-948 and STU-1163 closed.
`get_token_provider` already raises `GrantLost` under `interactive=False`
(STU-930); what was missing is that a script importing `email_classifier.fetch.outlook`
directly is not on `providers._interactive`'s path, so `EMAIL_CLASSIFIER_HEADLESS`
could not reach it.
"""

import importlib.util
from pathlib import Path

import pytest

from email_classifier import providers
from email_classifier.fetch.grant import GrantLost

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fetch_outlook():
    path = REPO_ROOT / "scripts" / "fetch_outlook.py"
    spec = importlib.util.spec_from_file_location("scripts_fetch_outlook", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def asked(monkeypatch):
    """What interactivity the script asked the Graph client for, refusing to build one."""
    seen = []

    def _get_token_provider(interactive=True, **kwargs):
        seen.append(interactive)
        raise GrantLost("dead grant")

    monkeypatch.setattr(
        "email_classifier.fetch.outlook.client.get_token_provider", _get_token_provider
    )
    return seen


def test_a_headless_host_gets_grant_lost_instead_of_a_consent_wait(
    fetch_outlook, asked, monkeypatch
):
    monkeypatch.setenv(providers.HEADLESS_ENV, "1")

    with pytest.raises(GrantLost):
        fetch_outlook.main([])

    assert asked == [False]


def test_a_human_at_a_keyboard_still_gets_the_browser(fetch_outlook, asked, monkeypatch):
    monkeypatch.delenv(providers.HEADLESS_ENV, raising=False)

    with pytest.raises(GrantLost):
        fetch_outlook.main([])

    assert asked == [True]
