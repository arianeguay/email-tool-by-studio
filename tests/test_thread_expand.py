"""Widening a fetch from messages to conversations (STU-963).

The date floor (STU-1123) rides on the same call and is widened out of the way here;
what it does to the expansion is `tests/test_fetch_window.py`.
"""

from datetime import date

from email_classifier.fetch.threads import expand_to_conversations

BEFORE_ANY_FIXTURE = date(2000, 1, 1)


class _Fetcher:
    """The two Fetcher calls the expansion makes, over a canned mailbox."""

    def __init__(self, mailbox):
        self.mailbox = mailbox  # message_id -> thread_id
        self.fetched = []
        self.listed = []

    def conversation_message_ids(self, thread_id):
        self.listed.append(thread_id)
        return [mid for mid, tid in self.mailbox.items() if tid == thread_id]

    def fetch_inputs_by_ids(self, message_ids):
        self.fetched.append(list(message_ids))
        return [_input(mid, self.mailbox[mid]) for mid in message_ids]


def _input(message_id, thread_id):
    return {"message_id": message_id, "thread_id": thread_id,
            "date": "2026-06-12T08:15:00+00:00"}


def test_a_notified_message_arrives_with_the_rest_of_its_thread():
    fetcher = _Fetcher({"opener": "t1", "reply": "t1"})

    result = expand_to_conversations(
        fetcher, [_input("reply", "t1")], start_date=BEFORE_ANY_FIXTURE)

    assert [i["message_id"] for i in result] == ["reply", "opener"]


def test_a_message_already_in_hand_is_not_fetched_twice():
    fetcher = _Fetcher({"opener": "t1", "reply": "t1"})

    expand_to_conversations(
        fetcher, [_input("opener", "t1"), _input("reply", "t1")],
        start_date=BEFORE_ANY_FIXTURE,
    )

    assert fetcher.fetched == []


def test_one_conversation_is_listed_once_however_many_of_its_messages_came_in():
    fetcher = _Fetcher({"a": "t1", "b": "t1", "c": "t1"})

    expand_to_conversations(
        fetcher, [_input("a", "t1"), _input("b", "t1")], start_date=BEFORE_ANY_FIXTURE)

    assert fetcher.listed == ["t1"]
    assert fetcher.fetched == [["c"]]


def test_several_threads_are_fetched_in_one_call():
    fetcher = _Fetcher({"a": "t1", "b": "t1", "c": "t2", "d": "t2"})

    result = expand_to_conversations(
        fetcher, [_input("a", "t1"), _input("c", "t2")], start_date=BEFORE_ANY_FIXTURE)

    assert fetcher.fetched == [["b", "d"]]
    assert [i["message_id"] for i in result] == ["a", "c", "b", "d"]


def test_an_empty_fetch_asks_the_mailbox_nothing():
    fetcher = _Fetcher({"a": "t1"})

    assert expand_to_conversations(fetcher, [], start_date=BEFORE_ANY_FIXTURE) == []
    assert fetcher.listed == []
    assert fetcher.fetched == []
