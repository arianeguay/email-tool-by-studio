"""Graph failures carry their response body into the raised exception (STU-862).

Every write journal in the repo records `str(exc)`, so the Graph error code has
to be in the message or the failure is unreadable after the fact.
"""

import pytest

requests = pytest.importorskip("requests")

from email_classifier.fetch.outlook.client import GrantLost, GraphSession  # noqa: E402

_URL = "https://graph.microsoft.com/v1.0/me/messages/abc/createReply"

_GRAPH_ERROR = (
    '{"error":{"code":"ErrorInvalidReferenceItem",'
    '"message":"The reference item does not support the requested operation."}}'
)


def _response(status: int, body: str) -> "requests.Response":
    response = requests.Response()
    response.status_code = status
    response.url = _URL
    response._content = body.encode("utf-8")
    return response


def _session(monkeypatch, response) -> GraphSession:
    monkeypatch.setattr(requests, "request", lambda *a, **kw: response)
    return GraphSession(lambda: "token")


def test_error_body_is_attached_to_the_exception(monkeypatch):
    session = _session(monkeypatch, _response(400, _GRAPH_ERROR))

    with pytest.raises(requests.HTTPError) as raised:
        session.request("POST", "/me/messages/abc/createReply")

    message = str(raised.value)
    assert "ErrorInvalidReferenceItem" in message
    assert "The reference item does not support the requested operation." in message
    # The status line and URL that were there before stay there.
    assert "400" in message
    assert _URL in message


def test_a_long_error_body_is_truncated(monkeypatch):
    session = _session(monkeypatch, _response(500, "x" * 2000))

    with pytest.raises(requests.HTTPError) as raised:
        session.request("GET", "/me/messages")

    assert "... (truncated)" in str(raised.value)
    assert len(str(raised.value)) < 800


def test_an_empty_error_body_still_raises(monkeypatch):
    session = _session(monkeypatch, _response(403, ""))

    with pytest.raises(requests.HTTPError) as raised:
        session.request("GET", "/me/messages")

    assert "403" in str(raised.value)


def test_a_204_with_no_body_returns_an_empty_dict(monkeypatch):
    session = _session(monkeypatch, _response(204, ""))

    assert session.request("DELETE", "/me/messages/abc") == {}


_UNAUTHENTICATED = (
    '{"error":{"code":"InvalidAuthenticationToken",'
    '"message":"Access token has expired or is not yet valid."}}'
)


def test_a_401_is_a_lost_grant_not_a_generic_http_error(monkeypatch):
    """STU-930: the grant behind the call is gone, so no retry can clear it."""
    session = _session(monkeypatch, _response(401, _UNAUTHENTICATED))

    with pytest.raises(GrantLost) as raised:
        session.request("GET", "/me/messages")

    message = str(raised.value)
    assert "InvalidAuthenticationToken" in message
    assert "make outlook-consent" in message
    assert not isinstance(raised.value, requests.HTTPError)


def test_a_403_stays_a_generic_http_error(monkeypatch):
    """A scope refusal is `preflight_write_scope`'s, and re-consenting fixes it."""
    session = _session(monkeypatch, _response(403, '{"error":{"code":"ErrorAccessDenied"}}'))

    with pytest.raises(requests.HTTPError):
        session.request("POST", "/me/messages/abc/move")
