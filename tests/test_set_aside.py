"""A conversation stage 2 keeps failing on is set aside after N polls (STU-2109)."""

import json
import os

from email_classifier.audit import stage2_calls
from email_classifier.orchestrate import batch, set_aside


def _report(directory, name, mtime, verdicts):
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"verdicts": verdicts}), encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _verdict(message_id, status, path="llm"):
    return {"message_id": message_id, "path": path, "status": status}


def test_a_message_that_failed_n_polls_in_a_row_is_set_aside(tmp_path):
    for i in range(set_aside.MAX_FAILED_POLLS):
        _report(tmp_path, f"r{i}", 100 + i, [_verdict("m1", "error")])
    streaks = set_aside.failed_polls(tmp_path, {"m1"})
    assert set_aside.is_set_aside(("m1",), streaks)


def test_a_message_that_failed_once_is_retried(tmp_path):
    _report(tmp_path, "r0", 100, [_verdict("m1", "error")])
    streaks = set_aside.failed_polls(tmp_path, {"m1"})
    assert streaks == {"m1": 1}
    assert not set_aside.is_set_aside(("m1",), streaks)


def test_a_success_ends_the_streak(tmp_path):
    for i in range(set_aside.MAX_FAILED_POLLS):
        _report(tmp_path, f"old{i}", 100 + i, [_verdict("m1", "error")])
    _report(tmp_path, "ok", 200, [_verdict("m1", "ok")])
    _report(tmp_path, "new", 300, [_verdict("m1", "error")])
    assert set_aside.failed_polls(tmp_path, {"m1"}) == {"m1": 1}


def test_a_poll_that_did_not_see_the_message_does_not_break_the_streak(tmp_path):
    _report(tmp_path, "a", 100, [_verdict("m1", "error")])
    _report(tmp_path, "b", 200, [])
    _report(tmp_path, "c", 300, [_verdict("m1", "error"), _verdict("m1", "ok", "deterministic")])
    assert set_aside.failed_polls(tmp_path, {"m1"}) == {"m1": 2}


def test_a_new_message_in_the_thread_brings_the_conversation_back():
    streaks = {"old": set_aside.MAX_FAILED_POLLS, "new": 0}
    assert not set_aside.is_set_aside(("old", "new"), streaks)


def test_an_empty_report_dir_sets_nothing_aside(tmp_path):
    assert set_aside.failed_polls(tmp_path / "missing", {"m1"}) == {"m1": 0}


def test_the_set_aside_verdict_is_an_error_that_is_not_a_call():
    record = {"message_id": "m1", "thread_id": "t1", "rule_id": "catch_all", "date": ""}
    verdict = batch.set_aside_verdict(record)
    assert verdict.status == "error" and verdict.error == set_aside.SET_ASIDE_ERROR
    assert stage2_calls([{"thread_id": "t1", "path": "llm", "error": verdict.error}]) == 0
