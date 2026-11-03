"""Inbox mail that no report holds a verdict for (STU-1502)."""

import json
from datetime import datetime, timezone

import pytest

from email_classifier.fetch.outlook.client import OutlookFetcher
from scripts import check_unverdicted

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
SINCE = "2026-08-30T02:24:00Z"


class _Session:
    """Two Graph pages of the Inbox listing, joined by a nextLink."""

    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def get(self, url, params=None, prefer=None):
        self.calls.append((url, params))
        if params is not None:
            return {"value": self.messages[:2], "@odata.nextLink": "https://next"}
        return {"value": self.messages[2:]}


def _message(message_id, received, draft=False):
    return {"id": message_id, "receivedDateTime": received, "isDraft": draft}


INBOX = [
    _message("verdicted", "2026-09-03T10:00:00Z"),
    _message("missed-a", "2026-09-03T17:55:00Z"),
    _message("missed-b", "2026-09-03T14:17:00Z"),
    _message("a-draft", "2026-09-04T09:00:00Z", draft=True),
    _message("beyond-shelf", "2026-08-27T09:00:00Z"),
    _message("in-flight", "2026-09-27T11:30:00Z"),
    _message("errored", "2026-09-05T08:00:00Z"),
]


@pytest.fixture
def reports(tmp_path):
    directory = tmp_path / "reports"
    directory.mkdir()
    (directory / "one.json").write_text(json.dumps({"verdicts": [
        {"message_id": "verdicted", "status": "ok"},
        {"message_id": "errored", "status": "error"},
    ]}))
    return directory


@pytest.fixture
def alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(check_unverdicted.alerts, "notify", sent.append)
    return sent


def _serve(monkeypatch, messages):
    session = _Session(messages)
    monkeypatch.setattr(
        check_unverdicted, "get_fetcher", lambda provider: OutlookFetcher(session)
    )
    return session


def _run(tmp_path, reports, *extra, since=SINCE):
    argv = ["--reports", str(reports), "--journal", str(tmp_path / "journal.jsonl")]
    if since:
        argv += ["--since", since]
    return check_unverdicted.main([*argv, *extra], now=NOW)


def test_the_listing_pages_through_the_inbox_and_drops_drafts():
    session = _Session(INBOX)
    listed = OutlookFetcher(session).inbox_received_since("2026-08-30T02:24:00Z")
    assert [message_id for message_id, _ in listed] == [
        "verdicted", "missed-a", "missed-b", "beyond-shelf", "in-flight", "errored",
    ]
    url, params = session.calls[0]
    assert url == "/me/mailFolders('inbox')/messages"
    assert params["$select"] == "id,receivedDateTime,isDraft"
    assert params["$filter"] == "receivedDateTime ge 2026-08-30T02:24:00Z"
    assert session.calls[1] == ("https://next", None)


def test_unmatched_ids_are_named_oldest_first(tmp_path, reports, alerts, monkeypatch, capsys):
    _serve(monkeypatch, INBOX)
    assert _run(tmp_path, reports) == check_unverdicted.UNVERDICTED
    out, err = capsys.readouterr()
    assert out.index("missed-b") < out.index("missed-a")
    assert out.index("missed-a") < out.index("errored")
    for judged_elsewhere in ("verdicted", "beyond-shelf", "in-flight"):
        assert judged_elsewhere not in out + err
    assert "3 of 4 Inbox message(s)" in err
    assert "oldest 2026-09-03T14:17:00Z" in err
    assert "1 older than the 30-day report shelf, 1 from the last 1h" in err
    assert len(alerts) == 1 and "missed-a" not in alerts[0]


def test_an_errored_verdict_alone_is_unverdicted(tmp_path, reports, alerts, monkeypatch, capsys):
    _serve(monkeypatch, [_message("errored", "2026-09-05T08:00:00Z")])
    assert _run(tmp_path, reports) == check_unverdicted.UNVERDICTED
    assert "errored" in capsys.readouterr().out
    assert len(alerts) == 1


def test_a_later_ok_verdict_clears_an_errored_one(tmp_path, reports, alerts, monkeypatch):
    (reports / "two.json").write_text(json.dumps({"verdicts": [
        {"message_id": "errored", "status": "ok"},
    ]}))
    _serve(monkeypatch, [_message("errored", "2026-09-05T08:00:00Z")])
    assert _run(tmp_path, reports) == 0
    assert alerts == []


def test_an_unreadable_mailbox_is_unmeasured_not_clean(tmp_path, reports, alerts, monkeypatch):
    def refuse(provider):
        raise RuntimeError("grant lost")

    monkeypatch.setattr(check_unverdicted, "get_fetcher", refuse)
    assert _run(tmp_path, reports) == check_unverdicted.UNREADABLE
    assert "could not be listed" in alerts[0]


def test_no_report_on_disk_is_unmeasured(tmp_path, alerts, monkeypatch):
    _serve(monkeypatch, INBOX)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run(tmp_path, empty) == check_unverdicted.NO_REPORTS


def test_no_instant_is_refused(tmp_path, reports, alerts, monkeypatch):
    monkeypatch.delenv(check_unverdicted.SINCE_ENV, raising=False)
    _serve(monkeypatch, INBOX)
    assert _run(tmp_path, reports, since=None) == check_unverdicted.NOT_CONFIGURED


def test_all_verdicted_exits_zero(tmp_path, reports, alerts, monkeypatch):
    _serve(monkeypatch, INBOX[:1])
    assert _run(tmp_path, reports) == 0
    assert alerts == []


def test_an_unchanged_miss_alerts_once(tmp_path, reports, alerts, monkeypatch):
    _serve(monkeypatch, INBOX)
    _run(tmp_path, reports)
    _run(tmp_path, reports)
    assert len(alerts) == 1


def test_a_new_miss_alerts_again_and_a_shrinking_one_does_not(
    tmp_path, reports, alerts, monkeypatch
):
    _serve(monkeypatch, INBOX[:2])
    _run(tmp_path, reports)
    _serve(monkeypatch, INBOX[:3])
    _run(tmp_path, reports)
    assert len(alerts) == 2
    _serve(monkeypatch, INBOX[:1] + INBOX[2:3])
    _run(tmp_path, reports)
    assert len(alerts) == 2


def test_a_miss_that_clears_then_returns_alerts_again(tmp_path, reports, alerts, monkeypatch):
    _serve(monkeypatch, INBOX[:2])
    _run(tmp_path, reports)
    _serve(monkeypatch, INBOX[:1])
    _run(tmp_path, reports)
    _serve(monkeypatch, INBOX[:2])
    _run(tmp_path, reports)
    assert len(alerts) == 2
