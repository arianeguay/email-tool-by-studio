"""Outlook (Microsoft Graph) read-only fetch provider (STU-325).

Public API of the provider, mirroring `fetch/gmail/`. `client` (OAuth + Graph
I/O), `projection` (raw Graph -> neutral EmailInput) and `push` (change-notification
subscriptions, STU-823) are internal modules; consumers import from here. See
`email_classifier/providers.py` for the dispatch and the ontology mapping.
"""

from email_classifier.fetch.grant import GrantLost
from email_classifier.fetch.outlook.client import (
    READ_SCOPES,
    WRITE_SCOPES,
    GraphSession,
    OutlookFetcher,
    build_session,
    get_token_provider,
    app_only,
    configured_mailbox,
    granted_scopes,
    preflight_write_scope,
)
from email_classifier.fetch.outlook.push import (
    delete_subscription,
    reauthorize_subscription,
    renew_subscription,
    subscribe,
)

__all__ = [
    "READ_SCOPES",
    "WRITE_SCOPES",
    "GrantLost",
    "get_token_provider",
    "app_only",
    "configured_mailbox",
    "granted_scopes",
    "preflight_write_scope",
    "build_session",
    "GraphSession",
    "OutlookFetcher",
    "subscribe",
    "renew_subscription",
    "reauthorize_subscription",
    "delete_subscription",
]
