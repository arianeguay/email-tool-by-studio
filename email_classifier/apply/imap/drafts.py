"""IMAP reply drafts (STU-2054), mirroring `apply/gmail/drafts.py`.

The reply is assembled here, as on Gmail, and `APPEND`ed to the `\\Drafts`
folder flagged `\\Draft`. IMAP cannot rewrite a stored message, so an upsert
deletes the conversation's draft and appends the new one; the draft id is the
new draft's `Message-ID`. Nothing in this module can send: IMAP has no send.
"""

import os
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from email_classifier.apply.gmail.drafts import _reply_subject
from email_classifier.fetch.imap import projection
from email_classifier.fetch.imap.client import ImapFetcher, Mailbox


class ImapDrafter:
    def __init__(self, session):
        self._mailbox = Mailbox(session)
        self._fetcher = ImapFetcher(session)

    def _drafts(self) -> str:
        return self._mailbox.folder("Drafts")

    def upsert_reply_draft(
        self,
        message_id: str,
        thread_id: str,
        body: str,
        reply_to: str | None = None,
        reply_name: str | None = None,
    ) -> dict:
        """`reply_to` overrides the `To` the parent message implies (STU-888)."""
        existing = self.find_draft(thread_id)
        draft = self._reply(self._fetcher.reply_source(message_id), body, reply_to, reply_name)
        if existing is not None:
            self.delete_draft(existing)
        self._mailbox.session.append(self._drafts(), "(\\Draft \\Seen)", draft.as_bytes())
        return {"draft_id": projection.message_id(draft), "updated": existing is not None}

    def get_draft(self, draft_id: str) -> dict | None:
        """INTERNALDATE is the draft's last save: a client saving an edit stores it anew."""
        located = self._mailbox.locate(draft_id, [self._drafts()])
        if located is None:
            return None
        return {"id": draft_id, "isDraft": True,
                "lastModifiedDateTime": projection.iso_date(located[1].received)}

    def delete_draft(self, draft_id: str) -> None:
        located = self._mailbox.locate(draft_id, [self._drafts()])
        if located is not None:
            self._mailbox.session.delete(self._drafts(), located[1].uid)

    def find_draft(self, thread_id: str) -> str | None:
        for _, raw in self._mailbox.conversation(thread_id, [self._drafts()]):
            return projection.message_id(projection.parse(raw.raw))
        return None

    def _reply(self, source: EmailMessage, body: str,
               reply_to: str | None, reply_name: str | None) -> EmailMessage:
        sender = os.environ.get("IMAP_FROM") or os.environ["IMAP_USER"]
        message = EmailMessage()
        message["From"] = sender
        message["To"] = (
            formataddr((reply_name or "", reply_to))
            if reply_to
            else str(source.get("Reply-To") or source.get("From", ""))
        )
        message["Subject"] = _reply_subject(str(source.get("Subject", "")))
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
        parent = str(source.get("Message-ID", "")).strip()
        if parent:
            message["In-Reply-To"] = parent
            message["References"] = " ".join(
                part for part in (str(source.get("References", "")).strip(), parent) if part
            )
        message.set_content(body)
        return message
