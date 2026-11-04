"""Gmail push plumbing (watch + history.list) against a fake service."""

from email_classifier.fetch.gmail import push


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self, **_kwargs):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeHistory:
    """Serves history.list pages in order and records the calls."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return _Call(self.pages.pop(0))


class FakeUsers:
    def __init__(self, history_pages=(), watch_response=None):
        self._history = FakeHistory(history_pages)
        self._watch_response = watch_response
        self.watch_calls = []

    def history(self):
        return self._history

    def watch(self, **kwargs):
        self.watch_calls.append(kwargs)
        return _Call(self._watch_response)


class FakeService:
    def __init__(self, **kwargs):
        self._users = FakeUsers(**kwargs)

    def users(self):
        return self._users


def test_watch_subscribes_inbox_to_topic():
    service = FakeService(watch_response={"historyId": "42", "expiration": "1750000000000"})
    response = push.watch(service, "projects/p/topics/gmail-inbox")

    assert response["historyId"] == "42"
    call = service.users().watch_calls[0]
    assert call["userId"] == "me"
    assert call["body"]["topicName"] == "projects/p/topics/gmail-inbox"
    assert call["body"]["labelIds"] == ["INBOX"]


def test_list_new_message_ids_paginates_and_dedupes():
    service = FakeService(history_pages=[
        {
            "history": [
                {"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m2"}}]},
                {"messagesAdded": [{"message": {"id": "m1"}}]},  # duplicate across records
            ],
            "nextPageToken": "page2",
        },
        {
            "history": [{"messagesAdded": [{"message": {"id": "m3"}}]}],
        },
    ])

    assert push.list_new_message_ids(service, "100") == ["m1", "m2", "m3"]

    calls = service.users().history().calls
    assert [c.get("pageToken") for c in calls] == [None, "page2"]
    assert all(c["startHistoryId"] == "100" for c in calls)
    assert all(c["historyTypes"] == ["messageAdded"] for c in calls)


def test_list_new_message_ids_handles_empty_history():
    service = FakeService(history_pages=[{"historyId": "101"}])
    assert push.list_new_message_ids(service, "100") == []


def test_list_ignores_records_without_messages_added():
    service = FakeService(history_pages=[
        {"history": [{"labelsRemoved": [{"message": {"id": "m9"}}]}]},
    ])
    assert push.list_new_message_ids(service, "100") == []
