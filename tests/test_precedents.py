"""The precedent-mail tool (STU-872). No mailbox: the searcher is a fake.

The redaction tests go through `redact` with a format-only tokenizer rather than
through `search`, so the safety property stays provable without the `ner` extra —
what they check is the second pass, which is what catches everything GLiNER is not
asked to find.
"""

import importlib.util
from pathlib import Path

import pytest
import yaml

from email_classifier import precedents
from email_classifier.anonymize import Tokenizer, deanonymize
from email_classifier.fetch.outlook import OutlookFetcher

requires_gliner = pytest.mark.skipif(
    importlib.util.find_spec("gliner") is None, reason="needs the `ner` extra"
)

# A parent asking, and the club answering — the two halves a precedent is made of.
HITS = [
    {
        "date": "2025-09-12T13:04:00+00:00",
        "body": (
            "Bonjour Marie Tremblay,\n"
            "Ma fille Léa joue U15 et nous devons annuler. "
            "Écrivez-moi à marie.tremblay@example.com ou au 514-555-0182."
        ),
    },
    {
        "date": "2025-09-13T09:20:00+00:00",
        "body": (
            "Bonjour Marie Tremblay, le remboursement est traité sous 10 jours ouvrables.\n"
            "Payeur : Isabelle Bergeron — inscrire Nathan au mini-volley le samedi."
        ),
    },
]

LEAKS = ["Marie", "Tremblay", "Léa", "Isabelle", "Bergeron", "Nathan",
         "marie.tremblay@example.com", "514-555-0182"]


class _Searcher:
    def __init__(self, hits: list[dict]):
        self.hits = hits
        self.calls: list[tuple[str, int]] = []

    def search_messages(self, query: str, max_results: int) -> list[dict]:
        self.calls.append((query, max_results))
        return self.hits[:max_results]


def _redacted(index: int) -> str:
    return precedents.redact(HITS[index]["body"], Tokenizer(persons=False))


def test_no_name_address_or_phone_leaves_the_tool():
    bodies = "\n".join(_redacted(i) for i in range(len(HITS)))

    for leak in LEAKS:
        assert leak not in bodies, f"{leak!r} reached the LLM in the clear"


@requires_gliner
def test_the_date_survives_so_a_precedent_can_be_cited():
    result = precedents.search("remboursement", _Searcher(HITS))

    assert [m["date"] for m in result["matches"]] == [h["date"] for h in HITS]
    assert "remboursement est traité sous 10 jours" in result["matches"][1]["body"]


def test_a_precedent_token_is_not_restored_to_the_batch_s_person():
    # `write-draft` de-tokenizes the draft against the batch keymap. A precedent's
    # PERSON_1 is somebody else entirely, so it must not survive as one.
    body = _redacted(0)

    assert deanonymize(body, {"PERSON_1": "Chantal Roy"}) == body


@requires_gliner
def test_a_long_precedent_is_truncated():
    long_hit = [{"date": HITS[0]["date"], "body": "a" * (precedents.BODY_LIMIT + 500)}]

    body = precedents.search("x", _Searcher(long_hit))["matches"][0]["body"]

    assert len(body) == precedents.BODY_LIMIT


@requires_gliner
def test_the_search_is_bounded_by_default():
    searcher = _Searcher(HITS)

    precedents.search("remboursement", searcher)

    assert searcher.calls == [("remboursement", precedents.MATCH_LIMIT)]


class _Session:
    def __init__(self, messages: dict[str, dict], folders: dict[str, str] | None = None):
        self.messages = messages
        self.folders = folders or {}  # message id -> well-known folder name
        self.gets: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None, prefer: list | None = None) -> dict:
        self.gets.append((url, params or {}))
        if url.startswith("/me/mailFolders('"):
            return {"id": url.split("'")[1] + "-id"}
        if url == "/me/messages":
            return {"value": [
                {"id": message_id,
                 "parentFolderId": f"{self.folders.get(message_id, 'archive')}-id"}
                for message_id in self.messages
            ]}
        return self.messages[url.rsplit("/", 1)[-1]]


def _message(message_id: str, body: str) -> dict:
    return {
        "id": message_id,
        "receivedDateTime": "2025-09-13T09:20:00Z",
        "uniqueBody": {"content": body},
    }


def test_the_whole_mailbox_is_searched_sent_replies_included():
    # `/me/messages` is every folder; a folder-scoped path would miss exactly the
    # half a precedent is wanted for — what the club itself wrote back.
    session = _Session({
        "sent-1": _message("sent-1", "Le remboursement est traité sous 10 jours.")
    })

    hits = OutlookFetcher(session).search_messages("remboursement", 5)

    assert hits == [{"date": "2025-09-13T09:20:00+00:00",
                     "body": "Le remboursement est traité sous 10 jours."}]
    listing = next(get for get in session.gets if get[0] == "/me/messages")
    assert listing[1]["$search"] == '"remboursement"'
    message = session.gets[-1]
    # The privacy allowlist at the request boundary: no sender, no recipients.
    assert message[1]["$select"] == "id,receivedDateTime,uniqueBody"


def test_a_deleted_or_junked_message_is_not_a_precedent():
    # STU-965: what somebody deleted, and what Outlook filed as junk, is the last
    # thing to show the drafter as an example of how the club writes.
    session = _Session(
        {
            "kept": _message("kept", "Le remboursement est traité sous 10 jours."),
            "trashed": _message("trashed", "Le remboursement est immédiat."),
            "junked": _message("junked", "REMBOURSEMENT GARANTI cliquez ici"),
        },
        folders={"trashed": "deleteditems", "junked": "junkemail"},
    )

    hits = OutlookFetcher(session).search_messages("remboursement", 5)

    assert [hit["body"] for hit in hits] == ["Le remboursement est traité sous 10 jours."]


def _studio(relative: str) -> dict:
    root = Path(__file__).resolve().parents[1] / ".studio"
    return yaml.safe_load((root / relative).read_text(encoding="utf-8"))


def test_the_agent_chooses_the_words_and_not_the_mailbox():
    command = _studio("tools/mail-precedents.tool.yaml")["commands"][0]

    assert set(command["parameters"]) == {"query"}
