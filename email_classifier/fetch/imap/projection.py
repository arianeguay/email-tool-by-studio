"""Pure projection: an RFC 5322 message -> neutral EmailInput dict (STU-2054).

The IMAP mirror of `fetch/gmail/projection.py` and `fetch/outlook/projection.py`.
A message is addressed by its `Message-ID` (angle brackets dropped), the one
identifier that survives a folder move; a conversation by the first id of its
`References` chain, which every reply in it carries.

IMAP has no server-side `uniqueBody`, so `message_body` cuts the quoted chain
here: classification, tokenization and drafting must read what the sender wrote,
not the mail it answers.
"""

import functools
import re
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import parseaddr

from email_classifier import lang
from email_classifier.fetch.headers import allowlisted

_ID = re.compile(r"<([^<>]+)>")


@functools.cache
def _quote_markers() -> tuple[re.Pattern, ...]:
    return lang.compiled_quote_markers(lang.deployment_config())


def parse(raw: bytes) -> EmailMessage:
    return message_from_bytes(raw, policy=policy.default)


def ids(value: str | None) -> list[str]:
    """Every `<id>` in a header value, brackets dropped, in order."""
    return _ID.findall(str(value or ""))


def message_id(message: EmailMessage) -> str | None:
    found = ids(message.get("Message-ID"))
    return found[0] if found else None


def thread_id(message: EmailMessage) -> str | None:
    """The conversation's root: the first `References` id, else the parent, else itself."""
    for header in ("References", "In-Reply-To"):
        found = ids(message.get(header))
        if found:
            return found[0]
    return message_id(message)


def message_body(message: EmailMessage) -> str:
    """The sender's own words: plain text preferred, the quoted chain cut off."""
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    cuts = [match.start() for marker in _quote_markers() if (match := marker.search(text))]
    return text[:min(cuts, default=len(text))].strip()


def iso_date(received: datetime) -> str:
    return received.astimezone(timezone.utc).isoformat()


def message_to_input(message: EmailMessage, received: datetime) -> dict:
    """`received` is the server's INTERNALDATE: delivery time, as Graph's receivedDateTime."""
    return {
        "message_id": message_id(message),
        "thread_id": thread_id(message),
        "from_address": parseaddr(str(message.get("From", "")))[1].lower(),
        "subject": str(message.get("Subject", "")),
        "headers": allowlisted([{"name": k, "value": str(v)} for k, v in message.items()]),
        "body_snippet": message_body(message),
        "date": iso_date(received),
        "provider_category": None,
    }


def message_to_turn(message: EmailMessage, received: datetime, sent: bool) -> dict:
    return {
        "date": iso_date(received),
        "direction": "sent" if sent else "received",
        "body": message_body(message),
        "from_address": parseaddr(str(message.get("From", "")))[1].lower(),
    }


def message_to_precedent(message: EmailMessage, received: datetime, sent: bool) -> dict:
    return {
        "date": iso_date(received),
        "direction": "sent" if sent else "received",
        "body": message_body(message),
    }
