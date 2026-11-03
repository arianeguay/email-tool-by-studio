"""Can a live source flip a verdict the QA gate freezes? (STU-1114)"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from email_classifier import (qa_live_sources, qa_provider, qa_replay,
                              qa_studio_version, websearch)
from email_classifier.qa_replay import ERROR
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM = REPO_ROOT / "scripts" / "qa_silent_tool.py"
SOURCES = deployment.ROOT / "rules" / "web_sources.yaml"

SITE = "org_web-search_site"
PRECEDENTS = "mail_precedents-search_mail"

# A resolved provider, the way every harness records one (STU-1175): what ran, not
# what a flag asked for.
PROVIDER = "anthropic"

# A resolved Studio version, the way every harness now records one too (STU-1241).
STUDIO_VERSION = "0.19.0"


def _case(live, silent, live_called=(SITE,), silent_called=(SITE,),
          live_unreachable=(), silent_unreachable=()):
    return qa_live_sources.CaseResult(
        "a-case", "a gate",
        qa_live_sources.Arm(qa_live_sources.LIVE, live, live_called, live_unreachable),
        qa_live_sources.Arm(qa_live_sources.SILENT, silent, silent_called, silent_unreachable),
    )


# --- the empty answer is the tool's own, not one written out here ---


def test_the_site_answers_with_no_matches_and_no_errors():
    """An error lands in `errors`, which the reviewer's prompt reads as a page it could
    not reach — the other question. Silence is a page that holds nothing."""
    answer = qa_live_sources.empty_answer(qa_live_sources.WEBSEARCH, "chaussures", SOURCES)

    assert answer["matches"] == []
    assert answer["errors"] == []
    assert answer["pages_searched"] == websearch.load_pages(SOURCES)


def test_the_precedents_answer_with_no_matches():
    answer = qa_live_sources.empty_answer(qa_live_sources.PRECEDENTS, "remboursement", SOURCES)

    assert answer == {"query": "remboursement", "matches": []}


def test_a_tool_with_no_defined_empty_answer_is_refused():
    """`org_memory-read_memory` raises on a name not on disk, which is unreachable and
    not empty (STU-1139) — so it is not silenceable and says so."""
    with pytest.raises(ValueError):
        qa_live_sources.empty_answer("email_classifier.club_memory", "x", SOURCES)

    assert "email_classifier.club_memory" not in qa_live_sources.SILENCEABLE


# --- the shim answers, and hands everything else to the real interpreter ---


def _shim(argv, silenced):
    return subprocess.run(
        [sys.executable, str(SHIM), *argv],
        capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin",
            qa_live_sources.SILENCED_ENV: silenced,
            qa_live_sources.REAL_PYTHON_ENV: sys.executable,
            deployment.ROOT_ENV: str(deployment.ROOT),
        },
    )


def test_the_shim_answers_a_silenced_module_with_an_empty_payload():
    result = _shim(["-m", qa_live_sources.PRECEDENTS, "--query", "gymnase"],
                   qa_live_sources.PRECEDENTS)

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"query": "gymnase", "matches": []}


def test_the_shim_exits_zero_so_the_tool_reads_as_reachable():
    """An empty answer and an unreachable tool are the two things this keeps apart: a
    non-zero exit here would be measuring STU-1139's question instead of this one."""
    result = _shim(["-m", qa_live_sources.PRECEDENTS, "--query", "x"],
                   qa_live_sources.PRECEDENTS)

    assert result.returncode == 0
    assert result.stderr == ""


def test_a_module_not_silenced_goes_to_the_real_interpreter():
    result = _shim(["-c", "print('real')"], qa_live_sources.PRECEDENTS)

    assert result.returncode == 0
    assert result.stdout.strip() == "real"


def test_the_shim_reaches_the_studio_child_through_the_replay(tmp_path, monkeypatch):
    """The arm differs from the live one only in what came back, so what carries it is
    the environment the tool commands are launched in — nothing else moves."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(qa_replay.subprocess, "run", fake_run)
    qa_replay.judge(
        {"thread_id": "t"}, {"draft_body": "b"}, tmp_path, "run", tmp_path,
        extra_env={"PYTHON": "/shim", qa_live_sources.SILENCED_ENV: "a,b"},
    )

    assert seen["PYTHON"] == "/shim"
    assert seen[qa_live_sources.SILENCED_ENV] == "a,b"


# --- an arm that did not agree with itself is not one side of a comparison ---


def test_an_arm_that_returned_two_verdicts_has_not_settled():
    arm = qa_live_sources.Arm(qa_live_sources.LIVE, ("rejected", "approved"))

    assert arm.settled is None


def test_an_unsettled_arm_is_reported_as_noise_not_as_a_divergence():
    result = _case(("rejected", "approved"), ("rejected", "rejected"))

    assert not result.diverged
    assert "UNSETTLED" in result.finding
    assert "noise floor" in result.finding


# --- the findings ---


def test_a_case_that_rejects_both_ways_is_judged_on_its_prose():
    result = _case(("rejected", "rejected"), ("rejected", "rejected"))

    assert result.on_prose
    assert not result.diverged
    assert result.finding.startswith("prose")
    assert "answered nothing" in result.finding


def test_a_verdict_that_moves_names_the_tool_the_live_arm_was_answered_by():
    result = _case(("rejected", "rejected"), ("approved", "approved"))

    assert result.diverged
    assert not result.on_prose
    assert "LIVE SOURCE" in result.finding
    assert SITE in result.finding


def test_a_tool_the_live_arm_never_called_is_not_a_candidate():
    """Silencing a source the reviewer never reached for cannot have moved anything."""
    result = _case(("rejected", "rejected"), ("rejected", "rejected"), live_called=())

    assert result.candidates == ()


def test_a_tool_with_no_empty_answer_is_never_a_candidate():
    """`org_memory-read_memory` is not silenced, so the silent arm had it too."""
    result = _case(("rejected", "rejected"), ("rejected", "rejected"),
                   live_called=("org_memory-read_memory",))

    assert result.candidates == ()


def test_a_silent_arm_that_searched_nothing_says_so_rather_than_claiming_a_silencing():
    """The reviewer decides whether to search. Declining reaches the same conclusion by
    a different route, and the reader deciding what the number covers needs which."""
    result = _case(("rejected", "rejected"), ("rejected", "rejected"), silent_called=())

    assert result.on_prose
    assert result.silenced_reached == ()
    assert "searched nothing at all" in result.finding
    assert "searched nothing at all" in qa_live_sources.render([result], 2, PROVIDER, STUDIO_VERSION)


def test_a_silent_arm_the_shim_answered_names_the_tools_it_answered():
    result = _case(("rejected", "rejected"), ("rejected", "rejected"),
                   silent_called=(SITE, PRECEDENTS))

    assert result.silenced_reached == (SITE, PRECEDENTS)
    assert "answered nothing" in result.finding


def test_an_unreachable_tool_disqualifies_both_arms():
    """Neither arm is production's reviewer, whichever way its verdict went."""
    result = _case(("rejected", "rejected"), ("rejected", "rejected"),
                   live_unreachable=(PRECEDENTS,))

    assert not result.on_prose
    assert result.finding.startswith("UNVERIFIED")
    assert PRECEDENTS in result.finding


def test_an_unreadable_arm_is_not_a_hold():
    result = _case(("rejected", "rejected"), (ERROR, ERROR))

    assert not result.on_prose
    assert result.finding.startswith("UNREADABLE")


def test_a_case_approved_both_ways_is_the_gate_failing_not_a_dependency():
    result = _case(("approved", "approved"), ("approved", "approved"))

    assert not result.diverged
    assert not result.on_prose
    assert "GATE FAILURE" in result.finding


# --- the record ---


def test_the_table_carries_both_arms_and_the_finding():
    rendered = qa_live_sources.render([_case(("rejected",), ("rejected",))], 1, PROVIDER, STUDIO_VERSION)

    assert qa_live_sources.LIVE in rendered
    assert qa_live_sources.SILENT in rendered
    assert "judged on prose alone" in rendered


def test_the_json_records_every_verdict_of_both_arms():
    payload = qa_live_sources.as_dict([_case(("rejected", "rejected"),
                                             ("approved", "approved"))], 2, PROVIDER, STUDIO_VERSION)

    assert payload["diverged"] == 1
    assert payload["on_prose"] == 0
    arms = payload["results"][0]["arms"]
    assert arms[qa_live_sources.LIVE]["verdicts"] == ["rejected", "rejected"]
    assert arms[qa_live_sources.SILENT]["settled"] == "approved"


# --- which provider produced the measurement (STU-1175) -------------------------------


def test_the_json_names_the_provider_that_judged():
    payload = qa_live_sources.as_dict([_case(("rejected",), ("rejected",))], 1, PROVIDER, STUDIO_VERSION)
    assert payload[qa_provider.KEY] == PROVIDER


def test_the_table_says_what_it_was_measured_on():
    """The arms differ in what came back and in nothing else, so which provider answered
    them is a property of the run and belongs beside the tool-health line."""
    assert PROVIDER in qa_live_sources.render(
        [_case(("rejected",), ("rejected",))], 1, PROVIDER, STUDIO_VERSION)


def test_an_unresolved_provider_is_named_rather_than_left_blank():
    results = [_case(("rejected",), ("rejected",))]
    assert "cannot name" in qa_live_sources.render(results, 1, None, STUDIO_VERSION)
    assert qa_live_sources.as_dict(results, 1, None, STUDIO_VERSION)[qa_provider.KEY] is None


# --- which Studio version produced the measurement (STU-1241) -------------------------


def test_the_json_names_the_studio_version_that_ran():
    payload = qa_live_sources.as_dict(
        [_case(("rejected",), ("rejected",))], 1, PROVIDER, STUDIO_VERSION)
    assert payload[qa_studio_version.KEY] == STUDIO_VERSION


def test_the_table_says_which_studio_version_ran():
    assert STUDIO_VERSION in qa_live_sources.render(
        [_case(("rejected",), ("rejected",))], 1, PROVIDER, STUDIO_VERSION)


def test_an_unresolved_studio_version_is_named_rather_than_left_blank():
    results = [_case(("rejected",), ("rejected",))]
    assert "cannot name" in qa_live_sources.render(results, 1, PROVIDER, None)
    assert qa_live_sources.as_dict(results, 1, PROVIDER, None)[qa_studio_version.KEY] is None
