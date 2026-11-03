"""Graph subscription plumbing against a fake session (STU-823). No network."""

from datetime import datetime, timezone

from email_classifier.fetch import outlook


class _Session:
    """Fake GraphSession recording every non-GET call."""

    # Delegated addressing, like the session this stands in for by default.
    mailbox_path = "/me"

    def __init__(self, response=None):
        self._response = response or {}
        self.calls = []

    def request(self, method, url, params=None, payload=None):
        self.calls.append({"method": method, "url": url, "params": params, "payload": payload})
        return self._response


def test_subscribe_payload():
    session = _Session({"id": "sub1", "expirationDateTime": "later"})

    response = outlook.subscribe(session, "https://host/graph", "s3cret")

    assert response["id"] == "sub1"
    call = session.calls[0]
    assert (call["method"], call["url"]) == ("POST", "/subscriptions")
    payload = call["payload"]
    assert payload["changeType"] == "created"
    assert payload["notificationUrl"] == "https://host/graph"
    assert payload["resource"] == "me/mailFolders('inbox')/messages"
    assert payload["clientState"] == "s3cret"
    assert datetime.fromisoformat(payload["expirationDateTime"]) > datetime.now(timezone.utc)


def test_subscribe_registers_a_lifecycle_url():
    """Without it Graph has nowhere to announce that delivery stopped (STU-937)."""
    session = _Session({"id": "sub1", "expirationDateTime": "later"})

    outlook.subscribe(session, "https://host/graph", "s3cret")

    payload = session.calls[0]["payload"]
    assert payload["lifecycleNotificationUrl"] == "https://host/graph/lifecycle"


def test_reauthorize_also_renews():
    """Reauthorizing alone leaves the subscription on its original clock."""
    session = _Session({"id": "sub1", "expirationDateTime": "later"})

    outlook.reauthorize_subscription(session, "sub1")

    assert [(c["method"], c["url"]) for c in session.calls] == [
        ("POST", "/subscriptions/sub1/reauthorize"),
        ("PATCH", "/subscriptions/sub1"),
    ]


def test_renew_patches_expiration_only():
    session = _Session()

    outlook.renew_subscription(session, "sub1")

    call = session.calls[0]
    assert (call["method"], call["url"]) == ("PATCH", "/subscriptions/sub1")
    assert set(call["payload"]) == {"expirationDateTime"}


def test_subscription_lifetime_stays_under_the_graph_cap():
    """Graph rejects a mail subscription longer than 4230 minutes."""
    session = _Session()

    outlook.subscribe(session, "https://host/graph", "s3cret")

    expiration = datetime.fromisoformat(session.calls[0]["payload"]["expirationDateTime"])
    assert (expiration - datetime.now(timezone.utc)).total_seconds() < 4230 * 60
