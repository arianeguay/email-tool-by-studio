"""Pure projection: raw Gmail metadata message -> neutral EmailInput dict (STU-391).

This is the ONLY module allowed to know both the Gmail vocabulary (labelIds,
internalDate, snippet, ...) and the neutral contract. `client.py` fetches the raw
resource; this module translates it. `fetch/outlook/projection.py` does the same
Graph -> neutral translation, and core/schema.py never changes.

No network, no Google imports — the testable heart that guarantees schema
conformance.
"""

import html
from datetime import datetime, timezone
from email.utils import parseaddr

from email_classifier.core.types import EmailInput
from email_classifier.fetch.headers import allowlisted

# Gmail tab categories are surfaced as CATEGORY_* label ids. Note the
# non-obvious one: CATEGORY_PERSONAL is the "Primary" tab.
_CATEGORY_BY_LABEL = {
    "CATEGORY_PERSONAL": "primary",
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_FORUMS": "forums",
}


def _extract_from_address(from_header: str) -> str:
    """Turn a raw `From` header value into a bare, normalized email address.

    The `From` header can look like `"Kobo News <News@Shop.Kobo.com>"`, a bare
    `news@shop.kobo.com`, or be empty/garbage. Stage 1 matches on the address
    and on the domain split from it, so the output should be a clean address.

    Returns "" when the header has no parseable address; the schema accepts an
    empty string, and a catch-all rule will still classify such a mail.
    """
    _, address = parseaddr(from_header)
    return address.lower()


def _iso_date(internal_date_ms: str) -> str:
    """Convert Gmail's internalDate (epoch milliseconds) to ISO 8601 UTC."""
    seconds = int(internal_date_ms) / 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def _category_from_labels(label_ids: list[str]) -> str | None:
    """Map Gmail CATEGORY_* labels to the tab name, or None if none present."""
    for label in label_ids:
        category = _CATEGORY_BY_LABEL.get(label)
        if category is not None:
            return category
    return None


def message_to_input(raw: dict) -> dict:
    """Project a Gmail `messages.get(format='metadata')` resource to EmailInput."""
    headers = raw.get("payload", {}).get("headers", [])
    by_name = {h["name"].lower(): h["value"] for h in headers}
    return {
        "message_id": raw["id"],
        "thread_id": raw["threadId"],
        "from_address": _extract_from_address(by_name.get("from", "")),
        "subject": by_name.get("subject", ""),
        "headers": allowlisted(headers),
        "body_snippet": html.unescape(raw.get("snippet", "")),
        "date": _iso_date(raw["internalDate"]),
        "provider_category": _category_from_labels(raw.get("labelIds", [])),
    }


# Reverse of _CATEGORY_BY_LABEL: every neutral category has a Gmail spelling,
# because the neutral field carries Gmail's tab vocabulary.
_LABEL_BY_CATEGORY = {category: label for label, category in _CATEGORY_BY_LABEL.items()}


def input_to_message(email: EmailInput) -> dict:
    """Encode a neutral EmailInput as the Gmail resource that projects back to it.

    The inverse of `message_to_input`, for the provider-parity check (STU-848).
    No fetch path calls it: it exists so one fixture can enter stage 1 through the
    Gmail vocabulary and through the Graph one, and the two results be compared.

    The snippet is escaped because Gmail sends it escaped — `message_to_input`
    unescapes, so a body carrying `&` would otherwise survive only one arm.
    """
    headers = [
        {"name": "From", "value": f"<{email.from_address}>"},
        {"name": "Subject", "value": email.subject},
    ] + [{"name": name, "value": value} for name, value in email.headers.items()]
    category_label = _LABEL_BY_CATEGORY.get(email.provider_category or "")
    return {
        "id": email.message_id,
        "threadId": email.thread_id,
        "payload": {"headers": headers},
        "snippet": html.escape(email.body_snippet, quote=False),
        "internalDate": str(int(datetime.fromisoformat(email.date).timestamp() * 1000)),
        "labelIds": ["INBOX"] + ([category_label] if category_label else []),
    }
