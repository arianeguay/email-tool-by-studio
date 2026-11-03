"""IMAP read path (STU-2054): the Fetcher and the drafting reads.

Incoming mail is what sits in the Inbox and the Archive; the Archive holds the
older half of a conversation an earlier run filed. Sent, Drafts and Junk are
never incoming (STU-996), and the Trash only for a message this app put there
itself (`ec/deleted`, STU-983). A message in any other folder, one a human or a
Sieve rule filed, is not this app's to read or touch, exactly as Outlook's
`ELSEWHERE` (STU-1075).
"""

from datetime import datetime
from email.message import EmailMessage

from email_classifier.fetch.imap import projection
from email_classifier.fetch.imap.session import INBOX, ImapSession, RawMessage
from email_classifier.providers import trashed_by_us

_HISTORY_TURNS = 6


def keywords(flags) -> list[str]:
    """The message's keywords: the label axis, system flags (`\\Seen`, ...) excluded."""
    return sorted(flag for flag in flags if not flag.startswith("\\"))


def _imap_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d-%b-%Y")


def _in_thread(message: EmailMessage, thread_id: str) -> bool:
    return thread_id in [projection.message_id(message), *projection.ids(message.get("References"))]


class Mailbox:
    """Locates messages by `Message-ID` and conversations by their root id."""

    def __init__(self, session: ImapSession):
        self.session = session

    def folder(self, use: str) -> str:
        return INBOX if use == "Inbox" else self.session.special(use)

    def incoming_folders(self) -> list[str]:
        return [INBOX, self.folder("Archive")]

    # HEADER search is a substring match, and Migadu finds nothing in `References`
    # for an id written with its angle brackets (measured 2026-11-03). So ids are
    # searched bare and every hit is checked against the exact id once fetched.

    def locate(self, message_id: str, folders: list[str]) -> tuple[str, RawMessage] | None:
        for folder in folders:
            uids = self.session.search(folder, "HEADER", "Message-ID", f'"{message_id}"')
            for raw in self.session.fetch(folder, uids):
                if projection.message_id(projection.parse(raw.raw)) == message_id:
                    return folder, raw
        return None

    def conversation(self, thread_id: str, folders: list[str]) -> list[tuple[str, RawMessage]]:
        found = []
        for folder in folders:
            uids = self.session.search(
                folder, "OR", "HEADER", "Message-ID", f'"{thread_id}"',
                "HEADER", "References", f'"{thread_id}"',
            )
            found.extend(
                (folder, raw) for raw in self.session.fetch(folder, uids)
                if _in_thread(projection.parse(raw.raw), thread_id)
            )
        return found


class ImapFetcher:
    def __init__(self, session: ImapSession):
        self._mailbox = Mailbox(session)

    @property
    def _session(self) -> ImapSession:
        return self._mailbox.session

    def _incoming(self, folder: str, raw: RawMessage) -> bool:
        if "\\Draft" in raw.flags:
            return False
        if folder == self._mailbox.folder("Trash"):
            return trashed_by_us(keywords(raw.flags))
        return True

    def _readable(self) -> list[str]:
        return [*self._mailbox.incoming_folders(), self._mailbox.folder("Trash")]

    def _project(self, raw: RawMessage) -> dict | None:
        message = projection.parse(raw.raw)
        # A message with no Message-ID cannot be addressed again by apply or undo,
        # so it is left in the mailbox untouched rather than classified.
        if projection.message_id(message) is None:
            return None
        return projection.message_to_input(message, raw.received)

    def fetch_inputs(self, query: str, max_results: int,
                     since: str | None = None) -> list[dict]:
        """Newest incoming messages first, from the Inbox and the Archive.

        `query` is matched as IMAP `TEXT`. UIDs grow with arrival, so each folder's
        highest `max_results` are the only ones fetched.
        """
        criteria = [*(["SINCE", _imap_date(since)] if since else []),
                    *(["TEXT", f'"{query}"'] if query else [])] or ["ALL"]
        found: list[tuple] = []
        for folder in self._mailbox.incoming_folders():
            uids = self._session.search(folder, *criteria)[-max_results:]
            found.extend(
                (raw.received, projected)
                for raw in self._session.fetch(folder, uids)
                if "\\Draft" not in raw.flags and (projected := self._project(raw))
            )
        found.sort(key=lambda pair: pair[0], reverse=True)
        return [projected for _, projected in found[:max_results]]

    def fetch_inputs_by_ids(self, message_ids: list[str]) -> list[dict]:
        inputs = []
        for message_id in message_ids:
            located = self._mailbox.locate(message_id, self._readable())
            if located and self._incoming(*located):
                inputs.append(self._project(located[1]))
        return inputs

    def conversation_message_ids(self, thread_id: str) -> list[str]:
        return [
            projection.message_id(projection.parse(raw.raw))
            for folder, raw in self._mailbox.conversation(thread_id, self._readable())
            if self._incoming(folder, raw)
        ]

    def fetch_bodies_by_ids(self, message_ids: list[str]) -> dict[str, str]:
        bodies = {}
        for message_id in message_ids:
            located = self._mailbox.locate(message_id, self._readable())
            if located:
                bodies[message_id] = projection.message_body(projection.parse(located[1].raw))
        return bodies

    def _sent(self, thread_id: str) -> list[RawMessage]:
        return [raw for _, raw in self._mailbox.conversation(
            thread_id, [self._mailbox.folder("Sent")]
        )]

    def fetch_thread_history(self, thread_id: str, exclude_message_id: str) -> list[dict]:
        """The conversation's other turns, oldest first, the club's own replies included."""
        sent_folder = self._mailbox.folder("Sent")
        turns = []
        for folder, raw in self._mailbox.conversation(
            thread_id, [*self._mailbox.incoming_folders(), sent_folder]
        ):
            message = projection.parse(raw.raw)
            if "\\Draft" in raw.flags or projection.message_id(message) == exclude_message_id:
                continue
            turns.append((raw.received, message, folder == sent_folder))
        turns.sort(key=lambda turn: turn[0])
        return [
            projection.message_to_turn(message, received, sent)
            for received, message, sent in turns[-_HISTORY_TURNS:]
        ]

    def last_sent_at(self, thread_id: str) -> str | None:
        return max((projection.iso_date(raw.received) for raw in self._sent(thread_id)),
                   default=None)

    def sent_replies(self, thread_ids: list[str]) -> dict[str, dict]:
        replies = {}
        for thread_id in thread_ids:
            sent = self._sent(thread_id)
            if sent:
                newest = max(sent, key=lambda raw: raw.received)
                replies[thread_id] = {
                    "sent_at": projection.iso_date(newest.received),
                    "body": projection.message_body(projection.parse(newest.raw)),
                }
        return replies

    def sent_since(self, since: str) -> list[dict]:
        sent = self._mailbox.folder("Sent")
        return [
            projection.message_to_precedent(projection.parse(raw.raw), raw.received)
            for raw in self._session.fetch(sent, self._session.search(sent, "SINCE", _imap_date(since)))
            if "\\Draft" not in raw.flags
        ]

    def search_messages(self, query: str, max_results: int) -> list[dict]:
        """Prior mail matching `query`, sent replies included; never Trash or Junk."""
        hits: list[tuple] = []
        for folder in [*self._mailbox.incoming_folders(), self._mailbox.folder("Sent")]:
            uids = self._session.search(folder, "TEXT", f'"{query}"')[-max_results:]
            hits.extend(
                (raw.received, projection.parse(raw.raw))
                for raw in self._session.fetch(folder, uids)
                if "\\Draft" not in raw.flags
            )
        hits.sort(key=lambda hit: hit[0], reverse=True)
        return [projection.message_to_precedent(message, received)
                for received, message in hits[:max_results]]

    def reply_source(self, message_id: str) -> EmailMessage:
        located = self._mailbox.locate(message_id, self._readable())
        if located is None:
            raise LookupError(f"no message {message_id} in the incoming folders")
        return projection.parse(located[1].raw)
