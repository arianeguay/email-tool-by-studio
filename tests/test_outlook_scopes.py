"""Graph scope preflight (STU-844): a read-only grant must not start an apply run."""

import json
import sys
import types

import pytest

from email_classifier.fetch.outlook import client
from email_classifier.providers import preflight_write


def write_cache(tmp_path, *scopes):
    path = tmp_path / "outlook-token.json"
    path.write_text(
        json.dumps({"AccessToken": {"k": {"target": " ".join(scopes)}}}), encoding="utf-8"
    )
    return path


def test_granted_scopes_are_unqualified_and_casefolded(tmp_path):
    path = write_cache(tmp_path, "https://graph.microsoft.com/Mail.Read", "openid")

    assert client.granted_scopes(path) == {"mail.read", "openid"}


def test_granted_scopes_of_an_absent_cache_is_empty(tmp_path):
    assert client.granted_scopes(tmp_path / "nope.json") == set()


def test_preflight_passes_on_a_read_write_grant(tmp_path):
    path = write_cache(tmp_path, "Mail.ReadWrite", "offline_access")

    assert client.preflight_write_scope(path) is None


def test_preflight_accepts_a_fully_qualified_scope(tmp_path):
    path = write_cache(tmp_path, "https://graph.microsoft.com/Mail.ReadWrite")

    assert client.preflight_write_scope(path) is None


def test_preflight_refuses_a_read_only_grant(tmp_path):
    path = write_cache(tmp_path, "Mail.Read", "openid")

    refusal = client.preflight_write_scope(path)

    assert "Mail.ReadWrite" in refusal
    assert client.CONSENT_COMMAND in refusal


def test_preflight_refuses_an_absent_cache(tmp_path):
    refusal = client.preflight_write_scope(tmp_path / "nope.json")

    assert "Mail.ReadWrite" in refusal
    assert client.CONSENT_COMMAND in refusal


def test_preflight_refuses_an_unparseable_cache(tmp_path):
    path = tmp_path / "outlook-token.json"
    path.write_text("{not json", encoding="utf-8")

    assert "Mail.ReadWrite" in client.preflight_write_scope(path)


def test_gmail_needs_no_scope_preflight():
    """Its client discards a narrower token and re-consents (STU-405)."""
    assert preflight_write("gmail") is None


def test_the_provider_dispatch_reads_the_configured_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTLOOK_TOKEN_FILE", str(write_cache(tmp_path, "Mail.Read")))

    assert "Mail.ReadWrite" in preflight_write("outlook")


def test_a_silent_token_short_of_the_requested_scopes_is_not_honoured():
    """MSAL redeems a pre-widening refresh token fine — with the old scopes."""
    result = {"access_token": "t", "scope": "Mail.Read openid"}

    assert not client._honours(result, client.WRITE_SCOPES)
    assert client._honours(result, client.READ_SCOPES)


def test_a_read_write_grant_honours_a_read_request():
    """Graph answers with the scopes it granted: a mailbox consented to
    Mail.ReadWrite hands back `Mail.ReadWrite` even when Mail.Read was asked for.
    Reading that as a refusal re-consents on every single Graph call."""
    result = {"access_token": "t", "scope": "Mail.ReadWrite email openid profile"}

    assert client._honours(result, client.READ_SCOPES)
    assert client._honours(result, client.WRITE_SCOPES)


def test_a_read_grant_still_does_not_honour_a_write_request():
    result = {"access_token": "t", "scope": "Mail.Read email openid profile"}

    assert not client._honours(result, client.WRITE_SCOPES)


def test_a_cache_served_token_carries_no_scope_and_is_honoured():
    """What `acquire_token_silent` actually answers on a cache hit — no `scope`
    key at all. MSAL only serves a token whose cached target already covers the
    request, so treating the absent key as an empty grant refuses every hit and
    re-consents on every Graph call."""
    result = {
        "access_token": "t",
        "expires_in": 3599,
        "token_type": "Bearer",
        "token_source": "cache",
    }

    assert client._honours(result, client.WRITE_SCOPES)
    assert client._honours(result, client.READ_SCOPES)


def test_an_empty_scope_string_is_still_a_refusal():
    """Distinct from an absent key: Graph answering with no scope granted."""
    assert not client._honours({"access_token": "t", "scope": ""}, client.WRITE_SCOPES)


class _Cache:
    has_state_changed = False

    def deserialize(self, blob):
        pass

    def serialize(self):
        return "{}"


def _fake_msal(monkeypatch, silent=None, interactive=None, calls=None, accounts=("account",)):
    class _App:
        def __init__(self, *args, **kwargs):
            pass

        def get_accounts(self):
            return list(accounts)

        def acquire_token_silent(self, scopes, account=None):
            return dict(silent or {})

        def acquire_token_interactive(self, scopes, prompt=None):
            calls.append(list(scopes))
            return dict(interactive or {})

    monkeypatch.setitem(
        sys.modules,
        "msal",
        types.SimpleNamespace(SerializableTokenCache=_Cache, PublicClientApplication=_App),
    )


def test_signed_in_address_reads_the_cached_account_casefolded(monkeypatch, tmp_path):
    """The seed guard's only source of truth, and every other test replaces it."""
    _fake_msal(monkeypatch, accounts=[{"username": "Test@Outlook.com"}])

    address = client.signed_in_address(client_id="app", token_file=tmp_path / "token.json")

    assert address == "test@outlook.com"


def test_signed_in_address_is_none_when_nothing_is_cached(monkeypatch, tmp_path):
    _fake_msal(monkeypatch, accounts=[])

    assert client.signed_in_address(client_id="app", token_file=tmp_path / "token.json") is None


def test_signed_in_address_is_none_when_the_account_carries_no_username(monkeypatch, tmp_path):
    """An empty username must not read as a mailbox the guard would then match."""
    _fake_msal(monkeypatch, accounts=[{"username": ""}])

    assert client.signed_in_address(client_id="app", token_file=tmp_path / "token.json") is None


def test_consent_is_offered_at_most_once_however_many_graph_calls(monkeypatch, tmp_path):
    """A token is acquired per Graph call, so an unconditional interactive fallback
    opens one browser window per message. Once consent has been given and the grant
    still falls short, the next call fails loud instead of prompting again."""
    calls = []
    _fake_msal(
        monkeypatch,
        silent={"access_token": "t", "scope": "openid"},
        interactive={"access_token": "t", "scope": "Mail.Read openid"},
        calls=calls,
    )
    access_token = client.get_token_provider(
        client_id="app", token_file=tmp_path / "token.json"
    )

    assert access_token() == "t"
    with pytest.raises(RuntimeError, match="after consenting"):
        access_token()

    assert len(calls) == 1


def test_headless_refuses_instead_of_opening_a_browser(monkeypatch, tmp_path):
    """STU-930: a service or a cron job has no human at a browser, so what would
    prompt is a lost grant — named as one, with the recovery command."""
    calls = []
    _fake_msal(
        monkeypatch,
        silent={"error": "invalid_grant",
                "error_description": "AADSTS700082: refresh token expired"},
        calls=calls,
    )
    access_token = client.get_token_provider(
        client_id="app", token_file=tmp_path / "token.json", interactive=False
    )

    with pytest.raises(client.GrantLost) as raised:
        access_token()

    assert "refresh token expired" in str(raised.value)
    assert client.CONSENT_COMMAND in str(raised.value)
    assert calls == []


def test_headless_names_an_empty_cache_rather_than_an_msal_error(monkeypatch, tmp_path):
    """The 90-day death leaves no account behind, not an error description."""
    _fake_msal(monkeypatch, accounts=[], calls=[])
    access_token = client.get_token_provider(
        client_id="app", token_file=tmp_path / "token.json", interactive=False
    )

    with pytest.raises(client.GrantLost, match="no cached account"):
        access_token()


def test_a_healthy_headless_grant_still_serves_its_token(monkeypatch, tmp_path):
    _fake_msal(monkeypatch, silent={"access_token": "t", "scope": "Mail.Read"}, calls=[])
    access_token = client.get_token_provider(
        client_id="app", token_file=tmp_path / "token.json", interactive=False
    )

    assert access_token() == "t"
