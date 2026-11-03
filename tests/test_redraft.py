"""Tests for `make redraft` — one conversation drafted again from scratch.

The mailbox is a fake throughout: no msal, no requests, no Graph. The link the
tests parse is the one Outlook writes when the reading pane shows a thread, so
the id it carries is a conversation id, not a message id.
"""

import json

import pytest
import yaml

from email_classifier import club_memory, mail_link
from email_classifier.apply.journal import read_entries
from email_classifier.apply.undo import drafts_of_run
from email_classifier.fetch import outlook
from tests.conftest import stub_detector
from tests.test_drafts import load_script, write_policy

LINK = (
    "https://outlook.cloud.microsoft/mail/inbox/id/"
    "AAQkAGVjY2MwY2I5LTc1MWYtNGIyOS1iNWM2LWZkNTllMWE2NWNhMQAQAFw%2Fe6m7r7dJgUL7JUs1rZk%3D"
    "?deeplink=mail%2F"
)
CONVERSATION = (
    "AAQkAGVjY2MwY2I5LTc1MWYtNGIyOS1iNWM2LWZkNTllMWE2NWNhMQAQAFw/e6m7r7dJgUL7JUs1rZk="
)


# ── the link ────────────────────────────────────────────────────────────────

def test_a_thread_link_yields_the_id_outlook_encoded_into_it():
    assert mail_link.message_ref(LINK) == CONVERSATION


def test_a_bare_id_is_already_the_reference():
    assert mail_link.message_ref("AAMkAGabc=") == "AAMkAGabc="


def test_a_url_that_names_no_message_is_refused():
    with pytest.raises(ValueError, match="no message id"):
        mail_link.message_ref("https://outlook.cloud.microsoft/mail/inbox/")


# ── resolving it against the mailbox ─────────────────────────────────────────

class _Malformed(Exception):
    """What Graph really answers for a conversation id on `/me/messages/{id}`.

    Not a 404: the id is the wrong shape for that route, so the refusal is
    `400 ErrorInvalidIdMalformed`. Fixing this fake is the whole of STU-926 —
    the fallback was written against an invented 404 and never fired live.
    """

    def __init__(self):
        self.response = type("Response", (), {"status_code": 400})()


class _NotFound(Exception):
    def __init__(self):
        self.response = type("Response", (), {"status_code": 404})()


class _Boom(Exception):
    def __init__(self):
        self.response = type("Response", (), {"status_code": 500})()


def _msg(mid, received):
    return {
        "id": mid,
        "conversationId": CONVERSATION,
        "from": {"emailAddress": {"address": "parent@example.ca"}},
        "subject": "Horaire",
        "uniqueBody": {"contentType": "text", "content": "Bonjour"},
        "receivedDateTime": received,
        "inferenceClassification": "focused",
    }


class _Session:
    """Fake GraphSession: messages by id, plus one conversation listing."""

    def __init__(self, by_id, listing=(), error=_Malformed):
        self._by_id = by_id
        self._listing = list(listing)
        self._error = error
        self.list_params = []

    def get(self, url, params=None, prefer=None):
        if url == "/me/messages":
            self.list_params.append(params)
            return {"value": self._listing}
        if url.startswith("/me/mailFolders('"):
            return {"id": f"{url.split(chr(39))[1]}-id"}
        message_id = url.rsplit("/", 1)[-1]
        if message_id not in self._by_id:
            raise self._error()
        return self._by_id[message_id]


def test_a_message_id_resolves_straight_to_that_message():
    session = _Session({"m1": _msg("m1", "2026-06-12T08:15:00Z")})

    result = outlook.OutlookFetcher(session).fetch_input_by_reference("m1")

    assert result["message_id"] == "m1"
    assert session.list_params == []


def test_a_conversation_id_resolves_to_its_most_recent_message():
    session = _Session(
        {"old": _msg("old", "2026-06-10T08:00:00Z"),
         "new": _msg("new", "2026-06-12T08:00:00Z")},
        listing=[{"id": "old", "receivedDateTime": "2026-06-10T08:00:00Z"},
                 {"id": "new", "receivedDateTime": "2026-06-12T08:00:00Z"}],
    )

    result = outlook.OutlookFetcher(session).fetch_input_by_reference(CONVERSATION)

    assert result["message_id"] == "new"


def test_the_conversation_listing_steps_over_the_draft_being_replaced():
    session = _Session({"m1": _msg("m1", "2026-06-12T08:00:00Z")},
                       listing=[{"id": "m1", "receivedDateTime": "2026-06-12T08:00:00Z"}])

    outlook.OutlookFetcher(session).fetch_input_by_reference(CONVERSATION)

    assert "isDraft eq false" in session.list_params[0]["$filter"]


def test_a_deleted_newest_turn_is_not_the_reply_target():
    """The listing is mailbox-wide, so a thrown-out turn is still in it (STU-967)."""
    session = _Session(
        {"live": _msg("live", "2026-06-10T08:00:00Z"),
         "trashed": _msg("trashed", "2026-06-12T08:00:00Z")},
        listing=[{"id": "live", "receivedDateTime": "2026-06-10T08:00:00Z",
                  "parentFolderId": "archive-id"},
                 {"id": "trashed", "receivedDateTime": "2026-06-12T08:00:00Z",
                  "parentFolderId": "deleteditems-id"}],
    )

    result = outlook.OutlookFetcher(session).fetch_input_by_reference(CONVERSATION)

    assert result["message_id"] == "live"


def test_a_conversation_deleted_whole_has_nothing_to_reply_to():
    session = _Session(
        {"trashed": _msg("trashed", "2026-06-12T08:00:00Z")},
        listing=[{"id": "trashed", "receivedDateTime": "2026-06-12T08:00:00Z",
                  "parentFolderId": "junkemail-id"}],
    )

    with pytest.raises(LookupError, match="no message to reply to"):
        outlook.OutlookFetcher(session).fetch_input_by_reference(CONVERSATION)


def test_a_reference_that_names_nothing_fails_loud():
    session = _Session({})

    with pytest.raises(LookupError, match="no message to reply to"):
        outlook.OutlookFetcher(session).fetch_input_by_reference(CONVERSATION)


def test_an_id_graph_no_longer_resolves_falls_back_to_the_conversation():
    session = _Session(
        {"new": _msg("new", "2026-06-12T08:00:00Z")},
        listing=[{"id": "new", "receivedDateTime": "2026-06-12T08:00:00Z"}],
        error=_NotFound,
    )

    result = outlook.OutlookFetcher(session).fetch_input_by_reference(CONVERSATION)

    assert result["message_id"] == "new"


def test_a_graph_error_that_is_not_about_the_id_is_not_a_conversation():
    session = _Session({}, error=_Boom)

    with pytest.raises(_Boom):
        outlook.OutlookFetcher(session).fetch_input_by_reference("m1")


# ── the redraft itself ───────────────────────────────────────────────────────

EMAIL = {
    "message_id": "m1", "thread_id": "t1", "from_address": "parent@example.ca",
    "subject": "Horaire des pratiques", "headers": {"From": "parent@example.ca"},
    "body_snippet": "Bonjour, quel est l'horaire?",
    "date": "2026-06-12T08:15:00+00:00", "provider_category": None,
}
BODY = "Bonjour,\n\nQuel est l'horaire?\n\nEt le coût de l'inscription?"


class _Resolver:
    def __init__(self):
        self.asked = []

    def fetch_input_by_reference(self, ref):
        self.asked.append(ref)
        return dict(EMAIL)


class _Bodies:
    def fetch_bodies_by_ids(self, message_ids):
        return {message_id: BODY for message_id in message_ids}


class _Threads:
    def fetch_thread_history(self, thread_id, exclude_message_id):
        return []


class _Drafter:
    def __init__(self, existing=None):
        self.existing = existing
        self.deleted = []

    def find_draft(self, thread_id):
        return self.existing

    def delete_draft(self, draft_id):
        self.deleted.append(draft_id)


@pytest.fixture
def redraft(monkeypatch, tmp_path):
    stub_detector(monkeypatch)
    monkeypatch.setattr(club_memory, "MEMORY_DIR", tmp_path / "club-memory")
    module = load_script("redraft")
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "preflight_write", lambda provider: None)
    monkeypatch.setattr(module, "get_body_fetcher", lambda provider: _Bodies())
    monkeypatch.setattr(module, "get_thread_reader", lambda provider: _Threads())
    return module


def run_redraft(module, monkeypatch, tmp_path, drafter, resolver=None):
    monkeypatch.setattr(module, "get_reference_resolver",
                        lambda provider: resolver or _Resolver())
    monkeypatch.setattr(module, "get_drafter", lambda provider: drafter)
    out = tmp_path / "redraft.input.yaml"
    module.main([LINK, "--out", str(out), "--policy", str(write_policy(tmp_path))])
    return yaml.safe_load(out.read_text(encoding="utf-8"))


def test_the_standing_draft_is_deleted_before_a_new_one_is_written(
    redraft, monkeypatch, tmp_path
):
    drafter = _Drafter(existing="draft_1")

    run_redraft(redraft, monkeypatch, tmp_path, drafter)

    assert drafter.deleted == ["draft_1"]


def test_an_unusable_provider_refuses_before_the_draft_is_deleted(
    redraft, monkeypatch, tmp_path
):
    """STU-1128: this script clears the mailbox and the drafting run is a separate
    process, so a provider that cannot initialize would leave the conversation with no
    draft at all — the cost this path already accepts, paid for nothing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    drafter = _Drafter(existing="draft_1")
    monkeypatch.setattr(redraft, "get_reference_resolver", lambda provider: _Resolver())
    monkeypatch.setattr(redraft, "get_drafter", lambda provider: drafter)

    with pytest.raises(SystemExit) as refusal:
        redraft.main([LINK, "--out", str(tmp_path / "redraft.input.yaml"),
                      "--policy", str(write_policy(tmp_path)),
                      "--provider", "anthropic"])

    assert "anthropic" in str(refusal.value)
    assert drafter.deleted == []


def test_the_deletion_is_journaled_so_undo_stops_offering_that_draft(
    redraft, monkeypatch, tmp_path
):
    run_redraft(redraft, monkeypatch, tmp_path, _Drafter(existing="draft_1"))

    entries = read_entries(tmp_path / "logs/drafts/journal.jsonl")
    assert [(e["draft_id"], e["status"]) for e in entries] == [("draft_1", "deleted")]
    assert drafts_of_run(entries, {"m1"}) == {}


def test_a_conversation_with_no_draft_is_drafted_from_scratch(
    redraft, monkeypatch, tmp_path
):
    drafter = _Drafter(existing=None)

    item = run_redraft(redraft, monkeypatch, tmp_path, drafter)

    assert drafter.deleted == []
    assert not (tmp_path / "logs/drafts/journal.jsonl").exists()
    assert item["message_id"] == "m1"


def test_the_item_carries_the_conversation_and_the_full_body(
    redraft, monkeypatch, tmp_path
):
    item = run_redraft(redraft, monkeypatch, tmp_path, _Drafter())

    assert (item["message_id"], item["thread_id"]) == ("m1", "t1")
    assert item["body_text"] == BODY
    assert item["journal"] == str(tmp_path / "logs/drafts/journal.jsonl")


def test_the_link_is_what_the_mailbox_is_asked_for(redraft, monkeypatch, tmp_path):
    resolver = _Resolver()

    run_redraft(redraft, monkeypatch, tmp_path, _Drafter(), resolver)

    assert resolver.asked == [CONVERSATION]


def test_the_item_reuses_the_batch_keymap_so_a_parent_keeps_their_token(
    redraft, monkeypatch, tmp_path
):
    keymap = tmp_path / "logs/anonymization/keymap.json"
    keymap.parent.mkdir(parents=True)
    keymap.write_text(json.dumps({"EMAIL_7": "parent@example.ca"}), encoding="utf-8")

    item = run_redraft(redraft, monkeypatch, tmp_path, _Drafter())

    assert item["from_address"] == "EMAIL_7"
