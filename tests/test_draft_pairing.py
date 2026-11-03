"""Which sent reply a draft is compared with (STU-1998)."""

from email_classifier import draft_pairing


def _row(thread_id, at, run_id="run-1", status="ok"):
    return {"thread_id": thread_id, "timestamp": at, "run_id": run_id, "status": status}


def test_the_write_time_is_the_runs_last_ok_write_of_the_draft():
    entries = [
        _row("T1", "2026-10-16T12:00:00+00:00"),
        _row("T1", "2026-10-16T12:05:00+00:00"),
        _row("T1", "2026-10-16T13:00:00+00:00", run_id="run-2"),
        _row("T1", "2026-10-16T14:00:00+00:00", status="error"),
        _row("T2", "2026-10-16T12:01:00+00:00"),
    ]
    assert draft_pairing.write_times(entries, "run-1") == {
        "T1": "2026-10-16T12:05:00+00:00", "T2": "2026-10-16T12:01:00+00:00"}


def test_the_pair_is_the_first_send_after_the_draft_not_the_newest():
    """A conversation answered several times holds a send per later question, and one
    answered before the draft holds a send that predates it."""
    sends = [{"id": "before", "at": "2026-07-31T09:00:00Z"},
             {"id": "later", "at": "2026-10-20T09:00:00Z"},
             {"id": "reply", "at": "2026-10-16T15:00:00Z"}]
    first = draft_pairing.first_send_after(
        sends, "2026-10-16T12:00:00+00:00", lambda s: s["at"])
    assert first["id"] == "reply"


def test_no_send_after_the_draft_leaves_it_unjoined():
    sends = [{"id": "before", "at": "2026-07-31T09:00:00Z"}]
    assert draft_pairing.first_send_after(
        sends, "2026-10-16T12:00:00+00:00", lambda s: s["at"]) is None
