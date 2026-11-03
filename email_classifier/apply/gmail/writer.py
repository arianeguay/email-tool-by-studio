"""Gmail mutation primitives: labels + trash (STU-405).

This module only executes the single operation it is asked to — no
classification or decision logic. The bucket->operation mapping and the policy
gating live in the executor (next ticket), which should depend on this
interface shape so an Outlook writer can be swapped in without changing the
caller.

Trash only, never messages.delete: trash is reversible for 30 days, permanent
deletion is not — and it is outside the gmail.modify scope anyway, so the
capability does not even exist here.

No Google imports: methods drive an already-built service object (see
`fetch.gmail.client.build_service`), so tests run against a fake without the
optional `[fetch]` deps installed.
"""


class GmailWriter:
    def __init__(self, service):
        self._service = service

    def add_label(self, message_id: str, label_id: str) -> None:
        self._service.users().messages().modify(
            userId="me", id=message_id, body={"addLabelIds": [label_id]}
        ).execute()

    def remove_label(self, message_id: str, label_id: str) -> None:
        self._service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": [label_id]}
        ).execute()

    def trash(self, message_id: str) -> None:
        self._service.users().messages().trash(userId="me", id=message_id).execute()

    def untrash(self, message_id: str) -> None:
        self._service.users().messages().untrash(userId="me", id=message_id).execute()

    def label_ids(self, message_id: str) -> list[str]:
        message = (
            self._service.users()
            .messages()
            .get(userId="me", id=message_id, format="minimal")
            .execute()
        )
        return message.get("labelIds", [])

    def find_label(self, name: str) -> str | None:
        """Return the id of the label `name`, or None. Creates nothing — a dry-run
        needs to resolve labels without mutating the mailbox.

        Matching is casefolded: Gmail rejects creating a label whose name
        differs from an existing one only by case.
        """
        existing = (
            self._service.users().labels().list(userId="me").execute().get("labels", [])
        )
        for label in existing:
            if label["name"].casefold() == name.casefold():
                return label["id"]
        return None

    def ensure_label(self, name: str) -> str:
        """Return the id of the label `name`, creating it if missing. Idempotent."""
        found = self.find_label(name)
        if found is not None:
            return found
        created = (
            self._service.users().labels().create(userId="me", body={"name": name}).execute()
        )
        return created["id"]
