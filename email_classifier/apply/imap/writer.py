"""IMAP mutation primitives (STU-2054), mirroring `apply/outlook/writer.py`.

A neutral label is an IMAP **keyword**: many-per-message and orthogonal to
location, as `ec/<bucket>` plus `ec/processed` need. `INBOX` and `TRASH` are the
message's folder, so archiving is a `MOVE` to the `\\Archive` folder and its
inverse a move back. Trash is a move to `\\Trash`, never an expunge.

The message is found again by `Message-ID` on every call: a move gives it a new
UID, so a UID is never what the journal or undo holds.
"""

from email.utils import parseaddr

from email_classifier.fetch.headers import allowlisted
from email_classifier.fetch.imap import projection
from email_classifier.fetch.imap.client import Mailbox, keywords
from email_classifier.fetch.imap.session import INBOX
from email_classifier.providers import (
    ELSEWHERE_LABEL as _ELSEWHERE,
    INBOX_LABEL as _INBOX,
    TRASH_LABEL as _TRASH,
)


class ImapWriter:
    def __init__(self, session):
        self._mailbox = Mailbox(session)

    def _places(self) -> dict[str, str | None]:
        """Every folder this app files to, and the location label it reads back as."""
        return {
            INBOX: _INBOX,
            self._mailbox.folder("Archive"): None,
            self._mailbox.folder("Trash"): _TRASH,
        }

    def _locate(self, message_id: str):
        located = self._mailbox.locate(message_id, list(self._places()))
        if located is None:
            raise LookupError(f"message {message_id} is in no folder this app files to")
        return located

    def _move(self, message_id: str, destination: str) -> None:
        folder, raw = self._locate(message_id)
        if folder != destination:
            self._mailbox.session.move(folder, raw.uid, destination)

    def find_label(self, name: str) -> str | None:
        """A keyword needs no registration: the name is the handle."""
        return name

    def ensure_label(self, name: str) -> str:
        return name

    def add_label(self, message_id: str, label_id: str) -> None:
        if label_id == _INBOX:
            self._move(message_id, INBOX)
            return
        folder, raw = self._locate(message_id)
        if label_id not in raw.flags:
            self._mailbox.session.add_flags(folder, raw.uid, label_id)

    def remove_label(self, message_id: str, label_id: str) -> None:
        if label_id == _INBOX:
            folder, raw = self._locate(message_id)
            if folder == INBOX:
                self._mailbox.session.move(folder, raw.uid, self._mailbox.folder("Archive"))
            return
        folder, raw = self._locate(message_id)
        if label_id in raw.flags:
            self._mailbox.session.remove_flags(folder, raw.uid, label_id)

    def trash(self, message_id: str) -> None:
        self._move(message_id, self._mailbox.folder("Trash"))

    def untrash(self, message_id: str) -> None:
        """Back to the Inbox: the executor only ever trashes mail it read from there."""
        self._move(message_id, INBOX)

    def label_ids(self, message_id: str) -> list[str]:
        places = self._places()
        located = self._mailbox.locate(message_id, list(places))
        if located is None:
            return [_ELSEWHERE]
        folder, raw = located
        label = places[folder]
        return keywords(raw.flags) + ([label] if label else [])

    def sender_and_headers(self, message_id: str) -> tuple[str, dict[str, str]] | None:
        """The message's sender address and allowlisted headers, None when it is gone."""
        located = self._mailbox.locate(message_id, list(self._places()))
        if located is None:
            return None
        message = projection.parse(located[1].raw)
        return _sender(message), allowlisted(
            [{"name": k, "value": str(v)} for k, v in message.items()]
        )

    def seen_from(self, sender: str) -> list[bool]:
        """One entry per message from `sender` in the folders this app files to: read or not."""
        seen = []
        for folder in self._places():
            uids = self._mailbox.session.search(folder, "HEADER", "From", f'"{sender}"')
            seen.extend(
                "\\Seen" in raw.flags
                for raw in self._mailbox.session.fetch(folder, uids)
                if _sender(projection.parse(raw.raw)) == sender
            )
        return seen


def _sender(message) -> str:
    return parseaddr(str(message.get("From", "")))[1].lower()
