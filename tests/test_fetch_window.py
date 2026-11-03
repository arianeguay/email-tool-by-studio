"""The run's date floor (STU-1123): what a fetch is allowed to reach for."""

from datetime import date, datetime, timedelta, timezone

import pytest
import yaml

from email_classifier.fetch import window
from email_classifier.fetch.threads import expand_to_conversations
from email_classifier.stage1.policy import load_policy


def _input(message_id: str, thread_id: str, when: str) -> dict:
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "from_address": "parent@example.com",
        "subject": "s",
        "headers": {},
        "body_snippet": "b",
        "date": when,
    }


def _policy(tmp_path, **extra):
    body = {
        "deterministic_can_finalize": True,
        "frontier_log": {"file": True},
        "email": {"provider": "outlook"},
        **extra,
    }
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return load_policy(path)


# --- the configured floor ---------------------------------------------------

def test_absent_start_date_means_today(tmp_path):
    assert _policy(tmp_path).start_date is None
    assert window.resolve(None) == datetime.now(window.CLUB_TZ).date()


def test_the_literal_today_reads_the_same_as_an_absent_key(tmp_path):
    assert _policy(tmp_path, start_date="today").start_date is None


def test_a_bare_iso_date_is_the_floor(tmp_path):
    assert _policy(tmp_path, start_date=date(2026, 8, 19)).start_date == date(2026, 8, 19)


def test_a_quoted_iso_date_is_the_same_floor(tmp_path):
    assert _policy(tmp_path, start_date="2026-08-19").start_date == date(2026, 8, 19)


def test_a_time_of_day_is_kept_as_an_instant(tmp_path):
    """STU-1897: an outage sweep starts when the outage did, not at the midnight before."""
    floor = _policy(tmp_path, start_date="2026-09-29T17:40").start_date
    assert floor == datetime(2026, 9, 29, 17, 40)
    assert window.since_iso(floor) == "2026-09-29T21:40:00+00:00"
    corpus = [
        _input("morning", "t1", "2026-09-29T09:00:00-04:00"),
        _input("outage", "t2", "2026-09-29T17:42:00-04:00"),
    ]
    assert [m["message_id"] for m in window.in_scope(corpus, floor)] == ["outage"]


def test_an_instant_with_its_own_offset_keeps_it(tmp_path):
    floor = _policy(tmp_path, start_date="2026-09-29T21:40:00+00:00").start_date
    assert window.since_iso(floor) == "2026-09-29T21:40:00+00:00"


def test_an_unreadable_start_date_refuses(tmp_path):
    """A floor nobody can read would default to today and silently drop everything."""
    with pytest.raises(ValueError, match="start_date must be an ISO date"):
        _policy(tmp_path, start_date="last tuesday")


def test_a_configured_floor_survives_the_day_turning_over():
    assert window.resolve(date(2026, 8, 19)) == date(2026, 8, 19)


def test_the_floor_is_the_start_of_that_day_in_the_mailboxs_own_timezone():
    """UTC midnight is 20:00 the previous evening in Montreal through the summer."""
    assert window.since_iso(date(2026, 8, 19)) == "2026-08-19T04:00:00+00:00"


def test_an_evening_run_still_reaches_the_whole_local_day():
    """Resolved in UTC, "today" turns over at 20:00 local: an evening run would floor
    itself at 20:00 tonight and drop the local working day, every evening."""
    evening = datetime(2026, 8, 31, 21, 0, tzinfo=window.CLUB_TZ)
    assert evening.astimezone(timezone.utc).date() == date(2026, 9, 1)

    floor = evening.astimezone(window.CLUB_TZ).date()
    this_morning = [_input("m", "t", "2026-08-31T10:00:00-04:00")]
    assert window.in_scope(this_morning, floor) == this_morning


# --- the exact boundary -----------------------------------------------------

def test_messages_before_the_floor_are_dropped():
    corpus = [
        _input("old", "t1", "2026-08-18T23:59:59-04:00"),
        _input("new", "t2", "2026-08-19T00:00:00-04:00"),
    ]
    assert [m["message_id"] for m in window.in_scope(corpus, date(2026, 8, 19))] == ["new"]


def test_the_floor_is_inclusive_of_its_own_first_local_moment():
    corpus = [_input("edge", "t1", "2026-08-19T00:00:00-04:00")]
    assert window.in_scope(corpus, date(2026, 8, 19)) == corpus


def test_a_date_in_another_offset_is_compared_as_an_instant():
    """02:00Z on the 19th is 22:00 on the 18th in Montreal — out; string order says in."""
    assert window.in_scope(
        [_input("m", "t1", "2026-08-19T02:00:00+00:00")], date(2026, 8, 19)
    ) == []


def test_two_floors_over_one_corpus_differ_by_exactly_the_older_messages():
    """The issue's hermetic verification, on a corpus no mailbox produced."""
    corpus = [
        _input("m1", "t1", "2026-08-10T09:00:00-04:00"),
        _input("m2", "t2", "2026-08-19T09:00:00-04:00"),
        _input("m3", "t3", "2026-08-30T09:00:00-04:00"),
    ]
    wide = window.in_scope(corpus, date(2026, 8, 1))
    narrow = window.in_scope(corpus, date(2026, 8, 19))
    assert wide == corpus
    assert [m["message_id"] for m in narrow] == ["m2", "m3"]
    dropped = [m for m in wide if m not in narrow]
    assert all(m["date"] < "2026-08-19" for m in dropped)


def test_an_unparseable_date_raises_rather_than_guessing_a_side():
    with pytest.raises(ValueError):
        window.in_scope([_input("m", "t", "sometime last week")], date(2026, 8, 19))


# --- floor first, expansion second ------------------------------------------

class _Fetcher:
    def __init__(self, threads: dict[str, list[dict]]):
        self._threads = threads
        self.expanded: list[str] = []

    def conversation_message_ids(self, thread_id: str) -> list[str]:
        self.expanded.append(thread_id)
        return [m["message_id"] for m in self._threads[thread_id]]

    def fetch_inputs_by_ids(self, message_ids: list[str]) -> list[dict]:
        by_id = {m["message_id"]: m for t in self._threads.values() for m in t}
        return [by_id[i] for i in message_ids]


def test_a_pre_floor_sibling_of_a_live_thread_is_kept():
    """Dropping it would rebuild the STU-963 split by policy instead of disagreement."""
    old = _input("first", "t1", "2026-07-01T09:00:00+00:00")
    fresh = _input("reply", "t1", "2026-08-30T09:00:00+00:00")
    fetcher = _Fetcher({"t1": [old, fresh]})

    result = expand_to_conversations(fetcher, [fresh], start_date=date(2026, 8, 19))

    assert [m["message_id"] for m in result] == ["reply", "first"]


def test_a_wholly_pre_floor_thread_is_never_reached_for():
    """Filtered before expansion, so it costs no conversation read either."""
    old = _input("first", "t1", "2026-07-01T09:00:00+00:00")
    fetcher = _Fetcher({"t1": [old]})

    assert expand_to_conversations(fetcher, [old], start_date=date(2026, 8, 19)) == []
    assert fetcher.expanded == []


def test_the_floor_is_not_optional():
    with pytest.raises(TypeError):
        expand_to_conversations(_Fetcher({}), [])


# --- the deployment's own clock (STU-1462) ----------------------------------

def test_resolve_reads_todays_date_in_the_given_timezone(monkeypatch):
    from zoneinfo import ZoneInfo

    # 01:30 UTC on the 20th is still the 19th west of the date line.
    now = datetime(2026, 8, 20, 1, 30, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def now(cls, tzinfo=None):
            return now if tzinfo is None else now.astimezone(tzinfo)

    monkeypatch.setattr(window, "datetime", _Frozen)
    assert window.resolve(None, ZoneInfo("Pacific/Honolulu")) == date(2026, 8, 19)
    assert window.resolve(None, window.CLUB_TZ) == date(2026, 8, 19)


def test_since_iso_and_in_scope_use_the_given_timezone():
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Pacific/Honolulu")
    # Midnight in Honolulu on the 19th is 10:00 UTC.
    assert window.since_iso(date(2026, 8, 19), tz) == "2026-08-19T10:00:00+00:00"

    just_before = _input("a", "t", "2026-08-19T09:59:59+00:00")
    just_after = _input("b", "t", "2026-08-19T10:00:00+00:00")
    assert window.in_scope([just_before, just_after], date(2026, 8, 19), tz) == [just_after]
