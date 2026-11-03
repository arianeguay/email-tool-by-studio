from pathlib import Path

import pytest
import yaml

from email_classifier.anonymize import Tokenizer
from email_classifier.core.types import Classification, EmailInput
from email_classifier.learn.promote import (
    evaluate,
    load_blacklist,
    load_learned,
    render,
    rule_id,
    save_blacklist,
)
from email_classifier.learn.store import Observation, VerdictStore, load_observations
from email_classifier.orchestrate.pipeline import run
from email_classifier.orchestrate.stage2_client import Stage2Result
from email_classifier.stage1.frontier_log import FrontierLogger
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import Rule, apply, load_rules
from tests.conftest import RULES_DIR

NOW = "2026-08-18T00:00:00+00:00"


def _obs(sender, run_id, message_id, bucket="C", action="label_archive", confidence="high",
         thread_id=None):
    return Observation(
        run_id=run_id, timestamp=NOW, message_id=message_id,
        thread_id=thread_id or message_id, from_address=sender,
        bucket=bucket, suggested_action=action, confidence=confidence,
    )


def _uniform(sender="centris@mlsmatrix.com", **overrides):
    return [
        _obs(sender, "run-1", "m1", **overrides),
        _obs(sender, "run-1", "m2", **overrides),
        _obs(sender, "run-2", "m3", **overrides),
    ]


def _decide(observations, hand_written=None, blacklist=None, previously_promoted=None):
    return evaluate(
        observations, hand_written or [], blacklist or set(), NOW, previously_promoted
    )[0]


def test_uniform_sender_is_promoted():
    decision = _decide(_uniform())
    assert decision.rule is not None
    assert decision.rule.bucket == "C"
    assert decision.rule.action == "label_archive"
    assert decision.rule.sample_size == 3
    assert decision.rule.run_ids == ["run-1", "run-2"]
    assert decision.rule.promoted_at == NOW


def test_split_verdict_is_refused():
    """STU-818's alert@uptimerobot.com: uniform on bucket, split on action."""
    sender = "alert@uptimerobot.com"
    observations = [
        _obs(sender, "run-1", "m1", bucket="B", action="keep_visible"),
        _obs(sender, "run-1", "m2", bucket="B", action="label_archive"),
        _obs(sender, "run-2", "m3", bucket="B", action="keep_visible"),
    ]
    decision = _decide(observations)
    assert decision.rule is None
    assert "disagree" in decision.reason


def test_single_run_is_refused():
    observations = [_obs("a@b.com", "run-1", f"m{i}") for i in range(3)]
    decision = _decide(observations)
    assert decision.rule is None
    assert "distinct run" in decision.reason


def test_repeated_message_does_not_inflate_the_sample():
    sender = "a@b.com"
    observations = [
        _obs(sender, "run-1", "m1"), _obs(sender, "run-2", "m1"), _obs(sender, "run-3", "m1"),
    ]
    decision = _decide(observations)
    assert decision.rule is None
    assert "distinct conversation" in decision.reason


def test_one_conversation_is_one_judgement_however_many_messages_it_has():
    """A thread is classified once (STU-944), so its messages cannot corroborate each
    other into a promotion the LLM was asked about a single time."""
    sender = "a@b.com"
    observations = [
        _obs(sender, "run-1", "m1", thread_id="t1"),
        _obs(sender, "run-1", "m2", thread_id="t1"),
        _obs(sender, "run-2", "m3", thread_id="t1"),
    ]
    decision = _decide(observations)
    assert decision.rule is None
    assert decision.reason == "1 distinct conversation(s) < 3"


def test_non_high_confidence_is_refused():
    observations = _uniform()
    observations[1] = _obs("centris@mlsmatrix.com", "run-1", "m2", confidence="medium")
    decision = _decide(observations)
    assert decision.rule is None
    assert "confidence: high" in decision.reason


def test_destructive_action_is_never_promoted():
    decision = _decide(_uniform(action="mark_for_delete"))
    assert decision.rule is None
    assert "mark_for_delete" in decision.reason


def test_escalate_is_never_promoted():
    decision = _decide(_uniform(action="escalate_to_llm"))
    assert decision.rule is None


def test_blacklisted_sender_is_refused():
    decision = _decide(_uniform(), blacklist={"centris@mlsmatrix.com"})
    assert decision.rule is None
    assert decision.reason == "blacklisted"


def test_sender_already_covered_by_a_hand_written_rule_is_refused():
    hand_written = [
        Rule(id="merchant_mlsmatrix", conditions={"from_domain": "mlsmatrix.com"},
             bucket="C", confidence="high", action="label_archive"),
        Rule(id="catch_all", conditions={}, bucket=None, confidence="low",
             action="escalate_to_llm"),
    ]
    decision = _decide(_uniform(), hand_written=hand_written)
    assert decision.rule is None
    assert "merchant_mlsmatrix" in decision.reason


def test_promoted_at_is_preserved_across_reruns():
    previous = {rule_id("centris@mlsmatrix.com"): "2026-01-01T00:00:00+00:00"}
    decision = _decide(_uniform(), previously_promoted=previous)
    assert decision.rule.promoted_at == "2026-01-01T00:00:00+00:00"


def test_render_round_trips_through_load_learned(tmp_path: Path):
    rule = _decide(_uniform()).rule
    path = tmp_path / "learned.yaml"
    path.write_text(render([rule]), encoding="utf-8")
    assert load_learned(path) == [rule]

    emitted = yaml.safe_load(path.read_text(encoding="utf-8"))["rules"][0]
    assert emitted["id"] == "learned_centris_mlsmatrix_com"
    assert emitted["confidence"] == "medium"
    assert emitted["provenance"]["run_ids"] == ["run-1", "run-2"]


def test_blacklist_round_trips(tmp_path: Path):
    path = tmp_path / "blacklist.yaml"
    save_blacklist(path, {"A@B.com".lower(), "c@d.com"})
    assert load_blacklist(path) == {"a@b.com", "c@d.com"}


def test_missing_files_load_as_empty(tmp_path: Path):
    assert load_learned(tmp_path / "nope.yaml") == []
    assert load_blacklist(tmp_path / "nope.yaml") == set()
    assert load_observations(tmp_path / "nope.jsonl") == []


# ── Layering ──────────────────────────────────────────────────────────────────


def _write_learned(path: Path, sender: str, action: str = "label_archive") -> None:
    rule = _decide(_uniform(sender, action=action)).rule
    path.write_text(render([rule]), encoding="utf-8")


def _email(sender: str, message_id: str = "m") -> EmailInput:
    return EmailInput(
        message_id=message_id, thread_id="t", from_address=sender, subject="S",
        headers={}, body_snippet="b", date="2026-06-10T00:00:00+00:00",
    )


def test_learned_layer_sits_between_hand_written_rules_and_the_catch_all(tmp_path: Path):
    learned = tmp_path / "learned.yaml"
    _write_learned(learned, "learned@example.com")
    rules = load_rules(RULES_DIR / "rules.yaml", learned)

    assert rules[-1].id == "catch_all"
    assert rules[-2].id == "learned_learned_example_com"
    assert apply(_email("learned@example.com"), rules).rule_id == "learned_learned_example_com"


def test_hand_written_rule_beats_a_learned_one(tmp_path: Path):
    learned = tmp_path / "learned.yaml"
    hand_written_sender = "no-reply@kmail-lists.com"
    _write_learned(learned, hand_written_sender, action="keep_visible")
    hand_written = tmp_path / "rules.yaml"
    hand_written.write_text(yaml.safe_dump({"rules": [
        {"id": "hand_written_sender", "conditions": {"from_address": hand_written_sender},
         "bucket": "C", "confidence": "high", "action": "label_archive"},
        {"id": "catch_all", "conditions": {}, "bucket": None,
         "confidence": "low", "action": "escalate_to_llm"},
    ]}), encoding="utf-8")
    rules = load_rules(hand_written, learned)

    match = apply(_email(hand_written_sender), rules)
    assert match.rule_id == "hand_written_sender"


def test_missing_learned_file_is_not_an_error(tmp_path: Path):
    rules = load_rules(RULES_DIR / "rules.yaml", tmp_path / "absent.yaml")
    assert rules == load_rules(RULES_DIR / "rules.yaml")


def test_learned_rule_without_conditions_is_rejected(tmp_path: Path):
    learned = tmp_path / "learned.yaml"
    learned.write_text(
        yaml.safe_dump({"rules": [
            {"id": "learned_bad", "conditions": {}, "bucket": "C",
             "confidence": "medium", "action": "label_archive"}
        ]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="shadow the catch-all"):
        load_rules(RULES_DIR / "rules.yaml", learned)


# ── Store ─────────────────────────────────────────────────────────────────────


class _Classifier:
    def __init__(self, result):
        self.result = result

    def classify(self, email, thread_history=""):
        return self.result


def _classification(email, confidence="high"):
    return Stage2Result(classification=Classification(
        thread_id=email.thread_id, bucket="C", subtype="product_newsletter",
        confidence=confidence, suggested_action="label_archive",
        suggested_label=None, reasoning="r",
    ))


def test_pipeline_records_escalated_verdicts_with_the_sender(tmp_path: Path):
    email = _email("unknown@example.com")
    store = VerdictStore(tmp_path / "verdicts.jsonl")
    logger = FrontierLogger(tmp_path / "frontier.jsonl")
    run(
        [email],
        load_rules(RULES_DIR / "rules.yaml"),
        load_policy(RULES_DIR / "policy.yaml"),
        _Classifier(_classification(email)),
        logger,
        None,
        store,
        # The shipped policy sets `scope: full`, so a run tokenizes what it sends
        # (STU-1009); the sender asserted below proves the store still keeps the
        # cleartext one. Format regexes alone keep this off the `ner` extra.
        Tokenizer(persons=False),
    )

    observations = load_observations(store.path)
    assert len(observations) == 1
    assert observations[0].from_address == "unknown@example.com"
    assert observations[0].run_id == logger.run_id
    assert observations[0].bucket == "C"
    assert observations[0].suggested_action == "label_archive"
    assert observations[0].confidence == "high"


def test_pipeline_records_nothing_for_deterministic_or_failed_verdicts(tmp_path: Path):
    store = VerdictStore(tmp_path / "verdicts.jsonl")
    deterministic = _email("no-reply@kmail-lists.com", "m1")
    escalated = _email("unknown@example.com", "m2")
    run(
        [deterministic, escalated],
        load_rules(RULES_DIR / "rules.yaml"),
        load_policy(RULES_DIR / "policy.yaml"),
        _Classifier(Stage2Result(error="studio_run_failed")),
        FrontierLogger(tmp_path / "frontier.jsonl"),
        None,
        store,
        Tokenizer(persons=False),
    )
    assert load_observations(store.path) == []


def test_store_lowercases_the_sender(tmp_path: Path):
    store = VerdictStore(tmp_path / "verdicts.jsonl")
    store.record("run-1", "m1", "t1", "Centris@MLSmatrix.COM", "C", "label_archive", "high")
    assert load_observations(store.path)[0].from_address == "centris@mlsmatrix.com"
