"""App-only Graph access (STU-1072): reaching a named mailbox as the application.

The delegated flow reaches whatever mailbox consented, as `/me`. App-only has no
signed-in user, so the mailbox is named instead — and every one of these tests is
about the two never being confused for each other.
"""

import base64
import json

import pytest

from email_classifier.fetch.grant import GrantLost
from email_classifier.fetch.outlook import client, push

MAILBOX = "inbox@club.example"


@pytest.fixture
def app_only_env(monkeypatch):
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "app-id")
    monkeypatch.setenv("OUTLOOK_TENANT_ID", "tenant-id")
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OUTLOOK_MAILBOX", MAILBOX)


def token_with(roles):
    """A JWT shaped like Graph's, carrying `roles` and nothing else that matters."""
    payload = base64.urlsafe_b64encode(json.dumps({"roles": roles}).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


# ── which flow runs ──────────────────────────────────────────────────────────


def test_no_secret_is_the_delegated_flow(monkeypatch):
    monkeypatch.delenv("OUTLOOK_CLIENT_SECRET", raising=False)

    assert client.app_only() is False


def test_a_secret_without_a_mailbox_refuses_rather_than_guessing(monkeypatch):
    """The one mistake here that is silent and destructive at once."""
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "shh")
    monkeypatch.delenv("OUTLOOK_MAILBOX", raising=False)

    with pytest.raises(ValueError, match="OUTLOOK_MAILBOX"):
        client.app_only()


def test_both_set_is_the_app_only_flow(app_only_env):
    assert client.app_only() is True


# ── the addressing every call funnels through ────────────────────────────────


def test_a_delegated_session_still_says_me():
    assert client.GraphSession(lambda: "t").mailbox_path == "/me"


def test_an_app_only_session_addresses_the_named_mailbox():
    session = client.GraphSession(lambda: "t", mailbox=MAILBOX)

    assert session.mailbox_path == "/users/inbox%40club.example"


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/me", "/users/inbox%40club.example"),
        ("/me/messages", "/users/inbox%40club.example/messages"),
        ("/me/mailFolders('inbox')", "/users/inbox%40club.example/mailFolders('inbox')"),
        # Not a mailbox path, and the rewrite must not treat it as one.
        ("/subscriptions", "/subscriptions"),
        # The prefix test is a boundary, not a `startswith` on three characters.
        ("/mention", "/mention"),
    ],
)
def test_the_session_rewrites_only_a_mailbox_path(monkeypatch, path, expected):
    seen = {}

    class Response:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {}

        @staticmethod
        def raise_for_status():
            return None

    def fake_request(method, url, **kwargs):
        seen["url"] = url
        return Response()

    monkeypatch.setattr(
        "requests.request", fake_request, raising=False
    )
    session = client.GraphSession(lambda: "t", mailbox=MAILBOX)
    session.request("GET", path)

    assert seen["url"] == f"{client.GRAPH_BASE}{expected}"


def test_an_absolute_next_link_is_left_alone(monkeypatch):
    seen = {}

    class Response:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {}

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr(
        "requests.request",
        lambda method, url, **kwargs: (seen.__setitem__("url", url), Response())[1],
        raising=False,
    )
    link = "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=abc"
    client.GraphSession(lambda: "t", mailbox=MAILBOX).request("GET", link)

    assert seen["url"] == link


# ── the one address the funnel cannot reach ──────────────────────────────────


def test_the_subscription_resource_names_the_mailbox():
    """Graph stores this string and resolves it later, so `/me` would deliver nothing."""
    session = client.GraphSession(lambda: "t", mailbox=MAILBOX)

    assert push._resource(session) == (
        "users/inbox%40club.example/mailFolders('inbox')/messages"
    )


def test_the_subscription_resource_of_a_delegated_session_is_unchanged():
    assert push._resource(client.GraphSession(lambda: "t")) == (
        "me/mailFolders('inbox')/messages"
    )


# ── identity, and the seed guard that rides on it ────────────────────────────


def test_the_app_only_identity_is_the_configured_mailbox(app_only_env):
    """No MSAL account exists to read, and none is needed: the mailbox is named."""
    assert client.signed_in_address() == MAILBOX


def test_seeding_refuses_when_the_configured_mailbox_is_not_the_declared_one(
    app_only_env, monkeypatch
):
    """Pointing the app at production must not let `make seed` write into it."""
    from email_classifier.seed import outlook as seeder

    monkeypatch.setenv("OUTLOOK_SEED_MAILBOX", "seed@example.com")

    with pytest.raises(RuntimeError, match="OUTLOOK_SEED_MAILBOX"):
        seeder.assert_test_mailbox()


# ── the write preflight, over roles rather than scopes ───────────────────────


def test_preflight_passes_on_a_grant_carrying_the_write_role(app_only_env, monkeypatch):
    monkeypatch.setattr(
        client, "_app_only_token_provider", lambda *a, **k: lambda: token_with(["Mail.ReadWrite"])
    )

    assert client.preflight_write_scope() is None


def test_preflight_names_the_missing_role(app_only_env, monkeypatch):
    monkeypatch.setattr(
        client, "_app_only_token_provider", lambda *a, **k: lambda: token_with(["Mail.Read"])
    )

    reason = client.preflight_write_scope()

    assert "Mail.ReadWrite" in reason
    assert "mail.read" in reason


def test_an_unreadable_token_does_not_refuse_a_valid_grant(app_only_env, monkeypatch, caplog):
    """"Could not tell" is not "insufficient" — the 403 stays the backstop."""
    monkeypatch.setattr(
        client, "_app_only_token_provider", lambda *a, **k: lambda: "not-a-jwt"
    )

    assert client.preflight_write_scope() is None
    assert "did not run" in caplog.text


def test_a_dead_secret_is_a_lost_grant_not_an_unreadable_token(app_only_env, monkeypatch):
    """A caller that stops a service on a dead delegated grant stops on this one."""

    def dead(*a, **k):
        def provider():
            raise GrantLost("secret expired")

        return provider

    monkeypatch.setattr(client, "_app_only_token_provider", dead)

    with pytest.raises(GrantLost):
        client.preflight_write_scope()


# ── the tenant an app-only grant cannot use ──────────────────────────────────


def test_app_only_refuses_the_common_tenant(monkeypatch):
    """`common` routes user sign-ins, and client credentials have no user to route."""
    monkeypatch.setenv("OUTLOOK_CLIENT_ID", "app-id")
    monkeypatch.setenv("OUTLOOK_CLIENT_SECRET", "shh")
    monkeypatch.setenv("OUTLOOK_MAILBOX", MAILBOX)
    monkeypatch.delenv("OUTLOOK_TENANT_ID", raising=False)

    with pytest.raises(ValueError, match="tenant"):
        client._app_only_token_provider()


# ── the delegated-only command ───────────────────────────────────────────────


def test_consent_refuses_on_an_app_only_host(app_only_env, capsys):
    """A sign-in here grants nothing and caches a token no app-only call reads."""
    import scripts.outlook_consent as consent

    assert consent.main() == 1
    assert MAILBOX in capsys.readouterr().err
