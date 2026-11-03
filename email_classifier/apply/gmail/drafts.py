"""Gmail reply drafts (STU-853), mirroring `apply/outlook/drafts.py`.

Gmail has no server-side `createReply`, so the reply is assembled here: the
draft carries the thread's `threadId` plus `In-Reply-To`/`References` and a
`Re:` subject, which is what makes Gmail file it inside the thread instead of
as a standalone message.

Upsert rather than create, for the same reason as the Outlook path: the
draft/QA group re-runs `generate-draft` on a rejection, and a second create
would leave the rejected draft sitting next to its replacement. `drafts.list`
is the only route from a thread to a draft id — a draft's message id is not
one — so the thread match happens client-side.

`drafts.send` appears nowhere in this module, and `gmail.modify` (already
granted for apply, STU-405) covers drafts — deletion included — without widening
the grant. `delete_draft` is what `undo` uses to take a run's drafts back out
(STU-854); it destroys only an object the run itself created.

No Google imports: the service object is injected, so tests run against a fake
without the optional `[fetch]` deps installed.
"""

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr

# Threading inputs only. These headers go back to Gmail and never reach the
# corpus, so `fetch.headers`' privacy allowlist does not apply here.
_REPLY_HEADERS = ["From", "Reply-To", "Subject", "Message-ID", "References"]

_PAGE_SIZE = 500


def _reply_subject(subject: str) -> str:
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


class GmailDrafter:
    def __init__(self, service):
        self._service = service

    def upsert_reply_draft(
        self,
        message_id: str,
        thread_id: str,
        body: str,
        reply_to: str | None = None,
        reply_name: str | None = None,
    ) -> dict:
        """`reply_to` overrides the `To` the parent message implies (STU-888)."""
        payload = {
            "message": {
                "threadId": thread_id,
                "raw": self._reply_mime(message_id, body, reply_to, reply_name),
            }
        }
        existing = self.find_draft(thread_id)
        if existing is not None:
            self._service.users().drafts().update(
                userId="me", id=existing, body=payload
            ).execute()
            return {"draft_id": existing, "updated": True}
        created = (
            self._service.users().drafts().create(userId="me", body=payload).execute()
        )
        return {"draft_id": created["id"], "updated": False}

    def get_draft(self, draft_id: str) -> dict | None:
        """The draft in the neutral shape `undo` reads, or None when it is gone.

        A draft the human sent is no longer a draft *and* no longer exists — Gmail
        moves it to the thread under a new id — so `isDraft` is only ever True
        here; the sent case arrives as the 404. `internalDate` is the draft's last
        save, which is what a human edit moves.
        """
        try:
            draft = (
                self._service.users()
                .drafts()
                .get(userId="me", id=draft_id, format="minimal")
                .execute()
            )
        except Exception as exc:
            # Read the status off the exception rather than importing googleapiclient,
            # which this module deliberately does not depend on.
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise
        saved_at = datetime.fromtimestamp(
            int(draft["message"]["internalDate"]) / 1000, timezone.utc
        )
        return {"id": draft_id, "isDraft": True,
                "lastModifiedDateTime": saved_at.isoformat()}

    def delete_draft(self, draft_id: str) -> None:
        self._service.users().drafts().delete(userId="me", id=draft_id).execute()

    def find_draft(self, thread_id: str) -> str | None:
        """Id of a draft already sitting on that thread, or None when there is none."""
        page_token = None
        while True:
            page = (
                self._service.users()
                .drafts()
                .list(userId="me", maxResults=_PAGE_SIZE, pageToken=page_token)
                .execute()
            )
            for draft in page.get("drafts", []):
                if draft.get("message", {}).get("threadId") == thread_id:
                    return draft["id"]
            page_token = page.get("nextPageToken")
            if not page_token:
                return None

    def _reply_mime(
        self,
        message_id: str,
        body: str,
        reply_to: str | None = None,
        reply_name: str | None = None,
    ) -> str:
        source = (
            self._service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=_REPLY_HEADERS,
            )
            .execute()
        )
        by_name = {
            h["name"].lower(): h["value"]
            for h in source.get("payload", {}).get("headers", [])
        }

        message = EmailMessage()
        message["To"] = (
            formataddr((reply_name or "", reply_to))
            if reply_to
            else by_name.get("reply-to") or by_name.get("from", "")
        )
        message["Subject"] = _reply_subject(by_name.get("subject", ""))
        parent = by_name.get("message-id")
        if parent:
            message["In-Reply-To"] = parent
            message["References"] = " ".join(
                part for part in (by_name.get("references"), parent) if part
            )
        message.set_content(body)
        return base64.urlsafe_b64encode(message.as_bytes()).decode()
