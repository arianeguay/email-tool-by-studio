"""One-click unsubscribe (STU-2071). The POST is injected: no test opens a socket."""

import json
import urllib.request

import pytest

from email_classifier.apply import unsubscribe
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.stage1.policy import Policy

ONE_CLICK = {
    "List-Unsubscribe": "<mailto:u@shop.example>, <https://shop.example/u?id=1>",
    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
}


def _policy(on=True):
    return Policy(deterministic_can_finalize=True, destructive_actions_allowed=[],
                  frontier_log_file=False, frontier_log_stdout=False,
                  email_provider="imap", auto_unsubscribe=on)


def _verdict(mid, bucket="C", status="ok"):
    return EmailVerdict(message_id=mid, thread_id=mid, path="deterministic", status=status,
                        bucket=bucket, suggested_action="label_archive", confidence="high",
                        rule_id="r", subtype=None, reasoning=None, error=None)


class FakeWriter:
    def __init__(self, messages, seen):
        self.messages = messages  # message id -> (sender, headers)
        self.seen = seen          # sender -> [read?] per message in the mailbox

    def sender_and_headers(self, message_id):
        return self.messages.get(message_id)

    def seen_from(self, sender):
        return self.seen.get(sender, [])


class Post:
    def __init__(self, status=200):
        self.status, self.urls = status, []

    def __call__(self, url):
        self.urls.append(url)
        return self.status


def _run(writer, tmp_path, post, verdicts=None, policy=None, dry_run=False):
    return unsubscribe.unsubscribe(
        verdicts or [_verdict("m1")], writer, policy or _policy(), "run-1",
        dry_run=dry_run, journal=tmp_path / "logs" / "unsubscribed.jsonl", post=post,
    )


def _journal(tmp_path):
    path = tmp_path / "logs" / "unsubscribed.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_an_unread_bulk_sender_is_posted_once_and_journaled(tmp_path):
    writer = FakeWriter({"m1": ("news@shop.example", ONE_CLICK)},
                        {"news@shop.example": [False, False]})
    post = Post(status=202)

    senders = _run(writer, tmp_path, post)

    assert senders == ["news@shop.example"]
    assert post.urls == ["https://shop.example/u?id=1"]
    assert _journal(tmp_path) == [{"sender": "news@shop.example", "host": "shop.example",
                                   "status": 202, "run_id": "run-1"}]


@pytest.mark.parametrize("messages, seen, verdict", [
    ({"m1": ("news@shop.example", ONE_CLICK)}, [False, True], _verdict("m1")),
    ({"m1": ("news@shop.example", ONE_CLICK)}, [False], _verdict("m1")),
    ({"m1": ("news@shop.example", {"List-Unsubscribe": "<https://shop.example/u>"})},
     [False, False], _verdict("m1")),
    ({"m1": ("news@shop.example", {**ONE_CLICK, "List-Unsubscribe": "<mailto:u@shop.example>"})},
     [False, False], _verdict("m1")),
    ({"m1": ("news@shop.example", {**ONE_CLICK, "List-Unsubscribe": "<http://shop.example/u>"})},
     [False, False], _verdict("m1")),
    ({"m1": ("news@shop.example", ONE_CLICK)}, [False, False], _verdict("m1", bucket="B")),
    ({"m1": ("news@shop.example", ONE_CLICK)}, [False, False], _verdict("m1", status="error")),
    ({}, [False, False], _verdict("m1")),
], ids=["one-read", "single-message", "no-one-click", "mailto-only", "plain-http",
        "not-bucket-c", "errored-verdict", "message-gone"])
def test_a_sender_failing_any_condition_is_left_alone(tmp_path, messages, seen, verdict):
    writer = FakeWriter(messages, {"news@shop.example": seen})
    post = Post()

    assert _run(writer, tmp_path, post, verdicts=[verdict]) == []
    assert post.urls == []
    assert _journal(tmp_path) == []


def test_a_journaled_sender_is_never_posted_twice(tmp_path):
    writer = FakeWriter({"m1": ("news@shop.example", ONE_CLICK),
                         "m2": ("news@shop.example", ONE_CLICK)},
                        {"news@shop.example": [False, False]})
    post = Post(status=500)

    _run(writer, tmp_path, post, verdicts=[_verdict("m1"), _verdict("m2")])
    _run(writer, tmp_path, post)

    assert len(post.urls) == 1
    assert len(_journal(tmp_path)) == 1


def test_the_switch_off_sends_nothing_and_reads_nothing(tmp_path):
    post = Post()

    assert _run(None, tmp_path, post, policy=_policy(on=False)) == []
    assert post.urls == []


def test_a_dry_run_names_the_sender_and_posts_nothing(tmp_path):
    writer = FakeWriter({"m1": ("news@shop.example", ONE_CLICK)},
                        {"news@shop.example": [False, False]})
    post = Post()

    assert _run(writer, tmp_path, post, dry_run=True) == ["news@shop.example"]
    assert post.urls == []
    assert _journal(tmp_path) == []


class _Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        raise OSError("unreachable")


def test_the_post_is_rfc_8058_and_refuses_redirects(monkeypatch):
    opener = _Opener()
    handlers = []
    monkeypatch.setattr(urllib.request, "build_opener",
                        lambda *h: handlers.extend(h) or opener)

    assert unsubscribe.post_one_click("https://shop.example/u") is None

    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.data == b"List-Unsubscribe=One-Click"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert handlers == [unsubscribe._NoRedirect]
    assert unsubscribe._NoRedirect().redirect_request(request, None, 302, "Found", {},
                                                      "https://shop.example/page") is None
