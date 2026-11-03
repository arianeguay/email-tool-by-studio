"""Outlook reply drafts on Microsoft Graph (STU-850).

`createReply` then a body PATCH. There is no call to `/me/messages/{id}/send`
anywhere in this module, and `Mail.ReadWrite` cannot send even if one appeared:
a draft waits for a human, always.

Upsert rather than create: the draft/QA group re-runs `generate-draft` on a
rejection, and a second `createReply` on the same message would leave the
rejected draft sitting next to its replacement. Keying on the conversation makes
a retry rewrite the same draft.

`delete_draft` is one of the two permanent deletions in the repo (STU-854), the
Gmail drafter carrying the other. It is safe where message deletion is not: the
object it destroys was created by the run and did not exist before it. Whether a
given draft still qualifies is decided by the caller, from `get_draft`.

No msal/requests imports: the session is injected, so tests run against a fake
without the optional `[fetch-outlook]` deps installed.
"""

from email_classifier.placeholder import PLACEHOLDER_CATEGORY, holds_placeholder

_DRAFTS_FOLDER = "/me/mailFolders/drafts/messages"
_DRAFT_FIELDS = "id,isDraft,lastModifiedDateTime,sentDateTime,categories"


class OutlookDrafter:
    def __init__(self, session):
        self._session = session

    def upsert_reply_draft(
        self,
        message_id: str,
        thread_id: str,
        body: str,
        reply_to: str | None = None,
        reply_name: str | None = None,
    ) -> dict:
        """`reply_to` overrides the recipient `createReply` inferred (STU-888).

        Still a `createReply`, not a bare create: it is what puts the draft in the
        conversation and carries `In-Reply-To` across. Only the address is wrong
        for a contact-form notification, and the same PATCH that writes the body
        replaces it — so a draft on a robot's envelope reaches the parent without
        leaving the thread it answers.
        """
        existing = self.find_draft(thread_id)
        draft_id = existing or self._create_reply(message_id)
        # The whole category set, not an added one: only an untouched draft of ours is
        # rewritten, so the marker is the only category it can carry, and a rewrite
        # without a placeholder must clear it (STU-2049).
        payload: dict = {
            "body": {"contentType": "Text", "content": body},
            "categories": [PLACEHOLDER_CATEGORY] if holds_placeholder(body) else [],
        }
        if reply_to:
            recipient: dict = {"address": reply_to}
            if reply_name:
                recipient["name"] = reply_name
            payload["toRecipients"] = [{"emailAddress": recipient}]
        self._session.request("PATCH", f"/me/messages/{draft_id}", payload=payload)
        return {"draft_id": draft_id, "updated": existing is not None}

    def get_draft(self, draft_id: str) -> dict | None:
        """The draft as the mailbox holds it now, or None when it is no longer there.

        A 404 is the normal answer for a draft a human deleted, so it is a result
        here rather than an error. Read off the exception's response instead of
        importing `requests`, which this module deliberately does not depend on.
        """
        try:
            return self._session.get(f"/me/messages/{draft_id}", {"$select": _DRAFT_FIELDS})
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                return None
            raise

    def delete_draft(self, draft_id: str) -> None:
        self._session.request("DELETE", f"/me/messages/{draft_id}")

    def find_draft(self, thread_id: str) -> str | None:
        """Id of the run's draft on that conversation, or None when there is none."""
        page = self._session.get(
            _DRAFTS_FOLDER,
            {"$filter": f"conversationId eq '{thread_id}'", "$select": "id", "$top": 1},
        )
        value = page.get("value") or []
        return value[0]["id"] if value else None

    def _create_reply(self, message_id: str) -> str:
        return self._session.request("POST", f"/me/messages/{message_id}/createReply")["id"]
