"""Classifier bake-off scoring (STU-1045)."""

import json
from pathlib import Path

import pytest
import yaml

from email_classifier import classify_bakeoff as bakeoff
from email_classifier.core.contract import load_taxonomy
from email_classifier.eval.ab import Arm, Verdict
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")


def arm(name: str, *rounds: dict[str, Verdict]) -> Arm:
    return Arm(name=name, fields=[], repeats=list(rounds))


def verdict(action: str, bucket: str = "A") -> Verdict:
    return Verdict(bucket=bucket, action=action)


GOOD = {"m1": verdict("needs_reply"), "m2": verdict("keep_visible")}
LABELS = {
    "m1": bakeoff.GoldenLabel("A", "needs_reply"),
    "m2": bakeoff.GoldenLabel("A", "keep_visible"),
}


def test_a_model_agreeing_with_itself_and_the_golden_scores_perfect():
    score = bakeoff.score(arm("m", GOOD, GOOD), LABELS, bakeoff.HEADLINE)
    assert (score.stable, score.correct) == (2, 2)
    assert score.agreement == 1.0
    assert score.noise_floor == 1.0


def test_a_message_the_model_answers_two_ways_is_not_scored_against_the_golden():
    """Half the point of the repeats: a coin flip is not a wrong verdict."""
    score = bakeoff.score(
        arm("m", GOOD, {**GOOD, "m2": verdict("needs_reply")}), LABELS, bakeoff.HEADLINE
    )
    assert score.unstable == ("m2",)
    assert (score.judged, score.stable) == (2, 1)
    assert score.noise_floor == 0.5
    # The unstable message is out of the numerator AND the denominator.
    assert score.agreement == 1.0


def test_a_single_repeat_reports_no_noise_floor():
    """Everything is trivially stable under one round, and 100% would be an artefact."""
    assert bakeoff.score(arm("m", GOOD), LABELS, bakeoff.HEADLINE).noise_floor is None


FLIPPED = {"m1": verdict("keep_visible"), "m2": verdict("needs_reply")}


def test_a_model_that_settled_nothing_reports_no_agreement_rather_than_zero():
    score = bakeoff.score(arm("m", GOOD, FLIPPED), LABELS, bakeoff.HEADLINE)
    assert score.stable == 0
    assert score.agreement is None


def test_an_errored_message_is_reported_apart_and_counts_against_no_rate():
    errored = {**GOOD, "m2": Verdict(None, None, error="studio_run_failed")}
    score = bakeoff.score(arm("m", errored, errored), LABELS, bakeoff.HEADLINE)
    assert score.errored == ("m2",)
    assert (score.judged, score.stable, score.correct) == (1, 1, 1)
    assert score.noise_floor == 1.0


def test_needs_reply_is_scored_apart_from_the_whole_verdict():
    """The STU-1044 shape: a bucket kept, the reply lost."""
    lost = {"m1": verdict("keep_visible"), "m2": verdict("keep_visible")}
    result = bakeoff.compare(arm("base", GOOD, GOOD), arm("cand", lost, lost), LABELS)
    assert result.of("cand", "verdict").misses[0].message_id == "m1"
    reply = result.of("cand", "needs_reply")
    assert reply.correct == 1 and reply.stable == 2
    assert [m.message_id for m in reply.misses] == ["m1"]
    # The baseline keeps both, on both axes.
    assert result.of("base", "needs_reply").agreement == 1.0


def test_the_needs_reply_axis_ignores_a_bucket_the_model_got_wrong():
    """`needs_reply` asks whether the parent gets an answer, not where the mail lands."""
    wrong_bucket = {"m1": verdict("needs_reply", bucket="B"), "m2": GOOD["m2"]}
    result = bakeoff.compare(
        arm("base", GOOD, GOOD), arm("cand", wrong_bucket, wrong_bucket), LABELS
    )
    assert result.of("cand", "verdict").agreement == 0.5
    assert result.of("cand", "needs_reply").agreement == 1.0


def test_a_candidate_that_ties_is_not_worse():
    result = bakeoff.compare(arm("base", GOOD, GOOD), arm("cand", GOOD, GOOD), LABELS)
    assert not result.candidate_is_worse


def test_a_candidate_that_loses_a_verdict_is_worse():
    lost = {**GOOD, "m1": verdict("keep_visible")}
    result = bakeoff.compare(arm("base", GOOD, GOOD), arm("cand", lost, lost), LABELS)
    assert result.candidate_is_worse
    assert "WORSE than" in bakeoff.render(result)


def test_a_candidate_that_settled_nothing_is_worse_rather_than_unrated():
    """An unmeasurable model is not a model to pin."""
    result = bakeoff.compare(arm("base", GOOD, GOOD), arm("cand", GOOD, FLIPPED), LABELS)
    assert result.of("cand", "verdict").agreement is None
    assert result.candidate_is_worse


def test_a_candidate_right_on_the_little_it_settled_does_not_outscore_the_baseline():
    """The hole a rate comparison leaves: 1/1 settled beats 2/2 on agreement.

    In production a verdict the model cannot hold is a verdict lost exactly like a wrong
    one, so both arms are scored over every golden row.
    """
    coin_flip = {"m1": GOOD["m1"], "m2": verdict("needs_reply")}
    result = bakeoff.compare(
        arm("base", GOOD, GOOD), arm("cand", GOOD, coin_flip), LABELS
    )
    cand = result.of("cand", "verdict")
    assert (cand.correct, cand.covered) == (1, 2)
    assert cand.agreement == 1.0  # the rate still says it was right when it committed
    assert result.candidate_is_worse


def test_a_candidate_that_errored_half_the_corpus_does_not_outscore_the_baseline():
    """An errored message leaves the arm's `judged` set, never the shared denominator."""
    half_dead = {"m1": GOOD["m1"], "m2": Verdict(None, None, error="studio_run_failed")}
    result = bakeoff.compare(
        arm("base", GOOD, GOOD), arm("cand", half_dead, half_dead), LABELS
    )
    assert result.of("cand", "verdict").covered == 2
    assert result.candidate_is_worse


def test_a_golden_row_outside_the_taxonomy_is_refused(tmp_path):
    """It would mark every model wrong forever on an answer no run can produce."""
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"m1": {"bucket": "C", "suggested_action": "needs_reply"}}))
    with pytest.raises(ValueError, match="outside the taxonomy"):
        bakeoff.load_golden(path, TAXONOMY)


def test_coverage_names_both_directions_of_a_stale_golden():
    uncovered, absent = bakeoff.coverage(["m1", "m3"], LABELS)
    assert (uncovered, absent) == (["m3"], ["m2"])


def test_as_dict_round_trips_through_json():
    result = bakeoff.compare(arm("base", GOOD, GOOD), arm("cand", GOOD, GOOD), LABELS)
    assert json.loads(json.dumps(bakeoff.as_dict(result)))["candidate_is_worse"] is False


def test_the_classifier_agent_takes_its_model_from_the_bakeoff_env():
    """Unset, `${CLASSIFY_MODEL}` resolves to '' and the engine falls back to
    `defaults.model` — so the override never pins production to a model."""
    agent = yaml.safe_load(
        (deployment.ROOT / ".studio" / "agents" / "classifier.agent.yaml").read_text(encoding="utf-8")
    )
    assert agent["model"] == "${CLASSIFY_MODEL}"


def test_a_pass_where_the_baseline_settled_nothing_reports_no_measurement():
    """Every arm erroring reads through `candidate_is_worse` exactly like a bad
    candidate, and that would be a verdict on a model nothing scored."""
    dead = {k: Verdict(None, None, error="studio_run_failed") for k in GOOD}
    result = bakeoff.compare(arm("base", dead, dead), arm("cand", dead, dead), LABELS)
    assert not result.measured
    assert "NO MEASUREMENT" in bakeoff.render(result)


def test_a_measured_pass_says_so():
    result = bakeoff.compare(arm("base", GOOD, GOOD), arm("cand", GOOD, GOOD), LABELS)
    assert result.measured
    assert "at least as good as" in bakeoff.render(result)
