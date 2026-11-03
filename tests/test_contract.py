from pathlib import Path

import pytest

from email_classifier.core.contract import (
    ACTIONS,
    CONFIDENCES,
    Taxonomy,
    ValidationResult,
    enum_and_consistency_errors,
    load_taxonomy,
    validate_classification,
)
from email_classifier.core.types import Classification
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")
BUCKETS = TAXONOMY.buckets
ALLOWED_ACTIONS = TAXONOMY.allowed_actions
ALLOWED_SUBTYPES = TAXONOMY.allowed_subtypes
SUBTYPES = TAXONOMY.subtypes


def test_classification_has_seven_frozen_fields():
    c = Classification(
        thread_id="t1",
        bucket="A",
        subtype="human_real",
        confidence="high",
        suggested_action="keep_visible",
        suggested_label=None,
        reasoning="real person",
    )
    assert c.bucket == "A"
    assert c.suggested_label is None


def test_table_matches_taxonomy():
    assert ALLOWED_ACTIONS["A"] == frozenset({"keep_visible", "needs_reply"})
    assert ALLOWED_ACTIONS["B"] == frozenset({"label_archive", "keep_visible"})
    assert ALLOWED_ACTIONS["C"] == frozenset({"label_archive", "mark_for_delete"})
    assert ALLOWED_ACTIONS["D"] == frozenset({"mark_for_delete", "label_archive"})
    assert set(BUCKETS) == {"A", "B", "C", "D"}
    assert set(CONFIDENCES) == {"high", "medium", "low"}
    assert "escalate_to_llm" in ACTIONS


def _valid_payload(bucket="A", action="keep_visible", subtype=None) -> dict:
    return {
        "thread_id": "t1",
        "bucket": bucket,
        "subtype": subtype or sorted(ALLOWED_SUBTYPES[bucket])[0],
        "confidence": "high",
        "suggested_action": action,
        "suggested_label": None,
        "reasoning": "because",
    }


def test_valid_payload_per_bucket_passes():
    for bucket, actions in ALLOWED_ACTIONS.items():
        for action in actions:
            res = validate_classification(_valid_payload(bucket, action), TAXONOMY)
            assert res.valid is True, (bucket, action, res.errors)
            assert res.errors == []


def test_escalate_to_llm_valid_for_every_bucket():
    for bucket in BUCKETS:
        res = validate_classification(_valid_payload(bucket, "escalate_to_llm"), TAXONOMY)
        assert res.valid is True, (bucket, res.errors)


@pytest.mark.parametrize(
    "bucket,action",
    [
        (b, a)
        for b in BUCKETS
        for a in ACTIONS
        if a != "escalate_to_llm" and a not in ALLOWED_ACTIONS[b]
    ],
)
def test_forbidden_pair_is_invalid(bucket, action):
    res = validate_classification(_valid_payload(bucket, action), TAXONOMY)
    assert res.valid is False
    assert any("not allowed for bucket" in e for e in res.errors)


def test_bad_confidence_enum_is_invalid():
    payload = _valid_payload()
    payload["confidence"] = "0.9"
    res = validate_classification(payload, TAXONOMY)
    assert res.valid is False
    assert any("confidence" in e for e in res.errors)


def test_missing_field_is_invalid():
    payload = _valid_payload()
    del payload["reasoning"]
    res = validate_classification(payload, TAXONOMY)
    assert res.valid is False
    assert any("reasoning" in e for e in res.errors)


def test_wrong_type_is_invalid():
    payload = _valid_payload()
    payload["suggested_label"] = 123  # not str | null
    res = validate_classification(payload, TAXONOMY)
    assert res.valid is False
    assert any("suggested_label" in e for e in res.errors)


def test_unexpected_field_is_invalid():
    payload = _valid_payload()
    payload["provider_category"] = "promotions"  # input-only, never in output contract
    res = validate_classification(payload, TAXONOMY)
    assert res.valid is False
    assert any("unexpected field" in e for e in res.errors)


def test_result_is_binary_and_collects_all_errors():
    res = validate_classification({}, TAXONOMY)  # everything missing
    assert isinstance(res, ValidationResult)
    assert res.valid is False
    assert len(res.errors) >= 6  # all required fields reported, not fail-fast


def test_cli_helper_matches_validator_on_enums():
    # forbidden -> errors
    assert enum_and_consistency_errors("A", "high", "label_archive", TAXONOMY)
    assert enum_and_consistency_errors("A", "high", "keep_visible", TAXONOMY) == []


def test_subtype_outside_the_enum_is_invalid():
    for unlisted in ("sécurité/compte", "securite/compte", "security/account", "facture"):
        res = validate_classification(_valid_payload(subtype=unlisted), TAXONOMY)
        assert res.valid is False, unlisted
        assert any(f"subtype '{unlisted}' not in" in e for e in res.errors)


def test_subtype_from_another_bucket_is_invalid():
    foreign = sorted(ALLOWED_SUBTYPES["C"] - ALLOWED_SUBTYPES["A"])[0]
    res = validate_classification(_valid_payload(bucket="A", subtype=foreign), TAXONOMY)
    assert res.valid is False
    assert any(f"subtype '{foreign}' not allowed for bucket 'A'" in e for e in res.errors)


def test_every_listed_subtype_passes_for_its_bucket():
    for bucket, subtypes in ALLOWED_SUBTYPES.items():
        for subtype in subtypes:
            action = sorted(ALLOWED_ACTIONS[bucket])[0]
            res = validate_classification(_valid_payload(bucket, action, subtype), TAXONOMY)
            assert res.valid is True, (bucket, subtype, res.errors)


def test_subtype_vocabulary_is_ascii_and_partitioned():
    assert len(SUBTYPES) == sum(len(s) for s in ALLOWED_SUBTYPES.values())  # no bucket overlap
    for subtype in SUBTYPES:
        assert subtype.isascii() and subtype == subtype.lower()


def test_deterministic_verdict_carries_no_subtype():
    assert enum_and_consistency_errors("A", "high", "keep_visible", TAXONOMY, None) == []


def test_load_taxonomy_refuses_a_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="no taxonomy at"):
        load_taxonomy(tmp_path / "no-such-taxonomy.yaml")


def test_load_taxonomy_refuses_a_clamp_outside_its_own_actions(tmp_path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        "buckets:\n"
        "  - name: A\n"
        "    allowed_actions: [keep_visible]\n"
        "    clamp: needs_reply\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not in its own allowed_actions"):
        load_taxonomy(path)


def test_load_taxonomy_refuses_an_action_outside_the_kernel_vocabulary(tmp_path):
    path = tmp_path / "taxonomy.yaml"
    path.write_text(
        "buckets:\n"
        "  - name: A\n"
        "    allowed_actions: [invent_a_new_action]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside the kernel action vocabulary"):
        load_taxonomy(path)


def test_taxonomy_is_a_dataclass_never_a_kernel_literal():
    assert isinstance(TAXONOMY, Taxonomy)
