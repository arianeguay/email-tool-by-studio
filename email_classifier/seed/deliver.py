"""SMTP delivery of fixtures to the test mailbox (STU-934).

The store-write seeder (`seed/outlook.py`) is the right tool for eval replay and
the wrong one for testing the push path: `POST /me/mailFolders/inbox/messages`
never touches the transport, so nothing produces the received chain, the
authentication results or the spam scoring a delivered message carries. This
module sends the same fixtures as ordinary mail from an outside account, so the
webhook sees what production will see.

Two fixture fields cannot survive the trip, and both are structural rather than
incidental:

- `from_address` — an authenticated submission relay rewrites or rejects a `From`
  it did not issue, and the fixture senders are `example-*` domains with no SPF
  or DKIM, so a forged one would be junked rather than delivered. The intended
  sender travels as `Reply-To` and `X-Email-Classifier-Fixture-From`.
- `date` — `receivedDateTime` is stamped by the store at delivery. The store-write
  path buys the fixture's own date with MAPI extended properties (STU-889);
  delivery has no such lever. `Date` is not backdated either: it would not move
  `receivedDateTime`, and a stale `Date` is a spam signal on the very path this
  is here to exercise.

What delivery buys in exchange is the headers a store-write cannot set: Graph
rejects a POSTed `List-Unsubscribe` with `400 InvalidInternetMessageHeader`
(STU-858), which is why the seeded club corpus classifies 0/24 deterministically.
Over SMTP those headers are just headers.

The seed markers are carried the same way as a seeded round — `x-email-classifier-seed`
and `x-email-classifier-fixture` — and the arrived messages are categorized on
arrival so `make unseed` sweeps a delivered round exactly like a seeded one.

A conversation is delivered as a real chain (STU-968), on plain RFC 5322 headers
rather than the MAPI properties the store-write path needed — Graph's header
allowlist governs a POST, not the transport. What the headers alone do not buy is
the grouping: `send_chain` says what the mailbox measurably requires on top.
"""

import os
import smtplib
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from email_classifier.core.types import EmailInput
from email_classifier.fetch.outlook import client
from email_classifier.seed.outlook import (
    CATEGORY,
    FIXTURE_HEADER,
    HEADER,
    divergence,
    internet_message_id,
)

# The fixture's intended sender, displaced from `From` by the relay's rewrite.
FIXTURE_FROM_HEADER = "x-email-classifier-fixture-from"

# The two fields no delivered message can carry, for the reasons in the module
# docstring. Reported once per round, not once per message.
STRUCTURAL_LOSSES = ("from_address", "date")

_INBOX = "/me/mailFolders/inbox/messages"
_PAGE_SIZE = 100


@dataclass(frozen=True)
class SmtpConfig:
    """Where the fixtures are sent from. No default account: the sender must be declared."""

    host: str
    port: int
    user: str
    password: str
    sender: str

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        missing = [
            name
            for name in ("DELIVER_SMTP_HOST", "DELIVER_SMTP_USER", "DELIVER_SMTP_PASSWORD")
            if not (os.environ.get(name) or "").strip()
        ]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} not set — delivery needs an outside account to "
                "send from. See 'Delivering fixtures as real inbound mail' in the README."
            )
        user = os.environ["DELIVER_SMTP_USER"].strip()
        return cls(
            host=os.environ["DELIVER_SMTP_HOST"].strip(),
            port=int(os.environ.get("DELIVER_SMTP_PORT") or 587),
            user=user,
            password=os.environ["DELIVER_SMTP_PASSWORD"],
            sender=(os.environ.get("DELIVER_FROM") or user).strip(),
        )


def assert_outside_account(config: SmtpConfig, mailbox: str) -> None:
    """Refuse to deliver from the mailbox under test.

    A message the mailbox sends to itself is not delivered mail: the store shortcuts
    it, and the received chain the whole exercise is about never exists.
    """
    if config.sender.lower() == mailbox.lower():
        raise RuntimeError(
            f"DELIVER_FROM is {config.sender!r}, the mailbox under test — a message a "
            "mailbox sends to itself never crosses the transport. Use an outside account."
        )


def build_message(
    email: EmailInput,
    run_id: str,
    sender: str,
    mailbox: str,
    references: tuple[str, ...] = (),
) -> EmailMessage:
    """Encode a fixture as the RFC 5322 message the relay submits.

    `Message-ID` is minted rather than left to the relay, and minted per round
    (`outlook.internet_message_id`), for the reason the seeder mints one: an id
    reused across rounds makes the second round's chain a continuation of the
    first, and the two land in the mailbox as one conversation.

    `references` are the ids of every earlier message of the same conversation,
    oldest first. Empty on a message that opens one.
    """
    message = EmailMessage()
    message["From"] = sender
    message["To"] = mailbox
    message["Subject"] = email.subject
    if email.from_address:
        message["Reply-To"] = email.from_address
        message[FIXTURE_FROM_HEADER] = email.from_address
    # Stamped now, not with the fixture's date. `receivedDateTime` is the store's
    # either way, and a `Date` nine days stale is a spam signal on the very path
    # this is here to exercise.
    message["Date"] = format_datetime(datetime.now(timezone.utc))
    message["Message-ID"] = internet_message_id(email, run_id)
    if references:
        message["In-Reply-To"] = references[-1]
        message["References"] = " ".join(references)
    message[HEADER] = run_id
    message[FIXTURE_HEADER] = email.message_id
    for name, value in email.headers.items():
        message[name] = value
    message.set_content(email.body_snippet)
    return message


def build_chain(
    messages: list[EmailInput], run_id: str, sender: str, mailbox: str
) -> list[EmailMessage]:
    """Encode a fixture conversation as a reply chain, in arrival order (STU-968).

    Each message references every one before it. That is what the store groups
    them on, and it is not enough on its own — `send_chain` carries what else the
    grouping needs.
    """
    built, references = [], []
    for email in messages:
        built.append(build_message(email, run_id, sender, mailbox, tuple(references)))
        references.append(built[-1]["Message-ID"])
    return built


@contextmanager
def relay(config: SmtpConfig):
    """One authenticated STARTTLS session, held open for the whole round.

    Yields the submit callable rather than the connection: a round paces itself
    against the mailbox between submissions, and reconnecting per message would
    make a flat round of two dozen fixtures two dozen logins.
    """
    with smtplib.SMTP(config.host, config.port, timeout=60) as smtp:
        smtp.starttls()
        smtp.login(config.user, config.password)
        yield smtp.send_message


def send_chain(
    submit,
    session: client.GraphSession,
    messages: list[EmailInput],
    run_id: str,
    sender: str,
    mailbox: str,
    timeout: float = 180.0,
    interval: float = 5.0,
) -> list[str]:
    """Deliver a conversation turn by turn; return the turns whose parent never landed.

    The chaining headers are necessary and not sufficient. Measured against the
    test mailbox (STU-968): a reply submitted while its parent is still in flight
    opens its own conversation, `In-Reply-To` and `References` intact, and the
    store does not revisit that grouping when the parent lands a second later.
    The same reply submitted once the parent is visible in the Inbox joins it.
    So the store resolves the reference against what it already holds, and a
    chain is paced against arrivals rather than submitted in one burst.

    A turn whose parent never arrives is submitted anyway: it will open its own
    conversation, which the round's threading report names, and holding it back
    would hide half the conversation on top of that.
    """
    built = build_chain(messages, run_id, sender, mailbox)
    unwaited = []
    for email, message in zip(messages[:-1], built):
        submit(message)
        if not await_message(session, run_id, email.message_id, timeout, interval):
            unwaited.append(email.message_id)
    submit(built[-1])
    return unwaited


def _headers(message: dict) -> dict[str, str]:
    return {
        header["name"].lower(): header["value"]
        for header in message.get("internetMessageHeaders") or []
    }


def arrived(session: client.GraphSession, run_id: str) -> dict[str, str]:
    """Fixture name -> mailbox id, for the delivered messages of that run.

    Keyed by fixture rather than listed: a relay may deliver out of order, and
    the corpus is what the caller has to diff against. Header-matched, because
    Graph cannot `$filter` on a header — and matched on the run id, so a standing
    round from an earlier delivery is not counted as this one's.
    """
    found: dict[str, str] = {}
    url = _INBOX
    params: dict | None = {
        "$select": "id,internetMessageHeaders",
        "$top": _PAGE_SIZE,
        "$orderby": "receivedDateTime desc",
    }
    while True:
        page = session.get(url, params)
        for message in page.get("value", []):
            headers = _headers(message)
            if headers.get(HEADER) == run_id:
                found[headers.get(FIXTURE_HEADER, "")] = message["id"]
        url = page.get("@odata.nextLink")
        if not url:
            return found
        params = None


def await_arrival(
    session: client.GraphSession,
    run_id: str,
    expected: int,
    timeout: float = 300.0,
    interval: float = 10.0,
    now=time.monotonic,
    sleep=time.sleep,
) -> tuple[dict[str, str], float]:
    """Poll the Inbox until the whole round has landed; return the ids and the wait.

    Returns what did arrive when the timeout runs out rather than raising: a
    partial delivery is a result about the transport, and the caller names which
    fixtures are missing.
    """
    started = now()
    while True:
        found = arrived(session, run_id)
        waited = now() - started
        if len(found) >= expected or waited >= timeout:
            return found, waited
        sleep(interval)


def await_message(
    session: client.GraphSession,
    run_id: str,
    fixture: str,
    timeout: float = 180.0,
    interval: float = 5.0,
    now=time.monotonic,
    sleep=time.sleep,
) -> bool:
    """Whether that one fixture is visible in the Inbox before the timeout.

    Named rather than counted: what a reply waits on is its own parent, and a
    straggler from an earlier conversation of the same round would satisfy a
    count while leaving the parent still in flight.
    """
    started = now()
    while True:
        if fixture in arrived(session, run_id):
            return True
        if now() - started >= timeout:
            return False
        sleep(interval)


def categorize(session: client.GraphSession, message_ids: list[str]) -> None:
    """Mark the arrived messages with the seed category.

    Teardown queries on the category — Graph can `$filter` on it and not on a
    header — so without this a delivered round is invisible to `make unseed`,
    while still carrying the header that authorizes deleting it.
    """
    for message_id in message_ids:
        session.request(
            "PATCH", f"/me/messages/{message_id}", payload={"categories": [CATEGORY]}
        )


def unexpected_divergence(fixture: EmailInput, landed: EmailInput) -> list[str]:
    """Fields delivery lost beyond the two it structurally cannot carry."""
    return [field for field in divergence(fixture, landed)
            if field not in STRUCTURAL_LOSSES]


_COMPARED_FIELDS = (
    "from_address",
    "subject",
    "headers",
    "body_snippet",
    "date",
    "provider_category",
)


def _flat(text: str) -> str:
    return " ".join(text.split())


def compare(delivered: list[dict], seeded: list[dict]) -> tuple[list[dict], list[str]]:
    """Field-by-field diff of a delivered round against its seeded counterpart.

    Joined on subject: both dumps are keyed by mailbox id, which is new on every
    round and says nothing across them, and the fixture header is stripped by the
    projection's privacy allowlist before either dump is written.

    Returns the per-subject differences and the subjects only one side holds.
    """
    by_subject = {item["subject"]: item for item in seeded}
    differences, unmatched = [], []
    for item in delivered:
        counterpart = by_subject.pop(item["subject"], None)
        if counterpart is None:
            unmatched.append(item["subject"])
            continue
        fields = {
            field: (counterpart[field], item[field])
            for field in _COMPARED_FIELDS
            if _differs(field, counterpart[field], item[field])
        }
        if fields:
            differences.append({"subject": item["subject"], "fields": fields})
    return differences, unmatched + list(by_subject)


def _differs(field: str, seeded, delivered) -> bool:
    """Whitespace-insensitive on the body: the two paths reflow it differently."""
    if field == "body_snippet":
        return _flat(seeded) != _flat(delivered)
    return seeded != delivered
