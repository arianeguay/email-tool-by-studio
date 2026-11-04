"""Outlook mutation primitives on Microsoft Graph (STU-847).

Mirror of `apply/gmail/writer.py`, against a vocabulary that does not line up
one-for-one (see the ontology table in `providers.py`):

* a neutral label is an Outlook **category**: many-per-message and orthogonal to
  location, which is what `email-tool/<bucket>` plus `email-tool/processed` need. Folders are
  one-per-message and could not hold both.
* `INBOX` and `TRASH` are not labels but the message's folder, so the executor's
  "remove INBOX" archive step is a move to the Archive folder, and its inverse a
  move back to the Inbox. `label_ids` reads the folder back on the same axis —
  and answers `ELSEWHERE` for a folder this app never files to, which is how a
  message the mailbox no longer holds as live mail is told from an archived one
  (STU-1075).

Category handles are the display names themselves: `message.categories` stores
names, so a name already is the opaque handle the `Writer` protocol hands back
to `add_label` / `remove_label`, and nothing has to be created before a message
can be stamped with one.

Trash is a move to Deleted Items, never `DELETE /me/messages/{id}`: a move is
reversible, permanent deletion is not.

Every mutation here is a folder move or a category rewrite, and undo replays the
journal by message id — which only holds because the Graph session asks for
immutable ids (`fetch/outlook/client.py`).

No msal/requests imports: the session is injected, so tests run against a fake
without the optional `[fetch-outlook]` deps installed.
"""

from email_classifier.providers import (
    ELSEWHERE_LABEL as _ELSEWHERE,
    INBOX_LABEL as _INBOX,
    TRASH_LABEL as _TRASH,
)

_INBOX_FOLDER = "inbox"
_ARCHIVE_FOLDER = "archive"
_DELETED_FOLDER = "deleteditems"

# Every folder this app ever puts a message in, and the label the executor reads it
# back as. Two are locations the executor names Gmail-style; the Archive maps to no
# label because "archived" is the absence of both on Gmail too — but it is in the
# table, because being a place we file to is the very thing that distinguishes it
# from a folder that is not (STU-1075).
_FOLDER_LABEL = {_INBOX_FOLDER: _INBOX, _DELETED_FOLDER: _TRASH, _ARCHIVE_FOLDER: None}


class OutlookWriter:
    def __init__(self, session):
        self._session = session
        self._folder_ids: dict[str, str] = {}

    def find_label(self, name: str) -> str | None:
        """Return the category named `name` — a category needs no registration.

        Stamping a message with a name absent from the mailbox's master category
        list works and shows up in Outlook clients; registering it there would
        only add a color, and costs a `MailboxSettings` scope this app does not
        hold (`/me/outlook/masterCategories` answers 403 under `Mail.ReadWrite`).
        """
        return name

    def ensure_label(self, name: str) -> str:
        return name

    def add_label(self, message_id: str, label_id: str) -> None:
        if label_id == _INBOX:
            self._move(message_id, _INBOX_FOLDER)
            return
        current = self._categories(message_id)
        if label_id not in current:
            self._set_categories(message_id, [*current, label_id])

    def remove_label(self, message_id: str, label_id: str) -> None:
        if label_id == _INBOX:
            self._move(message_id, _ARCHIVE_FOLDER)
            return
        current = self._categories(message_id)
        if label_id in current:
            self._set_categories(message_id, [c for c in current if c != label_id])

    def trash(self, message_id: str) -> None:
        self._move(message_id, _DELETED_FOLDER)

    def untrash(self, message_id: str) -> None:
        """Move the message back to the Inbox.

        Graph's move does not record where the message came from, so unlike
        Gmail's untrash this cannot restore the original folder. The Inbox is the
        right target anyway: the executor only ever trashes messages it fetched
        from there.
        """
        self._move(message_id, _INBOX_FOLDER)

    def label_ids(self, message_id: str) -> list[str]:
        """The message's categories, plus `INBOX` or `TRASH` for where it sits.

        Reporting the folder as a label is the same fiction `add_label` and
        `remove_label` already tell, and the executor needs the two on one axis
        (STU-963): it converges a thread by diffing what a message carries against
        what the verdict asks for, and where the message sits is half of that.
        Gmail's `label_ids` has always answered both the same way.

        Deleted Items is on that axis for the same reason (STU-983). Without it
        the trash is the one location with no state to diff, so a reply reopening
        a thread an earlier run threw out left the trashed half behind.

        And a folder outside the table answers `ELSEWHERE` (STU-1075). Returning
        the categories alone said "not in the Inbox", which the executor reads as
        an archived message it may still act on — so a report outliving its mail
        planned an archive with no move and stamped `email-tool/<bucket>` onto messages in
        the Deletions dumpster, every operation journalled `ok`. The folder axis
        has three answers, not two, and this is where it is read.
        """
        message = self._session.get(
            f"/me/messages/{message_id}", {"$select": "categories,parentFolderId"}
        )
        categories = message.get("categories") or []
        for folder, label in _FOLDER_LABEL.items():
            if message.get("parentFolderId") == self._folder_id(folder):
                return categories if label is None else [*categories, label]
        return [*categories, _ELSEWHERE]

    def _categories(self, message_id: str) -> list[str]:
        """Categories alone — what a category rewrite must not fold the folder into."""
        message = self._session.get(
            f"/me/messages/{message_id}", {"$select": "categories"}
        )
        return message.get("categories") or []

    def _folder_id(self, folder: str) -> str:
        """A well-known folder's real id, resolved once — a message reports the id,
        never the name."""
        if folder not in self._folder_ids:
            self._folder_ids[folder] = self._session.get(
                f"/me/mailFolders('{folder}')", {"$select": "id"}
            )["id"]
        return self._folder_ids[folder]

    def _move(self, message_id: str, folder: str) -> None:
        self._session.request(
            "POST", f"/me/messages/{message_id}/move", payload={"destinationId": folder}
        )

    def _set_categories(self, message_id: str, categories: list[str]) -> None:
        self._session.request(
            "PATCH", f"/me/messages/{message_id}", payload={"categories": categories}
        )
