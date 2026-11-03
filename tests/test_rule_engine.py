from datetime import timedelta

import pytest
import yaml
from pathlib import Path

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.stage1.rule_engine import Rule, load_rules, apply


def _make_email(
    from_address: str = "sender@example.com",
    subject: str = "Test",
    headers: dict | None = None,
    thread_id: str = "t001",
) -> EmailInput:
    return EmailInput(
        message_id=thread_id,
        thread_id=thread_id,
        from_address=from_address,
        subject=subject,
        headers=headers or {},
        body_snippet="Test body",
        date="2026-06-10T14:30:00+00:00",
    )


def _rules(*rules: Rule) -> list[Rule]:
    """Helper: always appends a catch-all at the end."""
    return list(rules) + [
        Rule(id="catch_all", conditions={}, bucket=None, confidence="low", action="escalate_to_llm")
    ]


def test_first_match_wins():
    rules = _rules(
        Rule(id="rule_a", conditions={"from_domain": "example.com"}, bucket="A", confidence="high", action="keep_visible"),
        Rule(id="rule_b", conditions={"from_domain": "example.com"}, bucket="C", confidence="low", action="label_archive"),
    )
    match = apply(_make_email(from_address="sender@example.com"), rules)
    assert match.rule_id == "rule_a"
    assert match.action == "keep_visible"


def test_catch_all_fires_when_no_specific_match():
    rules = _rules(
        Rule(id="rule_a", conditions={"from_domain": "specific.com"}, bucket="A", confidence="high", action="keep_visible"),
    )
    match = apply(_make_email(from_address="sender@other.com"), rules)
    assert match.rule_id == "catch_all"
    assert match.confidence == "low"
    assert match.action == "escalate_to_llm"
    assert match.bucket is None


def test_from_domain_extracted_from_address():
    rules = _rules(
        Rule(id="interac", conditions={"from_domain": "payments.interac.ca"}, bucket="A", confidence="high", action="keep_visible"),
    )
    match = apply(_make_email(from_address="notify@payments.interac.ca"), rules)
    assert match.rule_id == "interac"


def test_from_address_exact_match():
    rules = _rules(
        Rule(id="job_alert", conditions={"from_address": "jobalerts-noreply@linkedin.com"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(from_address="jobalerts-noreply@linkedin.com"), rules)
    assert match.rule_id == "job_alert"


def test_from_address_exact_no_partial_match():
    rules = _rules(
        Rule(id="job_alert", conditions={"from_address": "jobalerts-noreply@linkedin.com"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(from_address="inmail-hit-reply@linkedin.com"), rules)
    assert match.rule_id == "catch_all"


def test_subject_contains_case_insensitive():
    rules = _rules(
        Rule(id="virement", conditions={"subject_contains": "virement"}, bucket="A", confidence="high", action="keep_visible"),
    )
    match = apply(_make_email(subject="VIREMENT reçu de Desjardins"), rules)
    assert match.rule_id == "virement"


def test_subject_contains_list_matches_any_variant():
    rules = _rules(
        Rule(id="indexing", conditions={"subject_contains": ["from being indexed", "raisons empêchent"]},
             bucket="B", confidence="high", action="keep_visible"),
    )
    assert apply(_make_email(subject="New reasons prevent pages from being indexed"), rules).rule_id == "indexing"
    assert apply(_make_email(subject="De nouvelles raisons empêchent l'indexation de pages"), rules).rule_id == "indexing"
    assert apply(_make_email(subject="Indexation des pages issues successfully fixed"), rules).rule_id == "catch_all"


def test_header_present():
    rules = _rules(
        Rule(id="newsletter", conditions={"header_present": "List-Unsubscribe"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(headers={"List-Unsubscribe": "<https://example.com>"}), rules)
    assert match.rule_id == "newsletter"


def test_header_present_no_match_when_absent():
    rules = _rules(
        Rule(id="newsletter", conditions={"header_present": "List-Unsubscribe"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(headers={}), rules)
    assert match.rule_id == "catch_all"


def test_multiple_conditions_and_logic():
    rules = _rules(
        Rule(
            id="desjardins_virement",
            conditions={"from_domain": "desjardins.com", "subject_contains": "virement"},
            bucket="A",
            confidence="high",
            action="keep_visible",
        ),
    )
    # Both conditions match
    match = apply(_make_email(from_address="noreply@desjardins.com", subject="Virement reçu"), rules)
    assert match.rule_id == "desjardins_virement"

    # Only from_domain matches → no match
    match2 = apply(_make_email(from_address="noreply@desjardins.com", subject="Relevé mensuel"), rules)
    assert match2.rule_id == "catch_all"


def test_load_rules_from_yaml(tmp_path: Path):
    yaml_content = """
rules:
  - id: test_rule
    conditions:
      from_domain: example.com
    bucket: A
    confidence: high
    action: keep_visible
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    rules = load_rules(rules_file)
    assert len(rules) == 2
    assert rules[0].id == "test_rule"
    assert rules[0].conditions == {"from_domain": "example.com"}
    assert rules[1].id == "catch_all"
    assert rules[1].conditions == {}


def test_load_rules_raises_if_no_catch_all(tmp_path: Path):
    yaml_content = """
rules:
  - id: test_rule
    conditions:
      from_domain: example.com
    bucket: A
    confidence: high
    action: keep_visible
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="catch-all"):
        load_rules(rules_file)


def test_load_rules_raises_on_unknown_condition_key(tmp_path: Path):
    yaml_content = """
rules:
  - id: typo_rule
    conditions:
      from_domian: example.com
    bucket: A
    confidence: high
    action: keep_visible
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="unknown condition key"):
        load_rules(rules_file)


def test_load_rules_accepts_all_known_condition_keys(tmp_path: Path):
    yaml_content = """
rules:
  - id: full_rule
    conditions:
      from_address: sender@example.com
      from_domain: example.com
      subject_contains: hello
      header_present: List-Unsubscribe
    bucket: A
    confidence: high
    action: keep_visible
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    rules = load_rules(rules_file)
    assert len(rules) == 2


def test_header_value_matches_substring():
    rules = _rules(
        Rule(id="oneclick", conditions={"header_value": {"List-Unsubscribe-Post": "One-Click"}},
             bucket="C", confidence="high", action="label_archive"),
    )
    email = _make_email(headers={"List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    assert apply(email, rules).rule_id == "oneclick"


def test_header_value_no_match_when_header_absent():
    rules = _rules(
        Rule(id="oneclick", conditions={"header_value": {"List-Unsubscribe-Post": "One-Click"}},
             bucket="C", confidence="high", action="label_archive"),
    )
    assert apply(_make_email(headers={}), rules).rule_id == "catch_all"


def test_header_value_no_match_when_value_differs():
    rules = _rules(
        Rule(id="oneclick", conditions={"header_value": {"List-Unsubscribe-Post": "One-Click"}},
             bucket="C", confidence="high", action="label_archive"),
    )
    email = _make_email(headers={"List-Unsubscribe-Post": "List-Unsubscribe=Mailto"})
    assert apply(email, rules).rule_id == "catch_all"


def test_domain_matches_suffix_boundary():
    from email_classifier.stage1.rule_engine import _domain_matches_suffix
    assert _domain_matches_suffix("e.godaddy.com", "godaddy.com") is True
    assert _domain_matches_suffix("godaddy.com", "godaddy.com") is True
    assert _domain_matches_suffix("notgodaddy.com", "godaddy.com") is False
    assert _domain_matches_suffix("godaddy.com.evil.com", "godaddy.com") is False


def test_from_domain_suffix_list_matches():
    rules = _rules(
        Rule(id="bulk", conditions={"from_domain_suffix": ["kmail-lists.com", "customer.io"]},
             bucket="C", confidence="high", action="label_archive"),
    )
    assert apply(_make_email(from_address="x@email.kmail-lists.com"), rules).rule_id == "bulk"
    assert apply(_make_email(from_address="x@e.customer.io"), rules).rule_id == "bulk"
    assert apply(_make_email(from_address="x@other.com"), rules).rule_id == "catch_all"


def test_from_domain_suffix_string_value():
    rules = _rules(
        Rule(id="bulk", conditions={"from_domain_suffix": "kmail-lists.com"},
             bucket="C", confidence="high", action="label_archive"),
    )
    assert apply(_make_email(from_address="x@kmail-lists.com"), rules).rule_id == "bulk"


def test_guard_condition_keeps_email_visible_when_tripped():
    rules = _rules(
        Rule(id="uni", conditions={"header_present": "List-Unsubscribe", "guard": "not_transactional"},
             bucket="C", confidence="high", action="label_archive"),
    )
    # guard trips on "invoice" → falls through to catch_all
    tripped = _make_email(subject="Your invoice is ready", headers={"List-Unsubscribe": "<x>"})
    assert apply(tripped, rules).rule_id == "catch_all"
    # guard satisfied → rule matches
    clean = _make_email(subject="Weekly newsletter", headers={"List-Unsubscribe": "<x>"})
    assert apply(clean, rules).rule_id == "uni"


def test_load_rules_raises_on_malformed_header_value(tmp_path: Path):
    yaml_content = """
rules:
  - id: bad_header_value
    conditions:
      header_value: "One-Click"
    bucket: C
    confidence: high
    action: label_archive
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="header_value"):
        load_rules(rules_file)


def test_load_rules_raises_on_malformed_from_domain_suffix(tmp_path: Path):
    yaml_content = """
rules:
  - id: bad_suffix
    conditions:
      from_domain_suffix:
        - 1
        - 2
    bucket: C
    confidence: high
    action: label_archive
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="from_domain_suffix"):
        load_rules(rules_file)


def test_load_rules_accepts_valid_from_domain_suffix_forms(tmp_path: Path):
    yaml_content = """
rules:
  - id: string_suffix
    conditions:
      from_domain_suffix: example.com
    bucket: C
    confidence: high
    action: label_archive
  - id: list_suffix
    conditions:
      from_domain_suffix:
        - foo.com
        - bar.com
    bucket: C
    confidence: high
    action: label_archive
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    rules = load_rules(rules_file)
    assert len(rules) == 3


def test_load_rules_raises_on_malformed_subject_contains(tmp_path: Path):
    yaml_content = """
rules:
  - id: bad_subject
    conditions:
      subject_contains:
        - 1
    bucket: B
    confidence: high
    action: keep_visible
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="subject_contains"):
        load_rules(rules_file)


def test_load_rules_raises_on_unknown_guard(tmp_path: Path):
    yaml_content = """
rules:
  - id: bad
    conditions:
      guard: not_a_real_guard
    bucket: C
    confidence: high
    action: label_archive
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="unknown guard"):
        load_rules(rules_file)


# --- expires_after (STU-2070) --------------------------------------------------


def test_expires_after_counts_from_the_message_date():
    rules = _rules(
        Rule(id="code", conditions={"from_domain": "example.com"}, bucket="A",
             confidence="high", action="keep_visible", expires_after=timedelta(hours=1)),
    )
    match = apply(_make_email(), rules)
    assert match.expires_at == "2026-06-10T15:30:00+00:00"


def test_a_rule_without_expires_after_never_expires():
    match = apply(_make_email(), _rules())
    assert match.expires_at == ""


@pytest.mark.parametrize("value,expected", [
    ("30m", timedelta(minutes=30)), ("1h", timedelta(hours=1)), ("2d", timedelta(days=2)),
])
def test_load_rules_reads_expires_after(tmp_path: Path, value, expected):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml.dump({"rules": [
        {"id": "code", "conditions": {"from_domain": "example.com"}, "bucket": "A",
         "confidence": "high", "action": "keep_visible", "expires_after": value},
        {"id": "catch_all", "conditions": {}, "bucket": None, "confidence": "low",
         "action": "escalate_to_llm"},
    ]}))
    assert load_rules(rules_file)[0].expires_after == expected


@pytest.mark.parametrize("value", ["2 days", "1w", "h", 5, "-1h"])
def test_load_rules_raises_on_malformed_expires_after(tmp_path: Path, value):
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml.dump({"rules": [
        {"id": "code", "conditions": {"from_domain": "example.com"}, "bucket": "A",
         "confidence": "high", "action": "keep_visible", "expires_after": value},
        {"id": "catch_all", "conditions": {}, "bucket": None, "confidence": "low",
         "action": "escalate_to_llm"},
    ]}))
    with pytest.raises(ValueError, match="expires_after"):
        load_rules(rules_file)
