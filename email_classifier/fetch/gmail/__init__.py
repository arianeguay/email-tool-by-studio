"""Gmail read-only fetch provider (STU-391).

Public API of the provider. `client` (OAuth + API I/O), `projection`
(raw Gmail -> neutral EmailInput) and `push` (watch + history.list) are
internal modules; consumers import from here. A future `fetch/outlook/`
package mirrors this shape — see `email_classifier/providers.py` for the
dispatch and the Gmail/Outlook ontology mapping.
"""

from email_classifier.fetch.gmail.client import (
    CONSENT_COMMAND,
    SCOPES,
    GmailFetcher,
    GmailSentSource,
    GrantAware,
    build_service,
    get_credentials,
)
from email_classifier.fetch.gmail.push import list_new_message_ids, watch
from email_classifier.fetch.grant import GrantLost

__all__ = [
    "CONSENT_COMMAND",
    "SCOPES",
    "GrantLost",
    "GrantAware",
    "get_credentials",
    "build_service",
    "GmailFetcher",
    "GmailSentSource",
    "list_new_message_ids",
    "watch",
]
