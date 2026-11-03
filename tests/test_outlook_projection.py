"""Tests for the pure Graph-message -> EmailInput projection (STU-325).

Uses synthetic raw Graph `GET /me/messages/{id}` dicts. No network.
"""

import importlib.util

import pytest

from email_classifier.anonymize import Tokenizer
from email_classifier.core import schema
from email_classifier.fetch.gmail import projection as gmail_projection
from email_classifier.fetch.outlook import projection
from email_classifier.stage1.rule_engine import apply, load_rules
from tests.conftest import RULES_DIR
from tests.test_guards import POSTMARK_NEWSLETTER

requires_gliner = pytest.mark.skipif(
    importlib.util.find_spec("gliner") is None, reason="needs the `ner` extra"
)


def _raw(
    *,
    conversation_id="conv_x",
    sender="Music Club <info@Club.Example>",
    subject="Horaire",
    body="hello world",
    headers=None,
    received="2024-06-15T12:00:00Z",
    inference="focused",
):
    """Build a synthetic Graph message resource."""
    address = sender.split("<")[-1].rstrip(">") if sender else None
    raw = {
        "id": "AAMkAD_1",
        "conversationId": conversation_id,
        "from": {"emailAddress": {"name": "Music Club", "address": address}},
        "subject": subject,
        "uniqueBody": {"contentType": "text", "content": body},
        "receivedDateTime": received,
        "inferenceClassification": inference,
    }
    if headers is not None:
        raw["internetMessageHeaders"] = headers
    return raw


def test_projected_output_conforms_to_schema():
    result = projection.message_to_input(_raw())
    # Must validate against the STU-389 input schema (additionalProperties: False).
    schema.validate(result)


def test_projection_matches_the_gmail_key_set():
    # The neutral contract is the interface: both providers emit the same keys.
    gmail = gmail_projection.message_to_input(
        {
            "id": "g1",
            "threadId": "t1",
            "labelIds": ["INBOX"],
            "snippet": "hi",
            "internalDate": "0",
            "payload": {"headers": [{"name": "From", "value": "a@b.com"}]},
        }
    )
    assert projection.message_to_input(_raw()).keys() == gmail.keys()


def test_thread_id_comes_from_conversation_id():
    result = projection.message_to_input(_raw(conversation_id="conv_abc"))
    assert result["thread_id"] == "conv_abc"


def test_from_address_is_lowercased():
    result = projection.message_to_input(_raw(sender="X <News@Shop.Kobo.com>"))
    assert result["from_address"] == "news@shop.kobo.com"


def test_from_address_is_empty_string_when_graph_reports_no_sender():
    raw = _raw()
    del raw["from"]
    assert projection.message_to_input(raw)["from_address"] == ""


def test_subject_defaults_to_empty_string_when_null():
    result = projection.message_to_input(_raw(subject=None))
    assert result["subject"] == ""


def test_header_names_are_canonicalized_to_allowlist_casing():
    # Graph returns header names as sent; Stage 1 header_present is case-sensitive.
    result = projection.message_to_input(
        _raw(headers=[{"name": "list-UNSUBSCRIBE", "value": "<https://x/unsub>"}])
    )
    assert result["headers"] == {"List-Unsubscribe": "<https://x/unsub>"}


def test_non_allowlisted_headers_are_dropped():
    result = projection.message_to_input(
        _raw(
            headers=[
                {"name": "Received", "value": "from internal-relay.corp ..."},
                {"name": "Message-ID", "value": "<secret@host>"},
            ]
        )
    )
    assert result["headers"] == {}


def test_headers_default_to_empty_when_graph_omits_them():
    assert projection.message_to_input(_raw())["headers"] == {}


def test_received_date_is_converted_to_iso8601_utc():
    # Graph's `Z` suffix is normalized to the same +00:00 form Gmail dumps use.
    result = projection.message_to_input(_raw(received="2024-06-15T12:00:00Z"))
    assert result["date"] == "2024-06-15T12:00:00+00:00"


def test_received_date_is_normalized_to_utc_from_an_offset():
    result = projection.message_to_input(_raw(received="2024-06-15T09:00:00-03:00"))
    assert result["date"] == "2024-06-15T12:00:00+00:00"


@pytest.mark.parametrize(
    "inference,expected",
    [("focused", "primary"), ("other", "other")],
)
def test_provider_category_mapping(inference, expected):
    # The output key is the neutral "provider_category", and its value carries no
    # Graph vocabulary ("inferenceClassification" never leaves this module).
    result = projection.message_to_input(_raw(inference=inference))
    assert result["provider_category"] == expected


def test_provider_category_is_null_when_absent():
    raw = _raw()
    del raw["inferenceClassification"]
    assert projection.message_to_input(raw)["provider_category"] is None


def test_stage1_classifies_an_outlook_dump_unchanged():
    """The neutral interface holds: stage 1 runs on Graph mail with no changes.

    Same promo as `fixtures/bucket_c/generic_oneclick_promo.json`, delivered as a
    raw Graph resource — it must reach the same deterministic verdict.
    """
    email = schema.validate(
        projection.message_to_input(
            {
                "id": "AAMkAD_promo",
                "conversationId": "AAQkAD_promo",
                "from": {"emailAddress": {"address": "deals@news.example-shop.com"}},
                "subject": "Summer sale — 40% off our whole spring lineup",
                "uniqueBody": {
                    "contentType": "text",
                    "content": "Our biggest seasonal event is here. Browse hundreds of new styles.",
                },
                "receivedDateTime": "2026-06-09T13:05:00Z",
                "inferenceClassification": "other",
                "internetMessageHeaders": [
                    {"name": "List-Unsubscribe", "value": "<https://example-shop.com/unsub>"},
                    {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
                ],
            }
        )
    )
    match = apply(email, load_rules(RULES_DIR / "rules.yaml"))
    assert match.rule_id == "universal_oneclick_archive"
    assert match.action == "label_archive"


# ── a reply is not read through its own quote (STU-935) ──────────────────────

# What Graph returns for a one-line reply to a mail the club itself sent. `body`
# is the reply plus the whole quoted original, and `bodyPreview` is the first 255
# characters of that — so both carry Caroline's name and the "rendez-vous" the
# `not_transactional` guard trips on, neither of which the sender wrote here.
_REPLY = """Parfait, merci — désinscrivez-moi de l'infolettre.

Pour vous désabonner, cliquez sur le lien au bas du courriel."""

_QUOTED_ORIGINAL = """De : Caroline Tremblay <caroline@club.example>
Objet : Nouvelles du club

Bonjour, peux-tu confirmer le rendez-vous de jeudi?"""

_RAW_REPLY = {
    "id": "AAMkAD_reply",
    "conversationId": "AAQkAD_thread",
    "from": {"emailAddress": {"address": "news@example-shop.com"}},
    "subject": "RE: Nouvelles du club",
    "body": {"contentType": "text", "content": f"{_REPLY}\n\n{_QUOTED_ORIGINAL}"},
    "bodyPreview": f"{_REPLY} {_QUOTED_ORIGINAL}"[:255],
    "uniqueBody": {"contentType": "text", "content": _REPLY},
    "receivedDateTime": "2026-08-20T09:00:00Z",
    "inferenceClassification": "other",
    "internetMessageHeaders": [
        {"name": "List-Unsubscribe", "value": "<https://example-shop.com/unsub>"},
        {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
    ],
}


def test_the_snippet_is_the_reply_alone_not_the_chain_below_it():
    assert projection.message_to_input(_RAW_REPLY)["body_snippet"] == _REPLY


def test_a_guard_does_not_fire_on_a_trigger_only_the_quote_holds():
    """`rendez-vous` and `peux-tu` sit in the quoted original, so `not_transactional`
    stays satisfied and the one-click rule archives — reading `body` would leave this
    mail visible on words its sender never wrote."""
    email = schema.validate(projection.message_to_input(_RAW_REPLY))

    match = apply(email, load_rules(RULES_DIR / "rules.yaml"))

    assert match.rule_id == "universal_oneclick_archive"
    assert match.action == "label_archive"


@requires_gliner
def test_the_keymap_mints_no_token_for_a_name_only_the_quote_holds():
    """A name reached only through the chain still costs a keymap entry, and that
    entry outlives the batch — every later mail naming Caroline reuses it."""
    email = schema.validate(projection.message_to_input(_RAW_REPLY))
    tokenizer = Tokenizer()

    tokenizer.anonymize(email.body_snippet)

    assert "Caroline Tremblay" not in tokenizer.keymap.values()


# The One-Click newsletter of run 8704964d, entering stage 1 through the Graph
# vocabulary rather than through `is_satisfied` directly: the whole point of
# STU-997 is that this path — a full `uniqueBody` in `body_snippet` (STU-935) —
# is where the guard stopped being a bias and became a veto.
_RAW_NEWSLETTER = {
    "id": "AAMkAD_fyi",
    "conversationId": "AAQkAD_fyi",
    "from": {"emailAddress": {"address": "hello@fyi.postmarkapp.com"}},
    "subject": "Postmark FYI — August",
    "body": {"contentType": "text", "content": POSTMARK_NEWSLETTER},
    "bodyPreview": POSTMARK_NEWSLETTER[:255],
    "uniqueBody": {"contentType": "text", "content": POSTMARK_NEWSLETTER},
    "receivedDateTime": "2026-08-26T11:00:00Z",
    "inferenceClassification": "other",
    "internetMessageHeaders": [
        {"name": "List-Unsubscribe", "value": "<https://postmarkapp.com/unsub>"},
        {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
    ],
}


def test_a_full_body_newsletter_takes_the_oneclick_path_not_catch_all():
    """STU-892's criterion, restated at the provider boundary and this time bounded.

    `invoice`, `delivery`, `shipped`, `verify` and `can you` are all in this body,
    all of them past the head the guard reads. Whole-body, the message lands on
    `catch_all` — which on run 8704964d is what all 26 of them did.
    """
    email = schema.validate(projection.message_to_input(_RAW_NEWSLETTER))

    match = apply(email, load_rules(RULES_DIR / "rules.yaml"))

    assert match.rule_id == "universal_oneclick_archive"
    assert match.action == "label_archive"
