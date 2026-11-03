"""Tests for the pure Gmail-message -> EmailInput projection (STU-391).

Uses synthetic raw Gmail `messages.get(format='metadata')` dicts. No network.
"""

import pytest

from email_classifier.core import schema
from email_classifier.fetch.gmail import projection


def _raw(
    *,
    thread_id="thread_x",
    labels=None,
    snippet="hello world",
    headers=None,
    internal_date="0",
):
    """Build a synthetic Gmail metadata-format message resource."""
    hdrs = headers if headers is not None else [
        {"name": "From", "value": "Kobo <news@shop.kobo.com>"},
        {"name": "Subject", "value": "Soldes"},
    ]
    return {
        "id": "msg_1",
        "threadId": thread_id,
        "labelIds": labels if labels is not None else ["INBOX"],
        "snippet": snippet,
        "internalDate": internal_date,
        "payload": {"headers": hdrs},
    }


def test_projected_output_conforms_to_schema():
    result = projection.message_to_input(_raw())
    # Must validate against the STU-389 input schema (additionalProperties: False).
    schema.validate(result)


def test_thread_id_comes_from_thread_not_message_id():
    result = projection.message_to_input(_raw(thread_id="thread_abc"))
    assert result["thread_id"] == "thread_abc"


def test_from_address_is_parsed_to_bare_lowercase_email():
    result = projection.message_to_input(
        _raw(headers=[{"name": "From", "value": "Kobo News <News@Shop.Kobo.com>"}])
    )
    assert result["from_address"] == "news@shop.kobo.com"


def test_subject_defaults_to_empty_string_when_absent():
    result = projection.message_to_input(
        _raw(headers=[{"name": "From", "value": "a@b.com"}])
    )
    assert result["subject"] == ""


def test_list_unsubscribe_header_is_extracted():
    result = projection.message_to_input(
        _raw(
            headers=[
                {"name": "From", "value": "a@b.com"},
                {"name": "List-Unsubscribe", "value": "<https://kobo.com/unsub>"},
            ]
        )
    )
    assert result["headers"]["List-Unsubscribe"] == "<https://kobo.com/unsub>"


def test_header_names_are_canonicalized_to_allowlist_casing():
    # Gmail may return arbitrary casing; Stage 1 header_present is case-sensitive.
    result = projection.message_to_input(
        _raw(
            headers=[
                {"name": "From", "value": "a@b.com"},
                {"name": "list-UNSUBSCRIBE", "value": "<https://x/unsub>"},
            ]
        )
    )
    assert result["headers"] == {"List-Unsubscribe": "<https://x/unsub>"}


def test_from_and_subject_not_duplicated_into_headers():
    result = projection.message_to_input(_raw())
    assert "From" not in result["headers"]
    assert "Subject" not in result["headers"]


def test_non_allowlisted_headers_are_dropped():
    result = projection.message_to_input(
        _raw(
            headers=[
                {"name": "From", "value": "a@b.com"},
                {"name": "Received", "value": "from internal-relay.corp ..."},
                {"name": "Message-ID", "value": "<secret@host>"},
            ]
        )
    )
    assert result["headers"] == {}


def test_body_snippet_is_html_unescaped():
    result = projection.message_to_input(_raw(snippet="Save 50% &amp; more &gt;&gt;"))
    assert result["body_snippet"] == "Save 50% & more >>"


def test_internal_date_is_converted_to_iso8601_utc():
    # Gmail internalDate is epoch milliseconds; the neutral contract wants ISO 8601.
    result = projection.message_to_input(_raw(internal_date="0"))
    assert result["date"] == "1970-01-01T00:00:00+00:00"

    result = projection.message_to_input(_raw(internal_date="1718452800000"))
    assert result["date"] == "2024-06-15T12:00:00+00:00"


@pytest.mark.parametrize(
    "label,expected",
    [
        ("CATEGORY_PERSONAL", "primary"),
        ("CATEGORY_PROMOTIONS", "promotions"),
        ("CATEGORY_UPDATES", "updates"),
        ("CATEGORY_SOCIAL", "social"),
        ("CATEGORY_FORUMS", "forums"),
    ],
)
def test_provider_category_mapping(label, expected):
    # The output key is the neutral "provider_category", not "gmail_category".
    result = projection.message_to_input(_raw(labels=["INBOX", label, "UNREAD"]))
    assert result["provider_category"] == expected


def test_provider_category_is_null_when_no_category_label():
    result = projection.message_to_input(_raw(labels=["INBOX", "UNREAD"]))
    assert result["provider_category"] is None


def test_message_id_comes_from_raw_message_id():
    result = projection.message_to_input(_raw())
    assert result["message_id"] == "msg_1"


def test_same_thread_different_messages_get_distinct_message_ids():
    raw_a = {**_raw(thread_id="shared"), "id": "msg_a"}
    raw_b = {**_raw(thread_id="shared"), "id": "msg_b"}
    a = projection.message_to_input(raw_a)
    b = projection.message_to_input(raw_b)
    assert a["thread_id"] == b["thread_id"] == "shared"
    assert a["message_id"] != b["message_id"]
