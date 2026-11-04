"""Provider dispatch (STU-324): config picks the mailbox implementation.

Orchestration (run_email, executor) talks to `Fetcher` / `Writer` only; the
provider name comes from `email.provider` in the policy file. Gmail (STU-405),
Outlook (STU-847) and IMAP (STU-2054) are full read-write paths; an unknown
provider fails loud rather than falling back to any of them.

Ontology mapping — how a neutral operation lands on each provider:

| Neutral       | Gmail                              | Outlook (Graph)                            | IMAP                       |
|---------------|------------------------------------|--------------------------------------------|----------------------------|
| ensure_label  | labels.create/list -> labelId      | category (mailbox master category)         | keyword (the name itself)  |
| add_label     | messages.modify addLabelIds        | add to `categories`                        | STORE +FLAGS keyword       |
| remove_label  | messages.modify removeLabelIds     | remove from `categories`                   | STORE -FLAGS keyword       |
| remove INBOX  | removeLabelIds ["INBOX"]           | move to a folder (Archive)                 | MOVE to `\\Archive`        |
| trash         | messages.trash                     | move to Deleted Items (`/move` to folder)  | MOVE to `\\Trash`          |
| draft         | drafts API                         | Drafts folder                              | APPEND to `\\Drafts`       |

The asymmetry that shapes the interface: Gmail labels are many-per-message and
orthogonal to location, Outlook folders are one-per-message. So "label" maps to
Outlook *categories*, not folders — categories are many-per-message, which is
what `ec/<bucket>` + `ec/processed` need. Archiving is then a separate folder
move, which is why the executor's archive step is label+unlabel-INBOX in Gmail
terms: the Outlook writer implements `remove_label("INBOX")` as a folder move.

Push notifications are NOT abstracted here: Gmail (watch + history.list) and
Graph (subscriptions, ids carried in the notification) differ in shape,
retention and state, so `fetch/gmail/push.py` and `fetch/outlook/push.py` stay
provider-shaped and the webhook exposes one route per provider.
"""

import os
from collections.abc import Iterable
from typing import Protocol

# Pseudo-labels for the two locations the executor reads off a message. Gmail
# spells both as system labels, so its ids are the names themselves; the Outlook
# writer tells the same fiction off `parentFolderId`. Location has to sit on the
# label axis because that is the axis apply diffs a message's state on (STU-963).
INBOX_LABEL = "INBOX"
TRASH_LABEL = "TRASH"

# The third answer on that axis (STU-1075): the mailbox holds the message, but in
# none of the three places this app puts mail — not the Inbox it reads from, not
# the Archive it files to, not the trash. A hard delete lands there (Graph keeps
# the message in the Deletions dumpster, whose folder id matches nothing known),
# and so does a message a human filed into a folder of their own. Both are
# "not ours to touch", which is why one label serves for both.
#
# Only the Outlook writer ever reports it. Gmail has no folders to be outside of,
# and a message it no longer holds answers 404 rather than a location.
ELSEWHERE_LABEL = "ELSEWHERE"

# What this app writes on a message it has acted on. Only this app writes the
# `ec/` prefix, which is what makes these labels a record rather than a guess.
PROCESSED_LABEL = "ec/processed"

# The record that this app moved a message to the trash (STU-983), written before
# the move. `ec/<bucket>` says "we filed it here"; nothing said "we threw it out",
# and no location label can — a label reports *that* a message is in the trash,
# never *who* put it there. Inferring it from the absence of a bucket does not
# work: that is also the state of every message applied as keep_visible.
DELETED_LABEL = "ec/deleted"


def trashed_by_us(labels: Iterable[str]) -> bool:
    """True when a message's own labels say this app moved it to the trash.

    Read in both directions. In the trash it means the message is ours to bring
    back when a reply reopens its conversation; out of the trash it means somebody
    put it back — or a run died before the move — and either way this app does not
    throw it out a second time.
    """
    # Casefolded, like `GmailWriter.find_label`: Gmail refuses to create a label
    # differing from an existing one only by case, so the mailbox may hold the
    # label under a spelling this app never wrote.
    return DELETED_LABEL in {name.casefold() for name in labels}


class Fetcher(Protocol):
    """Read path. Returns provider-neutral EmailInput dicts (core/schema.py)."""

    # `since` is the run's date floor as ISO 8601 UTC (STU-1123) — neutral here, and
    # translated by each provider into its own query language. A hint only: it keeps
    # `max_results` off mail the run will drop, while `fetch.window.in_scope` stays
    # the boundary, since neither language expresses it exactly.
    def fetch_inputs(self, query: str, max_results: int,
                     since: str | None = None) -> list[dict]: ...

    def fetch_inputs_by_ids(self, message_ids: list[str]) -> list[dict]: ...


class ConversationReader(Protocol):
    """A conversation's live message ids (STU-963).

    What lets a report hold a whole thread instead of the one message a
    notification named. Live means what the mailbox still shows: drafts, Deleted
    Items and Junk are the fetcher's to drop, so the caller never has to know
    each provider's word for "gone".
    """

    def conversation_message_ids(self, thread_id: str) -> list[str]: ...


class BodyFetcher(Protocol):
    """Full-body read (STU-852). Drafting only — classification runs on the preview."""

    def fetch_bodies_by_ids(self, message_ids: list[str]) -> dict[str, str]: ...


class ReferenceResolver(Protocol):
    """Link -> EmailInput. What lets a human name one conversation by hand."""

    def fetch_input_by_reference(self, ref: str) -> dict: ...


class ThreadReader(Protocol):
    """The answered conversation's earlier turns (STU-941).

    Turns are `{date, direction, body}`, oldest first, `direction` being `sent`
    when the club wrote it. Distinct from `PrecedentSearcher`, which finds other
    people's mail by keyword: this is the same exchange, so its text keeps the
    batch's indexed tokens instead of being flattened.

    `last_sent_at` is the same conversation read down to one ISO 8601 UTC date —
    what the drafts selection needs to tell an answered thread from a waiting one
    (STU-945) without paying for the turns of a thread it will skip.
    """

    def fetch_thread_history(self, thread_id: str, exclude_message_id: str) -> list[dict]: ...

    def last_sent_at(self, thread_id: str) -> str | None: ...


class PrecedentSearcher(Protocol):
    """Prior-mail search (STU-872). Hits are `{date, body}`, sent replies included."""

    def search_messages(self, query: str, max_results: int) -> list[dict]: ...


class SentReader(Protocol):
    """The club's sent replies since an ISO 8601 UTC instant, as `{date, body}` (STU-2001)."""

    def sent_since(self, since: str) -> list[dict]: ...


class Writer(Protocol):
    """Write path. Label ids are opaque provider handles from `ensure_label`.

    `label_ids` answers the location on the label axis too — `INBOX_LABEL`,
    `TRASH_LABEL` and `ELSEWHERE_LABEL` — because that is the axis apply diffs a
    message's state on.
    """

    def find_label(self, name: str) -> str | None: ...

    def ensure_label(self, name: str) -> str: ...

    def add_label(self, message_id: str, label_id: str) -> None: ...

    def remove_label(self, message_id: str, label_id: str) -> None: ...

    def trash(self, message_id: str) -> None: ...

    def untrash(self, message_id: str) -> None: ...

    def label_ids(self, message_id: str) -> list[str]: ...


class Drafter(Protocol):
    """Draft path (STU-850). Creates reply drafts; nothing here ever sends.

    `get_draft` is the read `undo` needs to tell a draft the run left alone from
    one a human has since touched (STU-854). Each provider projects its own
    message into the same neutral shape — `{id, isDraft, lastModifiedDateTime}`
    — and answers None when the draft is no longer in the mailbox.

    `reply_to`/`reply_name` override the recipient the parent message implies
    (STU-888); absent, each provider replies to it the ordinary way.
    """

    def upsert_reply_draft(
        self,
        message_id: str,
        thread_id: str,
        body: str,
        reply_to: str | None = None,
        reply_name: str | None = None,
    ) -> dict: ...

    def get_draft(self, draft_id: str) -> dict | None: ...

    def delete_draft(self, draft_id: str) -> None: ...

    def find_draft(self, thread_id: str) -> str | None: ...


# Set on a process with no human at a browser. The webhook passes `interactive=False`
# to the clients it builds itself (STU-930), but it also spawns `studio` children that
# build their own — `drafts-pre` a drafter and a thread reader, the drafting tools their
# own sessions — and an argument does not reach a subprocess (STU-946).
HEADLESS_ENV = "EMAIL_CLASSIFIER_HEADLESS"


def _interactive(explicit: bool | None) -> bool:
    """Whether a dead grant may be restored by opening a browser.

    An explicit argument wins; otherwise the environment decides. Left interactive on
    a headless host, a lost grant blocks on a consent window nobody will ever see —
    which is indistinguishable from a service at rest.
    """
    if explicit is not None:
        return explicit
    return os.environ.get(HEADLESS_ENV) != "1"


def _gmail_service(interactive: bool | None = None):
    from email_classifier.fetch import gmail

    return gmail.build_service(gmail.get_credentials(interactive=_interactive(interactive)))


def _graph_session():
    from email_classifier.fetch import outlook

    return outlook.build_session(interactive=_interactive(None))


def _graph_write_session(interactive: bool | None = None):
    from email_classifier.fetch import outlook

    return outlook.build_session(
        scopes=outlook.WRITE_SCOPES, interactive=_interactive(interactive)
    )


def _imap_session():
    from email_classifier.fetch import imap

    return imap.build_session()


def configured_mailbox(provider: str) -> str | None:
    """Which mailbox this host addresses under `provider`, or None when nothing names one.

    Asked of the client that would do the reading rather than of a second declaration
    beside it (STU-1183): a report naming one mailbox while the fetcher opened another
    is the whole failure this answers, and two declarations is how that happens.

    Gmail answers None by construction, and that null is a fact rather than a gap:
    nothing in the configuration names a Gmail mailbox — every call is `userId="me"`,
    whoever the cached token belongs to — and the only way to learn the address is
    `users.getProfile`, a grant-bearing call that recording provenance must not make.

    Unknown providers fail loud here as they do at every other dispatcher in this
    module. A null would read as "nothing names a mailbox", which for a provider this
    function has never heard of is a guess, not an answer.
    """
    if provider == "gmail":
        return None
    if provider == "outlook":
        from email_classifier.fetch import outlook

        return outlook.configured_mailbox()
    if provider == "imap":
        from email_classifier.fetch import imap

        return imap.configured_mailbox()
    raise ValueError(f"no mailbox for email provider {provider!r}")


def get_sent_source(name: str):
    """A second mailbox's Sent folder, read only (STU-2082). Gmail is the only one."""
    if name == "gmail":
        from email_classifier.fetch import gmail

        return gmail.GmailSentSource(_gmail_service())
    raise ValueError(f"no sent source {name!r}")


def get_fetcher(provider: str) -> Fetcher:
    if provider == "gmail":
        from email_classifier.fetch import gmail

        return gmail.GmailFetcher(_gmail_service())
    if provider == "outlook":
        from email_classifier.fetch import outlook

        return outlook.OutlookFetcher(_graph_session())
    if provider == "imap":
        from email_classifier.fetch import imap

        return imap.ImapFetcher(_imap_session())
    raise ValueError(f"no fetcher for email provider {provider!r}")


# Drafting takes more than a drafter: before an agent writes anything, `drafts-pre`
# reads full bodies, the thread's earlier turns and prior mail, and only Outlook
# implements those three. So `drafts` on Gmail stops at `get_body_fetcher` rather
# than at `get_drafter`, which Gmail does implement (STU-853).
_DRAFTING_PROVIDERS = frozenset({"outlook", "imap"})


def can_draft(provider: str) -> bool:
    """True when `provider` has every read the drafting pipeline needs.

    The three factories below refuse on exactly this, so a caller that decides
    ahead of them cannot drift from the fact they enforce (STU-1002).
    """
    return provider in _DRAFTING_PROVIDERS


def _drafting_reader(provider: str, role: str):
    """The fetcher every drafting read goes through, on a provider that has them all."""
    if not can_draft(provider):
        raise ValueError(f"no {role} for email provider {provider!r}")
    if provider == "imap":
        from email_classifier.fetch import imap

        return imap.ImapFetcher(_imap_session())
    from email_classifier.fetch import outlook

    return outlook.OutlookFetcher(_graph_session())


def get_body_fetcher(provider: str) -> BodyFetcher:
    return _drafting_reader(provider, "body fetcher")


def get_reference_resolver(provider: str) -> ReferenceResolver:
    """Outlook-only, like `get_body_fetcher`: `redraft` is the only caller and the
    link it parses is an Outlook web URL."""
    if provider == "outlook":
        from email_classifier.fetch import outlook

        return outlook.OutlookFetcher(_graph_session())
    raise ValueError(f"no reference resolver for email provider {provider!r}")


def get_thread_reader(provider: str) -> ThreadReader:
    return _drafting_reader(provider, "thread reader")


def get_precedent_searcher(provider: str) -> PrecedentSearcher:
    return _drafting_reader(provider, "precedent searcher")


def get_sent_reader(provider: str) -> SentReader:
    return _drafting_reader(provider, "sent reader")


def get_writer(provider: str, interactive: bool | None = None) -> Writer:
    """`interactive=False` for a caller with no human at a browser — the webhook.
    A grant that no longer redeems raises `GrantLost` there (STU-930 on Graph,
    STU-948 on Gmail) instead of blocking the service on a consent window nobody
    will see."""
    if provider == "gmail":
        from email_classifier.apply.gmail import GmailWriter

        return GmailWriter(_gmail_service(interactive))
    if provider == "outlook":
        from email_classifier.apply.outlook import OutlookWriter

        return OutlookWriter(_graph_write_session(interactive))
    if provider == "imap":
        from email_classifier.apply.imap.writer import ImapWriter

        return ImapWriter(_imap_session())
    raise ValueError(f"no writer for email provider {provider!r}")


def get_drafter(provider: str) -> Drafter:
    if provider == "gmail":
        from email_classifier.apply.gmail import GmailDrafter

        return GmailDrafter(_gmail_service())
    if provider == "outlook":
        from email_classifier.apply.outlook import OutlookDrafter

        return OutlookDrafter(_graph_write_session())
    if provider == "imap":
        from email_classifier.apply.imap.drafts import ImapDrafter

        return ImapDrafter(_imap_session())
    raise ValueError(f"no drafter for email provider {provider!r}")


def preflight_write(provider: str) -> str | None:
    """None when the provider's cached grant may mutate the mailbox, else why not.

    Gmail needs no check: its client discards a token narrower than `gmail.modify`
    and re-consents (STU-405). Graph grants are per-scope and long-lived, so a
    read-only Outlook token has to be caught before the first mutation (STU-844).
    """
    if provider == "outlook":
        from email_classifier.fetch import outlook

        return outlook.preflight_write_scope()
    return None
