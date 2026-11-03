"""The erosion measurement's reading of a group replay (STU-1104)."""

import json
from pathlib import Path

import pytest
import yaml

from email_classifier import qa_erosion, qa_provider, qa_replay, qa_studio_version
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]

# A resolved provider, the way every harness records one (STU-1175): what ran, not
# what a flag asked for.
PROVIDER = "anthropic"

# A resolved Studio version, the way every harness now records one too (STU-1241).
STUDIO_VERSION = "0.19.0"


def _record(*verdicts: str) -> str:
    return json.dumps(
        {"stages": [{"stage_name": "draft-qa", "output": {"verdict": v}} for v in verdicts]}
    )


def _passes(*verdict_runs: tuple[str, ...]) -> qa_erosion.CaseResult:
    return qa_erosion.CaseResult(
        "case",
        "a gate",
        tuple(qa_erosion.Pass("case", n, v) for n, v in enumerate(verdict_runs, 1)),
    )


def test_verdicts_of_returns_one_entry_per_group_pass():
    assert qa_replay.verdicts_of(_record("rejected", "rejected", "approved")) == [
        "rejected", "rejected", "approved",
    ]


def test_verdict_of_still_reads_a_single_stage_replay():
    assert qa_replay.verdict_of(_record("rejected")) == "rejected"
    assert qa_replay.verdict_of("not json") == qa_replay.ERROR


def test_flip_k_counts_the_rejections_before_the_approval():
    p = qa_erosion.Pass("case", 1, ("rejected", "rejected", "approved"))
    assert p.flipped and p.flip_k == 2


def test_a_gate_that_holds_every_pass_has_no_flip():
    p = qa_erosion.Pass("case", 1, ("rejected", "rejected", "rejected"))
    assert not p.flipped and p.flip_k is None


def test_no_verdict_is_unreadable_rather_than_held():
    p = qa_erosion.Pass("case", 1, ())
    assert p.unreadable and not p.flipped
    assert "UNREADABLE" in _passes(()).finding


def test_a_cold_approval_is_reported_as_a_gate_failure_not_erosion():
    # K=0 is `make qa-gate` failing: nothing eroded, the gate never held.
    result = _passes(("approved",), ("approved",))
    assert not result.held_cold
    assert "COLD APPROVAL" in result.finding


def test_one_flip_in_several_repeats_defers_to_the_run_line():
    """Right per case and not the case's to decide (STU-1161): one flip in two repeats
    is a draw against STU-1014's floor, and four such cases were an eroding run."""
    result = _passes(("rejected", "rejected", "approved"), ("rejected",) * 3)
    assert result.flips == 1
    assert "inside the measured noise floor" in result.finding
    assert "the run line is what decides" in result.finding


def test_a_flip_in_every_repeat_is_the_finding():
    result = _passes(("rejected", "approved"), ("rejected", "approved"))
    assert "EROSION" in result.finding
    assert result.flip_ks == (1, 1)


def test_render_names_every_repeat_and_the_finding():
    out = qa_erosion.render([_passes(("rejected", "approved"), ("rejected",) * 3)], 3,
                            PROVIDER, STUDIO_VERSION)
    assert "repeat 1: rejected -> approved" in out
    assert "repeat 2: rejected -> rejected -> rejected" in out


def test_as_dict_reports_the_mean_flip_k_over_flips_only():
    payload = qa_erosion.as_dict(
        [_passes(("rejected", "approved"), ("rejected", "rejected", "approved"))], 3, 2,
        PROVIDER, STUDIO_VERSION
    )
    assert payload["flipped"] == 1
    assert payload["mean_flip_k"] == pytest.approx(1.5)


def test_as_dict_has_no_mean_when_nothing_flipped():
    assert qa_erosion.as_dict([_passes(("rejected",) * 3)], 3, 1, PROVIDER, STUDIO_VERSION)["mean_flip_k"] is None


def test_the_erosion_group_is_the_production_group_minus_the_drafter():
    """The measurement is only about `email-draft` if it reaches as far as it does and
    feeds the reviewer the same context. A group retuned on one side and not the other
    would publish a K axis production never has."""
    studio = deployment.ROOT / ".studio" / "pipelines"
    erosion = yaml.safe_load((studio / "draft-qa-erosion.pipeline.yaml").read_text())["stages"][0]
    production = yaml.safe_load((studio / "email-draft.pipeline.yaml").read_text())["stages"][0]

    assert erosion["max_iterations"] == production["max_iterations"]

    prod_qa = next(s for s in production["stages"] if s["name"] == "draft-qa")
    erosion_qa = next(s for s in erosion["stages"] if s["name"] == "draft-qa")
    assert erosion_qa["agent"] == prod_qa["agent"]
    assert erosion_qa["contract"] == prod_qa["contract"]
    assert erosion_qa["context"]["include"] == prod_qa["context"]["include"]
    # Pinned literally, not only by the equality above: the reviewer not seeing its own
    # last critique is STU-1160's fix, and equality alone would let both sides drift
    # back together. Handed `group_feedback`, this reviewer read it as a description of
    # the draft and approved a body it had just rejected — four of STU-1148's five
    # flips. What that costs this harness is written on the pipeline: with nothing
    # moving between passes it measures the noise floor, not erosion.
    assert "group_feedback" not in erosion_qa["context"]["include"]

    # The drafter and the mailbox write are what the replay removes, and nothing else.
    assert [s["name"] for s in erosion["stages"]] == ["replay-draft", "draft-qa"]


PRECEDENTS = "mail_precedents-search_mail"


def _unreachable_passes(*verdict_runs: tuple[str, ...]) -> qa_erosion.CaseResult:
    return qa_erosion.CaseResult(
        "case",
        "a gate",
        tuple(
            qa_erosion.Pass("case", n, v, (PRECEDENTS,))
            for n, v in enumerate(verdict_runs, 1)
        ),
    )


def test_a_hold_says_which_source_the_reviewer_was_missing():
    """The reading a missing source manufactures: no flip, from a reviewer that never
    reached one of the three things production's reviewer reads (STU-1147)."""
    result = _unreachable_passes(("rejected",) * 3, ("rejected",) * 3)
    assert result.unreachable == (PRECEDENTS,)
    assert "held at every K" in result.finding
    assert PRECEDENTS in result.finding
    assert "not yet a reading of production's reviewer" in result.finding


def test_a_lone_flip_is_qualified_too():
    result = _unreachable_passes(("rejected", "approved"), ("rejected",) * 3)
    assert "noise floor" in result.finding and PRECEDENTS in result.finding


def test_a_repeated_flip_stands_on_its_own():
    """A flip is a finding whichever sources the reviewer had, so the qualification
    would only dilute it; `render` names the tool beside it regardless."""
    result = _unreachable_passes(("rejected", "approved"), ("rejected", "approved"))
    assert result.finding.startswith("EROSION")
    assert PRECEDENTS not in result.finding
    assert PRECEDENTS in qa_erosion.render([result], 3, PROVIDER, STUDIO_VERSION)


def test_render_names_the_tool_and_counts_the_unverified_cases():
    out = qa_erosion.render(
        [_unreachable_passes(("rejected",) * 3), _passes(("rejected",) * 3)], 3, PROVIDER, STUDIO_VERSION)
    assert f"unreachable: {PRECEDENTS}" in out
    assert "1 of 2 case(s) were judged by a reviewer that never reached" in out


def test_a_run_where_every_tool_answered_says_nothing_about_tools():
    out = qa_erosion.render([_passes(("rejected",) * 3)], 3, PROVIDER, STUDIO_VERSION)
    payload = qa_erosion.as_dict([_passes(("rejected",) * 3)], 3, 1, PROVIDER, STUDIO_VERSION)
    assert "unreachable" not in out
    assert payload["unverified"] == 0
    assert payload["results"][0]["unreachable"] == []


def test_as_dict_carries_the_tools_per_case_and_per_pass():
    payload = qa_erosion.as_dict([_unreachable_passes(("rejected",) * 3)], 3, 1, PROVIDER, STUDIO_VERSION)
    assert payload["unverified"] == 1
    assert payload["results"][0]["unreachable"] == [PRECEDENTS]
    assert payload["results"][0]["passes"][0]["unreachable"] == [PRECEDENTS]


def test_the_erosion_measurement_stays_a_measurement(monkeypatch, tmp_path):
    """An unreachable tool annotates and does not fail: the exit code is what separates
    this from `make qa-gate`, which exits 4 because it stamps a record (STU-1147)."""
    import importlib

    script = importlib.import_module("scripts.qa_erosion")

    monkeypatch.setattr(
        script.qa_replay, "judge_passes",
        lambda *a, **k: qa_replay.Judgement(("rejected",) * 3, (PRECEDENTS,)),
    )
    out = tmp_path / "qa-erosion.json"
    assert script.main(["--repeats", "1", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["unverified"] > 0


# --- The run's own reading, which no per-case line can reach (STU-1161) ---


def _run(*cases: qa_erosion.CaseResult) -> qa_erosion.RunReading:
    return qa_erosion.RunReading(cases)


def _case(case_id: str, flips: int, repeats: int = 6) -> qa_erosion.CaseResult:
    """`flips` replays that reversed at K=1, the rest holding all three passes."""
    runs = [("rejected", "approved")] * flips + [("rejected",) * 3] * (repeats - flips)
    return qa_erosion.CaseResult(
        case_id,
        "a gate",
        tuple(qa_erosion.Pass(case_id, n, v) for n, v in enumerate(runs, 1)),
    )


def _stu_1148() -> list[qa_erosion.CaseResult]:
    """The three STU-1148 passes pooled: 4 cases x 6 replays, 5 flips all at K=1,
    against a cold pass that rejected 24 of 24."""
    return [_case("stu-1070", 2), _case("stu-1101", 1),
            _case("stu-1106", 1), _case("stu-1113", 1)]


def test_the_stu_1148_run_reads_as_erosion_though_no_case_crosses_its_own_bar():
    results = _stu_1148()
    assert [r.flips for r in results] == [2, 1, 1, 1]
    # Three of the four are the shape the per-case bar cannot reach.
    assert sum("EROSION" in r.finding for r in results) == 1

    run = _run(*results)
    assert (len(run.passes), run.flips, run.cold_rejections) == (24, 5, 24)
    assert run.control_holds
    assert run.finding.startswith("EROSION")
    assert "5/24 replay(s) reversed their own K=0 rejection" in run.finding
    assert "the cold pass rejected 24/24" in run.finding


def test_the_run_line_is_the_first_thing_under_the_header():
    """The failure being fixed is a reader who stops at the table."""
    out = qa_erosion.render(_stu_1148(), 3, PROVIDER, STUDIO_VERSION).splitlines()
    assert "5 flip(s) in 24 replay(s) across 4 case(s)" in out[0]
    assert out[1].startswith("EROSION")
    assert "inside the measured noise floor" in "\n".join(out[2:])


def test_a_run_where_every_replay_held_still_reports_no_finding():
    run = _run(_case("stu-1070", 0), _case("stu-1113", 0))
    assert run.flips == 0 and run.control_holds
    assert run.finding.startswith("NO EROSION")
    assert "12 replay(s) whose cold pass rejected 12/12" in run.finding
    assert qa_erosion.render([_case("stu-1070", 0)], 3, PROVIDER, STUDIO_VERSION).splitlines()[1].startswith("NO EROSION")


def test_a_single_flip_is_still_the_run_verdict_and_prints_its_own_weight():
    """No threshold: the licence is the paired K=0 rejection inside each replay plus a
    cold pass that agreed with itself, so one flip is the same verdict — and the line
    carries both numerators because it is not the same weight."""
    run = _run(_case("stu-1070", 1))
    assert run.finding.startswith("EROSION")
    assert "1/6 replay(s)" in run.finding and "rejected 6/6" in run.finding


def test_a_cold_approval_leaves_the_run_with_no_control():
    """A flip and a gate that never held are the same observation without it."""
    result = _passes(("approved",), ("rejected", "approved"))
    run = _run(result)
    assert not run.control_holds
    assert run.finding.startswith("NO CONTROL")
    assert "the cold pass approved in 1 of 2 replay(s)" in run.finding


def test_an_unreadable_replay_is_dropped_from_the_denominator_and_named():
    run = _run(qa_erosion.CaseResult("case", "a gate", (
        qa_erosion.Pass("case", 1, ()),
        qa_erosion.Pass("case", 2, ("rejected", "approved")),
        qa_erosion.Pass("case", 3, ("rejected",) * 3),
    )))
    assert (len(run.passes), len(run.readable), run.unreadable) == (3, 2, 1)
    assert "1/2 replay(s) reversed" in run.finding
    assert "1 replay(s) returned no verdict and are not counted" in run.finding


def test_a_run_that_judged_nothing_is_unreadable_rather_than_held():
    run = _run(_passes((), ()))
    assert run.finding.startswith("UNREADABLE")
    assert "this run measured nothing" in run.finding


def test_the_runs_finding_of_nothing_names_the_missing_tool():
    """Same rule as the per-case line: the reading a missing source manufactures is the
    one that carries it, and a flip stands on its own (STU-1147)."""
    held = _unreachable_passes(("rejected",) * 3, ("rejected",) * 3)
    assert _run(held).finding.startswith("NO EROSION")
    assert PRECEDENTS in _run(held).finding

    flipped = _unreachable_passes(("rejected", "approved"), ("rejected", "approved"))
    assert PRECEDENTS not in _run(flipped).finding


def test_as_dict_carries_the_run_reading_beside_the_per_case_ones():
    payload = qa_erosion.as_dict(_stu_1148(), 3, 6, PROVIDER, STUDIO_VERSION)
    # `flipped` counts cases and would read as four draws; the run block is the verdict.
    assert payload["flipped"] == 4
    assert payload["run"] == {
        "replays": 24,
        "readable": 24,
        "unreadable": 0,
        "flips": 5,
        "cold_rejections": 24,
        "control_holds": True,
        "finding": qa_erosion.RunReading(tuple(_stu_1148())).finding,
    }

# --- which provider produced the measurement (STU-1175) -------------------------------


def test_the_json_names_the_provider_that_judged():
    """This file is read the day it prints and never as an assertion, so the provider is
    the only thing in it that can say the numbers were not taken on production's."""
    payload = qa_erosion.as_dict([_passes(("rejected",) * 3)], 3, 1, PROVIDER, STUDIO_VERSION)
    assert payload[qa_provider.KEY] == PROVIDER


def test_the_table_says_what_it_was_measured_on():
    """Beside the tool-health line and for its reason: what answered and what ran are
    the two facts that say whether the flips under them are about production at all."""
    assert PROVIDER in qa_erosion.render([_passes(("rejected",) * 3)], 3, PROVIDER, STUDIO_VERSION)


def test_an_unresolved_provider_is_named_rather_than_left_blank():
    assert "cannot name" in qa_erosion.render([_passes(("rejected",) * 3)], 3, None, STUDIO_VERSION)
    assert qa_erosion.as_dict(
        [_passes(("rejected",) * 3)], 3, 1, None, STUDIO_VERSION)[qa_provider.KEY] is None


# --- which Studio version produced the measurement (STU-1241) -------------------------


def test_the_json_names_the_studio_version_that_ran():
    payload = qa_erosion.as_dict([_passes(("rejected",) * 3)], 3, 1, PROVIDER, STUDIO_VERSION)
    assert payload[qa_studio_version.KEY] == STUDIO_VERSION


def test_the_table_says_which_studio_version_ran():
    assert STUDIO_VERSION in qa_erosion.render(
        [_passes(("rejected",) * 3)], 3, PROVIDER, STUDIO_VERSION)


def test_an_unresolved_studio_version_is_named_rather_than_left_blank():
    assert "cannot name" in qa_erosion.render([_passes(("rejected",) * 3)], 3, PROVIDER, None)
    assert qa_erosion.as_dict(
        [_passes(("rejected",) * 3)], 3, 1, PROVIDER, None)[qa_studio_version.KEY] is None
