"""Candidate drafting rules read off (draft, sent) pairs (STU-1108)."""

import json

import yaml

from email_classifier import correction_rules, coverage_read
from tests.conftest import stub_detector

DRAFT_ASK = "Hello, could you tell us your child's date of birth so we can check?"
SENT_LINK = "Hello, registration for the tryouts is done online on the registration page."
DRAFT_PROMISE = "Thank you, we will get back to you about the schedule next week."
SENT_ANSWER = "Thank you, the schedule is already on the website under Calendar."


def _pairs(n):
    return {f"P{i}": {"thread_id": f"T{i}", "draft": f"draft {i}", "sent": f"sent {i}"}
            for i in range(1, n + 1)}


def test_a_reply_sent_unchanged_corrects_nothing_and_is_left_out():
    drafted = {"T1": DRAFT_ASK, "T2": DRAFT_PROMISE, "T3": "unsent draft"}
    sent = {"T1": DRAFT_ASK, "T2": SENT_ANSWER}
    assert list(correction_rules.corrected(drafted, sent)) == ["T2"]


def test_a_rule_needs_two_pairs_and_one_pair_is_named_not_promoted():
    output = {"candidates": [
        {"rule": "Answer instead of asking", "pairs": ["P1", "P2"], "why": "w"},
        {"rule": "Shorter", "pairs": ["P3"], "why": "w"},
    ], "unexplained": []}
    result = correction_rules.candidates_of(output, _pairs(3))
    assert result["reading"] == "ok"
    assert [(c["rule"], c["support"]) for c in result["candidates"]] == [
        ("Answer instead of asking", 2)]
    assert [c["pairs"][0]["thread_id"] for c in result["candidates"]] == ["T1"]
    assert [c["rule"] for c in result["single_pair"]] == ["Shorter"]


def test_support_is_counted_on_pairs_that_exist():
    output = {"candidates": [{"rule": "r", "pairs": ["P1", "P9", "P1"], "why": ""}],
              "unexplained": ["P2"]}
    result = correction_rules.candidates_of(output, _pairs(2))
    assert result["candidates"] == []
    assert result["single_pair"][0]["support"] == 1


def test_a_pair_no_rule_explains_is_reported_as_unexplained():
    output = {"candidates": [{"rule": "r", "pairs": ["P1", "P2"], "why": ""}],
              "unexplained": ["P3"]}
    assert correction_rules.candidates_of(output, _pairs(3))["unexplained"] == ["T3"]


def test_a_pair_both_cited_and_unexplained_or_neither_is_incoherent():
    both = {"candidates": [{"rule": "r", "pairs": ["P1", "P2"]}], "unexplained": ["P2"]}
    neither = {"candidates": [{"rule": "r", "pairs": ["P1", "P2"]}], "unexplained": []}
    assert correction_rules.candidates_of(both, _pairs(2))["reading"] == "incoherent"
    assert correction_rules.candidates_of(neither, _pairs(3))["reading"] == "incoherent"


def test_wrong_shape_is_an_error_and_serialised_lists_still_read():
    assert correction_rules.candidates_of({}, _pairs(2))["reading"] == "error"
    output = {"candidates": json.dumps([{"rule": "r", "pairs": '["P1", "P2"]'}]),
              "unexplained": "[]"}
    assert correction_rules.candidates_of(output, _pairs(2))["candidates"][0]["support"] == 2


def test_both_bodies_are_redacted_before_the_reader_sees_them(monkeypatch):
    stub_detector(monkeypatch)
    monkeypatch.setattr(correction_rules, "vouched", lambda: frozenset({"hello", "thanks"}))
    pairs = correction_rules.redacted({"T1": (
        "Hello, write to coach@example.org about it.",
        "Thanks, Bramwell answers that at the front desk.")})
    assert pairs["P1"]["thread_id"] == "T1"
    assert "example.org" not in pairs["P1"]["draft"]
    assert "EMAIL_X" in pairs["P1"]["draft"]
    assert "Bramwell" not in pairs["P1"]["sent"]
    assert "NAME_X" in pairs["P1"]["sent"]


def test_propose_hands_the_reader_every_pair_in_one_item(monkeypatch, tmp_path):
    items = []

    def run_stage(pipeline, stage, item_path, cwd, provider, label):
        items.append(yaml.safe_load(item_path.read_text(encoding="utf-8")))
        assert (pipeline, stage) == (correction_rules.PIPELINE, correction_rules.STAGE)
        return {"candidates": [{"rule": "r", "pairs": ["P1", "P2"], "why": ""}],
                "unexplained": []}

    monkeypatch.setattr(coverage_read, "run_stage", run_stage)
    result = correction_rules.propose(_pairs(2), tmp_path)
    assert len(items) == 1
    assert [p["id"] for p in items[0]["pairs"]] == ["P1", "P2"]
    assert set(items[0]["pairs"][0]) == {"id", "draft", "sent"}
    assert items[0]["run_id"].startswith("draft-corrections-")
    assert result["candidates"][0]["support"] == 2


def test_fewer_pairs_than_the_floor_calls_no_reader(monkeypatch, tmp_path):
    def run_stage(*args):
        raise AssertionError("no reader for a window that cannot support a rule")

    monkeypatch.setattr(coverage_read, "run_stage", run_stage)
    result = correction_rules.propose(_pairs(1), tmp_path)
    assert result["candidates"] == [] and "no reader was called" in result["note"]
