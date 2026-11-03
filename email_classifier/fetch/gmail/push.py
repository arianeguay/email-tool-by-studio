"""Gmail push-notification plumbing: users.watch + history.list.

Still read-only of the mailbox: watch() only registers where Gmail publishes
new-mail notifications (a Pub/Sub topic) and is permitted under the
gmail.readonly scope; history.list is a read. Neither call can mutate a message.

No Google imports: both functions only drive the service object built by
client.build_service, so tests run with a fake service and no [fetch] deps.
"""


def watch(service, topic: str) -> dict:
    """Subscribe the mailbox's INBOX changes to a Pub/Sub topic.

    Returns the raw response: {"historyId": ..., "expiration": epoch-ms}.
    Gmail expires the watch after at most 7 days — re-call from cron.
    """
    return (
        service.users()
        .watch(
            userId="me",
            body={
                "topicName": topic,
                "labelIds": ["INBOX"],
                "labelFilterBehavior": "include",
            },
        )
        .execute()
    )


def list_new_message_ids(service, start_history_id: str) -> list[str]:
    """Message ids added after start_history_id, in order, deduped.

    Raises the client's HttpError (404) when start_history_id is older than
    Gmail's history retention (~1 week); callers must rebaseline.
    """
    ids: list[str] = []
    seen: set[str] = set()
    page_token = None
    while True:
        response = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                pageToken=page_token,
            )
            .execute()
        )
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                message_id = added["message"]["id"]
                if message_id not in seen:
                    seen.add(message_id)
                    ids.append(message_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            return ids
