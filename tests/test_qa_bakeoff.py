"""The QA bake-off's corpus extraction and verdict diff (STU-1014)."""

import json

import pytest

from email_classifier.qa_bakeoff import (
    Disagreement,
    OrphanDraft,
    compare,
    drafts_from_journal,
    unreachable_note,
)


def _event(stage, output, **extra):
    return json.dumps({"event": "stage_complete", "stage": stage, "output": output, **extra})


def _pre(*thread_ids):
    return _event(
        "drafts-pre",
        {"pending": [{"thread_id": t, "subject": f"re {t}"} for t in thread_ids]},
    )


def _draft(thread_id, body="bonjour"):
    return _event("write-draft", {"thread_id": thread_id, "draft_body": body})


def test_pairs_each_draft_with_its_item():
    drafts = drafts_from_journal([_pre("A", "B"), _draft("A"), _draft("B")])

    assert [d.key for d in drafts] == ["A#1", "B#1"]
    assert [d.item["subject"] for d in drafts] == ["re A", "re B"]


def test_a_thread_redrafted_by_the_group_yields_one_entry_per_attempt():
    """The group rewrites after a rejection, so a thread can carry several drafts —
    each is a draft a judge has to rule on, and they are not the same text."""
    drafts = drafts_from_journal(
        [_pre("A"), _draft("A", "first"), _draft("A", "second")]
    )

    assert [d.key for d in drafts] == ["A#1", "A#2"]
    assert [d.draft["draft_body"] for d in drafts] == ["first", "second"]


def test_interleaved_fanout_still_pairs_by_thread():
    """Concurrency puts two children's events in one file, and `stage_complete` carries
    the root run id — so position says nothing about which child wrote what."""
    drafts = drafts_from_journal(
        [_pre("A", "B"), _draft("B"), _draft("A"), _draft("B", "again")]
    )

    assert {d.key: d.item["subject"] for d in drafts} == {
        "B#1": "re B", "A#1": "re A", "B#2": "re B",
    }


def test_events_that_are_not_stage_completions_are_ignored():
    lines = [
        json.dumps({"event": "stage_start", "stage": "write-draft"}),
        json.dumps({"event": "stage_context", "stage": "draft-qa", "run_id": "abc"}),
        "not json at all",
        _pre("A"),
        _draft("A"),
    ]

    assert [d.key for d in drafts_from_journal(lines)] == ["A#1"]


def test_a_draft_with_no_item_is_refused():
    """Dropping it would shrink the corpus without saying so, and a bake-off that
    scores fewer drafts than the run wrote under-reports every disagreement."""
    with pytest.raises(OrphanDraft, match="ORPHAN"):
        drafts_from_journal([_pre("A"), _draft("ORPHAN")])


def test_agreement_on_identical_verdicts():
    result = compare(
        {"A#1": "approved", "B#1": "rejected"},
        {"A#1": "approved", "B#1": "rejected"},
    )

    assert result.judged == 2
    assert result.agreed == 2
    assert result.agreement == 1.0
    assert result.disagreements == ()


def test_a_waved_through_rejection_is_the_half_that_matters():
    result = compare(
        {"A#1": "rejected", "B#1": "approved"},
        {"A#1": "approved", "B#1": "rejected"},
    )

    assert result.agreed == 0
    assert result.missed_rejections == (
        Disagreement("A#1", "rejected", "approved"),
    )


def test_an_arm_that_could_not_read_a_verdict_disagrees_rather_than_vanishing():
    result = compare({"A#1": "approved"}, {"A#1": "error"})

    assert result.disagreements == (Disagreement("A#1", "approved", "error"),)
    assert result.missed_rejections == ()


def test_arms_over_different_drafts_are_refused():
    with pytest.raises(ValueError, match="B#1"):
        compare({"A#1": "approved", "B#1": "approved"}, {"A#1": "approved"})


# --- what the arms could not reach (STU-1147) ---

PRECEDENTS = "mail_precedents-search_mail"


def test_a_run_where_every_tool_answered_says_nothing():
    assert unreachable_note({"opus": (), "sonnet": ()}) == []


def test_the_same_tool_down_for_both_arms_is_a_comparison_that_survives():
    lines = unreachable_note({"opus": (PRECEDENTS,), "sonnet": (PRECEDENTS,)})
    assert f"opus: never reached {PRECEDENTS}" in lines[1]
    assert "compared like with like" in lines[-1]
    assert "neither judged as production's reviewer does" in lines[-1]


def test_a_tool_down_for_one_arm_only_is_two_different_reviewers():
    lines = unreachable_note({"opus": (PRECEDENTS,), "sonnet": ()})
    assert len([l for l in lines if "never reached" in l]) == 1
    assert "two different reviewers" in lines[-1]


def test_two_arms_missing_different_tools_is_not_like_with_like():
    lines = unreachable_note({"opus": (PRECEDENTS,), "sonnet": ("org_web-search_site",)})
    assert "two different reviewers" in lines[-1]
