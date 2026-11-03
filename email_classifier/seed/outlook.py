"""Graph side of the synthetic mail seeder (STU-845).

Messages are written straight into the Inbox folder
(`POST /me/mailFolders/inbox/messages`) instead of being sent: nothing leaves
the tenant, and the fixture's own sender and date land with the message.

One message is written into Sent Items instead: the turn that supersedes a
draft (STU-964). It is chained onto a seeded message the same way a seeded reply
is, and refused outright on a parent the seeder did not write.

Only fixtures are ever written. The seeder reads a mailbox message in exactly
three places — verifying what it just wrote, reading the parent a sent turn
answers, and reading back where a seeded message now sits before deleting it —
and reproduces no real mail anywhere.

Writing needs `Mail.ReadWrite`, which the seeder takes from the same grant and
the same token cache as every other write path (STU-844): one `make
outlook-consent`, and `preflight_write_scope` catches a read-only cache before
the first message rather than 403-ing partway through a batch.
"""

import os
from dataclasses import dataclass

from email_classifier.apply.outlook.drafts import OutlookDrafter
from email_classifier.core.types import EmailInput
from email_classifier.fetch.outlook import client

# The queryable marker: Graph can $filter on categories, but not on headers.
CATEGORY = "email-classifier-seed"

# The authoritative marker, carried inside the message itself. Teardown deletes
# on this alone, so a hand-categorized real message is never touched.
HEADER = "x-email-classifier-seed"

# Which fixture the message was drawn from. Mailbox ids are new on every round
# and say nothing to `fixtures-club/`; this is what makes an exported review
# replayable against the corpus (STU-875).
FIXTURE_HEADER = "x-email-classifier-fixture"

_MESSAGES = "/me/messages"
_INBOX = "/me/mailFolders/inbox/messages"
_SENT = "/me/mailFolders/sentitems/messages"
_PAGE_SIZE = 100

# The folders the seeder itself writes into. A seeded message sitting in one of
# them was put there by the seeder, not filed there by a human review (STU-875).
_SEED_FOLDERS = ("inbox", "sentitems")

# Where a seeded message's own Message-ID says it came from. Reserved by RFC 6761,
# so nothing here can ever name a host that resolves.
_SEED_DOMAIN = "seed.email-classifier.invalid"


def build_session() -> client.GraphSession:
    """Build a read-write Graph session, refusing early on a read-only grant."""
    refusal = client.preflight_write_scope()
    if refusal:
        raise RuntimeError(refusal)
    return client.build_session(scopes=client.WRITE_SCOPES)


def assert_test_mailbox() -> str:
    """Refuse to write unless the signed-in mailbox is the declared test one.

    Seeding a real inbox with 50 fake emails is one stale token cache away, and
    the declaration is mandatory: no default, no inference.

    The identity comes from the cached MSAL account, not from `GET /me` — the
    profile endpoint needs `User.Read`, which the seeder never consents to, so
    asking Graph 403s under a mail-only grant.
    """
    declared = (os.environ.get("OUTLOOK_SEED_MAILBOX") or "").strip().lower()
    if not declared:
        raise RuntimeError(
            "OUTLOOK_SEED_MAILBOX is not set — refusing to seed an undeclared mailbox."
        )
    signed_in = client.signed_in_address()
    if not signed_in:
        raise RuntimeError(
            f"no Outlook account cached in {client._token_path()} — run "
            f"`{client.CONSENT_COMMAND}` before seeding."
        )
    if signed_in != declared:
        raise RuntimeError(
            f"signed-in mailbox is {signed_in!r}, not OUTLOOK_SEED_MAILBOX ({declared!r}) "
            "— refusing to seed."
        )
    return signed_in


def internet_message_id(email: EmailInput, run_id: str) -> str:
    """The Message-ID a seeded message carries, unique to its round.

    The run id is in it because a second round seeds the same fixtures again: a
    Message-ID reused across rounds makes the new chain a continuation of the old
    one, and the two rounds land in the mailbox as a single conversation.
    """
    return f"<{email.message_id}.{run_id}@{_SEED_DOMAIN}>"


def _stamped(date: str) -> list[dict]:
    """The properties that make a folder write land as delivered mail, dated.

    A message POSTed to a folder is a draft (MSGFLAG_UNSENT set), and Graph
    refuses `createReply` on a draft — the drafts pipeline (STU-850) would get
    ErrorInvalidReferenceItem on every seeded mail. Writing PidTagMessageFlags=0
    at create time clears the unsent bit so the message lands as delivered mail;
    the bit cannot be cleared by a later PATCH.

    `receivedDateTime` and `sentDateTime` are stamped by the store on a folder
    write, whatever the payload asks for, and a later PATCH of them is ignored
    too (STU-889). The MAPI properties underneath them are not:
    PR_MESSAGE_DELIVERY_TIME and PR_CLIENT_SUBMIT_TIME survive the read-back,
    which is what keeps the corpus spread across the club year.
    """
    return [
        {"id": "Integer 0x0E07", "value": "0"},
        {"id": "SystemTime 0x0E06", "value": date},
        {"id": "SystemTime 0x0039", "value": date},
    ]


def _chained(message_id: str, references: tuple[str, ...]) -> list[dict]:
    """The properties that file a message on an earlier one's conversation (STU-942).

    They travel as MAPI properties for the same reason the seed markers do not:
    `In-Reply-To` and `References` are rejected by Graph's header allowlist,
    while PidTagInReplyToId and PidTagInternetReferences are ordinary properties
    the store reads — and reading them is what makes it file this message on the
    parent's `conversationId` instead of opening a new conversation.
    """
    properties = []
    if message_id:
        properties.append({"id": "String 0x1035", "value": message_id})
    if references:
        properties += [
            {"id": "String 0x1042", "value": references[-1]},
            {"id": "String 0x1039", "value": " ".join(references)},
        ]
    return properties


def to_graph(
    email: EmailInput,
    run_id: str,
    mailbox: str,
    message_id: str = "",
    references: tuple[str, ...] = (),
) -> dict:
    """Project a fixture to the Graph message resource written into the Inbox.

    The fixture carries no recipient — the neutral contract has no such field —
    so the seeded mailbox is addressed, which is what a delivered message would
    look like.

    Graph only accepts custom internet headers with an `x-` prefix — a probe
    POSTing List-Unsubscribe answers `400 InvalidInternetMessageHeader`
    (STU-858) — so those headers cannot be reproduced, and the seeded corpus
    classifies 0/24 deterministically. `divergence` reports the loss per
    message; `fixtures-club/README.md` says what that costs the corpus.

    `message_id` and `references` chain the message onto an earlier one (STU-942).
    """
    sender = {"emailAddress": {"address": email.from_address}}
    return {
        "subject": email.subject,
        "body": {"contentType": "text", "content": email.body_snippet},
        "from": sender,
        "sender": sender,
        "toRecipients": [{"emailAddress": {"address": mailbox}}],
        "isRead": False,
        "singleValueExtendedProperties": _stamped(email.date)
        + _chained(message_id, references),
        "categories": [CATEGORY],
        "internetMessageHeaders": [
            {"name": HEADER, "value": run_id},
            {"name": FIXTURE_HEADER, "value": email.message_id},
        ]
        + [
            {"name": name, "value": value}
            for name, value in email.headers.items()
            if name.lower().startswith("x-")
        ],
    }


def seed(
    session: client.GraphSession, emails: list[EmailInput], run_id: str, mailbox: str
) -> list[str]:
    """Write each fixture into the Inbox; return the mailbox ids, in order."""
    return [
        session.request("POST", _INBOX, payload=to_graph(email, run_id, mailbox))["id"]
        for email in emails
    ]


def seed_thread(
    session: client.GraphSession, messages: list[EmailInput], run_id: str, mailbox: str
) -> list[str]:
    """Write a conversation as a real reply chain; return the mailbox ids, in order.

    Each message answers every one before it, so the store threads them itself:
    it derives the `conversationId` and the conversation index from the reply
    properties, which is the grouping Outlook web shows. Writing the messages
    independently — what `seed` does — lands the same fixtures as three unrelated
    mails, and a thread that is not a thread proves nothing about thread handling.
    """
    ids: list[str] = []
    references: list[str] = []
    for message in messages:
        message_id = internet_message_id(message, run_id)
        payload = to_graph(message, run_id, mailbox, message_id, tuple(references))
        ids.append(session.request("POST", _INBOX, payload=payload)["id"])
        references.append(message_id)
    return ids


# What the seeded sent turn says. Nobody reads it for meaning: the drafts
# selection stops at its date, and the fixture thread it joins already carries the
# exchange. It only has to look like a human answering from the mailbox.
_SENT_BODY = "Merci, je vous ai répondu directement. Bonne journée!"

# Enough of the parent to answer it, plus the marker that says it is ours to answer.
_PARENT_SELECT = "internetMessageId,subject,from,internetMessageHeaders"


def sent_reply(parent: dict, mailbox: str, run_id: str, sent_at: str) -> dict:
    """The Sent Item answering `parent`, as a human replying from the mailbox leaves one.

    Written into Sent Items rather than sent: the grant is `Mail.ReadWrite`, and
    no path in this repo asks for `Mail.Send`. A plain folder write opens a new
    conversation, so the turn carries the same chaining properties `seed_thread`
    uses — a sent turn outside the draft's conversation says nothing about that
    draft (STU-964).

    `sent_at` lands through PidTagMessageDeliveryTime, which is what `last_sent_at`
    reads back as `receivedDateTime`. It is the whole point of the message: later
    than the draft's `lastModifiedDateTime` is what makes the draft superseded.
    """
    sender = {"emailAddress": {"address": mailbox}}
    parent_id = parent["internetMessageId"]
    subject = parent["subject"]
    return {
        "subject": subject if subject.lower().startswith("re:") else f"RE: {subject}",
        "body": {"contentType": "text", "content": _SENT_BODY},
        "from": sender,
        "sender": sender,
        "toRecipients": [
            {"emailAddress": {"address": parent["from"]["emailAddress"]["address"]}}
        ],
        "isRead": True,
        "singleValueExtendedProperties": _stamped(sent_at)
        + _chained(parent_id, (parent_id,)),
        "categories": [CATEGORY],
        "internetMessageHeaders": [{"name": HEADER, "value": run_id}],
    }


def seed_sent_reply(
    session: client.GraphSession, message_id: str, mailbox: str, sent_at: str
) -> tuple[str, str]:
    """Answer a seeded message from the mailbox; return the new id and its round.

    The round is read off the parent rather than asked for: the turn belongs to
    the round it joins, so `teardown --run` sweeps both together.

    A parent without the seed header is refused. This is the one seeder path whose
    target is named by hand, and the message it writes is a reply the mailbox owner
    never wrote — putting one inside a real conversation is not recoverable by
    saying sorry.
    """
    parent = session.get(f"/me/messages/{message_id}", {"$select": _PARENT_SELECT})
    run_id = _headers(parent).get(HEADER)
    if not run_id:
        raise RuntimeError(
            f"{message_id} carries no {HEADER} — refusing to answer a message the "
            "seeder did not write."
        )
    payload = sent_reply(parent, mailbox, run_id, sent_at)
    return session.request("POST", _SENT, payload=payload)["id"], run_id


_FIDELITY_FIELDS = ("from_address", "subject", "headers", "date")


def _flat(text: str) -> str:
    """The mailbox reflows what it stores, so bodies compare whitespace-insensitively."""
    return " ".join(text.split())


def divergence(fixture: EmailInput, landed: EmailInput) -> list[str]:
    """Fields the mailbox did not reproduce from the fixture.

    The body is checked as a prefix rather than for equality: the mailbox may
    store less than was sent, so a shorter body that agrees as far as it goes is
    not a loss. A body that came back empty, or altered before the cut, is.
    """
    lost = [
        field
        for field in _FIDELITY_FIELDS
        if getattr(fixture, field) != getattr(landed, field)
    ]
    sent, returned = _flat(fixture.body_snippet), _flat(landed.body_snippet)
    if not sent.startswith(returned) or (sent and not returned):
        lost.append("body_snippet")
    return lost


def find_marked(session: client.GraphSession) -> list[str]:
    """Ids of every message carrying the seed category, across all folders."""
    ids: list[str] = []
    url = _MESSAGES
    params: dict | None = {
        "$select": "id",
        "$top": _PAGE_SIZE,
        "$filter": f"categories/any(c:c eq '{CATEGORY}')",
    }
    while True:
        page = session.get(url, params)
        ids += [message["id"] for message in page.get("value", [])]
        url = page.get("@odata.nextLink")
        if not url:
            return ids
        # nextLink already carries every query option.
        params = None


def _headers(message: dict) -> dict[str, str]:
    return {
        header["name"].lower(): header["value"]
        for header in message.get("internetMessageHeaders") or []
    }


def _wears_header(message: dict) -> bool:
    return any(
        header["name"].lower() == HEADER
        for header in message.get("internetMessageHeaders") or []
    )


def find_standing(session: client.GraphSession) -> list[str]:
    """Ids of every seeded message the mailbox still holds, in any folder (STU-976).

    Standing means the mailbox, not the Inbox: a round a human filed into review
    folders (STU-875) is still there, still described by the golden and the dump,
    and still what a fresh round would stack onto. Scoping this to the Inbox let
    a bare `make seed` overwrite the golden out from under those messages, and
    dropped them from both artifacts under `--add` while `make eval-seeded` went
    on scoring the smaller set in silence. Nothing is gained by narrowing:
    Graph's DELETE is permanent, so a torn-down round is invisible to the wider
    scope too (STU-969).

    The header is read straight from the listing — Graph serves
    `internetMessageHeaders` under `$select` on a collection — so the whole
    pre-flight is one call, and a hand-categorized real message does not inflate
    the count.
    """
    ids: list[str] = []
    url = _MESSAGES
    params: dict | None = {
        "$select": "id,internetMessageHeaders",
        "$top": _PAGE_SIZE,
        "$filter": f"categories/any(c:c eq '{CATEGORY}')",
    }
    while True:
        page = session.get(url, params)
        ids += [message["id"] for message in page.get("value", []) if _wears_header(message)]
        url = page.get("@odata.nextLink")
        if not url:
            return ids
        params = None


@dataclass(frozen=True)
class Seeded:
    """One seeded message as the mailbox currently holds it."""

    message_id: str
    conversation_id: str
    folder_id: str
    fixture: str  # "" on a round seeded before FIXTURE_HEADER existed
    run_id: str


def _seeded(session: client.GraphSession, message_id: str) -> Seeded | None:
    """The message as a `Seeded` when it carries the seed header, else None."""
    message = session.get(
        f"/me/messages/{message_id}",
        {"$select": "conversationId,parentFolderId,internetMessageHeaders"},
    )
    headers = _headers(message)
    if HEADER not in headers:
        return None
    return Seeded(
        message_id=message_id,
        conversation_id=message["conversationId"],
        folder_id=message["parentFolderId"],
        fixture=headers.get(FIXTURE_HEADER, ""),
        run_id=headers[HEADER],
    )


def find_seeded(session: client.GraphSession) -> tuple[list[Seeded], list[str]]:
    """Every seeded message, and the ids wearing the category without the header."""
    seeded, skipped = [], []
    for message_id in find_marked(session):
        message = _seeded(session, message_id)
        if message is None:
            skipped.append(message_id)
        else:
            seeded.append(message)
    return seeded, skipped


def seed_folder_ids(session: client.GraphSession) -> set[str]:
    """Ids of the folders the seeder writes into — the ones a review moves out of."""
    return {
        session.get(f"/me/mailFolders/{name}", {"$select": "id"})["id"]
        for name in _SEED_FOLDERS
    }


def folder_names(session: client.GraphSession, folder_ids: list[str]) -> dict[str, str]:
    """Folder id -> display name, one lookup per distinct folder."""
    return {
        folder_id: session.get(
            f"/me/mailFolders/{folder_id}", {"$select": "displayName"}
        )["displayName"]
        for folder_id in dict.fromkeys(folder_ids)
    }


def review(session: client.GraphSession, seeded: list[Seeded]) -> list[dict]:
    """Where a human left each seeded message: the folder is the verdict (STU-875).

    Moving a message into a folder is the whole record of a mailbox review, so
    it is read back before anything deletes it — with the fixture named, since
    the mailbox ids expire with the round and `fixtures-club/` never knew them.
    """
    names = folder_names(session, [message.folder_id for message in seeded])
    return [
        {
            "message_id": message.message_id,
            "fixture": message.fixture,
            "folder": names[message.folder_id],
            "run_id": message.run_id,
        }
        for message in seeded
    ]


def moved_out(seeded: list[Seeded], seed_folders: set[str]) -> list[Seeded]:
    """The seeded messages a human filed somewhere the seeder never wrote."""
    return [message for message in seeded if message.folder_id not in seed_folders]


def teardown(session: client.GraphSession, seeded: list[Seeded]) -> list[str]:
    """Delete the given seeded messages; return the deleted ids.

    Graph's DELETE is permanent: the message leaves every folder `find_marked`
    spans and lands in the recoverable-items dumpster, never in Deleted Items
    (measured on the test mailbox, STU-969). A second teardown therefore finds
    nothing and deletes nothing. Nothing piles up either — the dumpster is on
    the mailbox's own retention, and Graph exposes no way to empty it. The apply
    writer trashes by folder move to stay reversible; a seeded message is meant
    to stop existing, so the seeder deletes.

    The reply draft a run wrote on the conversation goes with it. Left behind,
    it is a draft whose parent no longer exists: it reads as an unthreaded reply
    out of nowhere, and the next seeding round puts a fresh message with a new
    conversation id next to it.
    """
    drafter = OutlookDrafter(session)
    for message in seeded:
        draft_id = drafter.find_draft(message.conversation_id)
        if draft_id:
            drafter.delete_draft(draft_id)
        session.request("DELETE", f"/me/messages/{message.message_id}")
    return [message.message_id for message in seeded]
