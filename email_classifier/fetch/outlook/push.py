"""Graph change-notification subscriptions (STU-823) — the Outlook push path.

Still read-only of the mailbox: a subscription only registers where Graph POSTs
new-mail notifications, and is permitted under the `Mail.Read` scope.

Graph caps a mail subscription's lifetime at 4230 minutes (~3 days) — renew from
a daily cron (`scripts/renew_graph_subscription.py`).

A subscription also registers a *lifecycle* URL (STU-937). Graph announces
`reauthorizationRequired`, `subscriptionRemoved` and `missed` there, and nowhere
else: without it a lapsed grant stops delivery while the daily PATCH keeps
succeeding, so the cron reports health over an unclassified mailbox.
"""

from datetime import datetime, timedelta, timezone

from email_classifier.fetch.outlook.client import GraphSession

# The one Graph address here that is not a URL. It is a value Graph stores and
# resolves later, on its own side, so `GraphSession`'s `/me` rewrite cannot reach
# it — an app-only subscription left saying `me` is accepted at creation and
# delivers nothing, which is the failure this whole app has the hardest time
# telling apart from a quiet mailbox.
def _resource(session: GraphSession) -> str:
    return f"{session.mailbox_path.lstrip('/')}/mailFolders('inbox')/messages"
_LIFETIME = timedelta(minutes=4200)


def _expiration() -> str:
    return (datetime.now(timezone.utc) + _LIFETIME).isoformat()


def _lifecycle_url(notification_url: str) -> str:
    """The lifecycle endpoint that goes with a notification URL.

    Derived rather than configured: the same webhook serves both, and a second
    env var is a second thing to get wrong on a host where getting it wrong
    shows up as silence.
    """
    return f"{notification_url.rstrip('/')}/lifecycle"


def subscribe(session: GraphSession, notification_url: str, client_state: str) -> dict:
    """Create a subscription: Graph POSTs to notification_url on each new inbox
    message. Returns the raw response ({"id": ..., "expirationDateTime": ...}).

    Creation triggers the validation handshake on *both* URLs — each endpoint
    must already be up and echoing validationToken.
    """
    return session.request(
        "POST",
        "/subscriptions",
        payload={
            "changeType": "created",
            "notificationUrl": notification_url,
            "lifecycleNotificationUrl": _lifecycle_url(notification_url),
            "resource": _resource(session),
            "expirationDateTime": _expiration(),
            "clientState": client_state,
        },
    )


def renew_subscription(session: GraphSession, subscription_id: str) -> dict:
    """Push the subscription's expiration out to the maximum again."""
    return session.request(
        "PATCH",
        f"/subscriptions/{subscription_id}",
        payload={"expirationDateTime": _expiration()},
    )


def reauthorize_subscription(session: GraphSession, subscription_id: str) -> dict:
    """Answer `reauthorizationRequired`: re-consent, then renew.

    Reauthorizing alone does not move the expiration — a subscription
    reauthorized but not renewed still lapses on its original clock.
    """
    session.request("POST", f"/subscriptions/{subscription_id}/reauthorize")
    return renew_subscription(session, subscription_id)


def delete_subscription(session: GraphSession, subscription_id: str) -> None:
    """Drop a subscription, so a replacement does not double every notification."""
    session.request("DELETE", f"/subscriptions/{subscription_id}")
