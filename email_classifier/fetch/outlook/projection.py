"""Pure projection: raw Graph message -> neutral EmailInput dict (STU-325).

The Outlook mirror of `fetch/gmail/projection.py`: the ONLY module allowed to
know both the Graph vocabulary (conversationId, receivedDateTime, uniqueBody,
internetMessageHeaders, inferenceClassification, ...) and the neutral contract.
`client.py` fetches the raw resource; this module translates it, and
core/schema.py never changes.

No network, no msal/requests imports — the testable heart that guarantees
schema conformance.
"""

from datetime import datetime, timezone

from email_classifier.core.types import EmailInput
from email_classifier.fetch.headers import allowlisted

# Outlook's Focused Inbox split is the closest thing Graph has to Gmail's tabs.
# "focused" is Gmail's Primary; "other" has no Gmail equivalent — it merges what
# Gmail splits into promotions/updates/social/forums, so it stays as-is rather
# than being forced into one of those buckets.
_CATEGORY_BY_INFERENCE = {"focused": "primary", "other": "other"}


def _from_address(raw: dict) -> str:
    """Bare, normalized sender address; "" when Graph reports no sender.

    Graph already gives a structured address, so there is no header to parse —
    but `from` is absent on drafts and null on some system mail, and the schema
    accepts an empty string (a catch-all rule still classifies such a mail).
    """
    sender = raw.get("from") or raw.get("sender") or {}
    return (sender.get("emailAddress", {}).get("address") or "").lower()


def iso_date(received: str) -> str:
    """Convert Graph's receivedDateTime (ISO 8601, `Z` suffix) to ISO 8601 UTC.

    Normalizing to a `+00:00` offset makes an Outlook dump byte-comparable with
    a Gmail one, whose dates are built from epoch milliseconds.
    """
    parsed = datetime.fromisoformat(received.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).isoformat()


def message_to_input(raw: dict) -> dict:
    """Project a Graph `GET /me/messages/{id}` resource to EmailInput.

    `body_snippet` is the whole `uniqueBody`, not a cut of it. Graph publishes no
    preview of that field, and cutting one here would make the projection lossy —
    the parity check (STU-848) reads that as the two providers disagreeing.
    """
    return {
        "message_id": raw["id"],
        "thread_id": raw["conversationId"],
        "from_address": _from_address(raw),
        "subject": raw.get("subject") or "",
        "headers": allowlisted(raw.get("internetMessageHeaders") or []),
        "body_snippet": message_body(raw),
        "date": iso_date(raw["receivedDateTime"]),
        "provider_category": _CATEGORY_BY_INFERENCE.get(
            raw.get("inferenceClassification", "")
        ),
    }


# Reverse of _CATEGORY_BY_INFERENCE, and deliberately partial: `updates`,
# `promotions`, `social` and `forums` have no Graph spelling, so a fixture
# carrying one round-trips with no category at all.
_INFERENCE_BY_CATEGORY = {
    category: inference for inference, category in _CATEGORY_BY_INFERENCE.items()
}


def input_to_message(email: EmailInput) -> dict:
    """Encode a neutral EmailInput as the Graph resource that projects back to it.

    The inverse of `message_to_input`, for the provider-parity check (STU-848).
    No fetch path calls it. It is not `seed/outlook.py:to_graph`, which models
    what Graph *accepts on a store-write* — this models what a delivered message
    looks like on the wire, headers included.

    `receivedDateTime` carries the `Z` suffix Graph actually returns, so the
    round trip exercises the normalization rather than skipping it.
    """
    received = datetime.fromisoformat(email.date).astimezone(timezone.utc)
    raw = {
        "id": email.message_id,
        "conversationId": email.thread_id,
        "from": {"emailAddress": {"address": email.from_address}},
        "subject": email.subject,
        "internetMessageHeaders": [
            {"name": name, "value": value} for name, value in email.headers.items()
        ],
        "uniqueBody": {"contentType": "text", "content": email.body_snippet},
        "receivedDateTime": received.isoformat().replace("+00:00", "Z"),
    }
    inference = _INFERENCE_BY_CATEGORY.get(email.provider_category or "")
    if inference:
        raw["inferenceClassification"] = inference
    return raw


def message_to_precedent(raw: dict, sent: bool) -> dict:
    """Project a `$select=id,receivedDateTime,parentFolderId,uniqueBody` resource to a precedent hit.

    What a precedent is allowed to be: the words, the day they were written and
    whether the club wrote them. No sender, no id — anything else would be
    third-party PII the anonymizer then has to take back out.
    """
    return {
        "date": iso_date(raw["receivedDateTime"]),
        "direction": "sent" if sent else "received",
        "body": message_body(raw),
    }


def message_to_turn(raw: dict, sent: bool) -> dict:
    """Project a `$select=id,receivedDateTime,uniqueBody` resource to a thread turn.

    What an earlier turn of the answered conversation is allowed to be (STU-941):
    when it was written, whether the club wrote it, and its words. The sender rides
    along as `from_address` only so the tokenizer can veto against the turn's own
    domain (STU-1467); `draft_item` drops it before the item reaches the agent.
    """
    return {
        "date": iso_date(raw["receivedDateTime"]),
        "direction": "sent" if sent else "received",
        "body": message_body(raw),
        "from_address": _from_address(raw),
    }


def message_body(raw: dict) -> str:
    """The message text of a `$select=uniqueBody` resource — the sender's own words.

    `uniqueBody` is the message with the quoted chain removed (STU-935). Reading
    `body` instead lets a reply be classified, tokenized and drafted through the
    original it answers — rules firing on text the sender never wrote, and our own
    earlier answer handed back to the model as fresh context.

    Plain text when the request asked for it; Graph answers in HTML on a message
    it cannot convert, and the drafting agent reads that well enough that
    stripping tags here would cost more than it saves.
    """
    return (raw.get("uniqueBody") or {}).get("content") or ""
