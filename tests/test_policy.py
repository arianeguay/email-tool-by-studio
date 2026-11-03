import pytest
from pathlib import Path

from email_classifier.core.types import RuleMatch
from email_classifier import deployment
from email_classifier.stage1.policy import Policy, load_policy, filter_action


def _match(action: str, bucket: str | None = "D") -> RuleMatch:
    return RuleMatch(rule_id="test_rule", action=action, action_original=None, confidence="high", bucket=bucket)


def test_destructive_action_blocked_when_not_allowed():
    policy = Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=[],
        frontier_log_file=True,
        frontier_log_stdout=False,
    )
    result = filter_action(_match("mark_for_delete"), policy)
    assert result.action == "label_archive"
    assert result.action_original == "mark_for_delete"
    assert result.rule_id == "test_rule"  # other fields preserved
    assert result.bucket == "D"


def test_destructive_action_allowed_when_permitted():
    policy = Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=["mark_for_delete"],
        frontier_log_file=True,
        frontier_log_stdout=False,
    )
    result = filter_action(_match("mark_for_delete"), policy)
    assert result.action == "mark_for_delete"
    assert result.action_original is None


def test_non_destructive_action_never_modified():
    policy = Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=[],
        frontier_log_file=True,
        frontier_log_stdout=False,
    )
    for action in ("keep_visible", "label_archive", "needs_reply", "escalate_to_llm"):
        result = filter_action(_match(action, bucket="A"), policy)
        assert result.action == action
        assert result.action_original is None


def test_load_policy_from_yaml(tmp_path: Path):
    yaml_content = """
deterministic_can_finalize: true
destructive_actions_allowed:
  - mark_for_delete
frontier_log:
  file: true
  stdout: false
"""
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml_content)
    policy = load_policy(policy_file)
    assert policy.deterministic_can_finalize is True
    assert policy.destructive_actions_allowed == ["mark_for_delete"]
    assert policy.frontier_log_file is True
    assert policy.frontier_log_stdout is False


def test_load_policy_empty_destructive_list(tmp_path: Path):
    yaml_content = """
deterministic_can_finalize: true
destructive_actions_allowed: []
frontier_log:
  file: true
  stdout: false
"""
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml_content)
    policy = load_policy(policy_file)
    assert policy.destructive_actions_allowed == []
    result = filter_action(_match("mark_for_delete"), policy)
    assert result.action == "label_archive"


def test_load_policy_reads_email_provider(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\nemail:\n  provider: outlook\n")
    assert load_policy(policy_file).email_provider == "outlook"


def test_load_policy_defaults_email_provider_to_gmail(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\n")
    assert load_policy(policy_file).email_provider == "gmail"


def test_load_policy_defaults_timezone_to_america_toronto(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\n")
    assert load_policy(policy_file).timezone == "America/Toronto"


def test_load_policy_reads_timezone(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\ntimezone: Europe/Paris\n")
    assert load_policy(policy_file).timezone == "Europe/Paris"


def test_load_policy_rejects_an_unresolvable_timezone(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\ntimezone: Not/A_Zone\n")
    with pytest.raises(ValueError, match="timezone"):
        load_policy(policy_file)


def test_load_policy_maps_scope_to_fields(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\nanonymization:\n  scope: full\n")
    assert load_policy(policy_file).anonymization_fields == [
        "from_address", "subject", "headers", "body_snippet", "body_text",
        "memory_index",
    ]


def test_load_policy_body_only_scope_leaves_the_sender_in_the_clear(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\nanonymization:\n  scope: body_only\n")
    # `memory_index` is in both scopes: a club fact is third-party either way (STU-879).
    assert load_policy(policy_file).anonymization_fields == [
        "body_snippet", "body_text", "memory_index",
    ]


def test_load_policy_without_anonymization_block_disables_it(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\n")
    assert load_policy(policy_file).anonymization_fields == []


def test_load_policy_rejects_unknown_scope(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\nanonymization:\n  scope: partial\n")
    with pytest.raises(ValueError, match="unknown anonymization.scope"):
        load_policy(policy_file)


def test_shipped_policy_scope_is_valid():
    """The scope in rules/policy.yaml must stay one the app knows how to map."""
    policy = load_policy(deployment.ROOT / "rules" / "policy.yaml")
    assert policy.anonymization_fields


def test_a_non_boolean_auto_apply_fails_loud(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text(
        "deterministic_can_finalize: true\nauto_apply: 'false'\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="auto_apply must be true or false"):
        load_policy(path)


def test_load_policy_defaults_language_to_fr_ca(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\n")
    assert load_policy(policy_file).language == "fr_ca"


def test_load_policy_reads_language(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\nlanguage: en_us\n")
    assert load_policy(policy_file).language == "en_us"


@pytest.mark.parametrize("value", ["../secrets", "fr ca", "''", "3"])
def test_load_policy_rejects_a_language_that_is_not_a_pack_code(tmp_path: Path, value: str):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(f"deterministic_can_finalize: true\nlanguage: {value}\n")
    with pytest.raises(ValueError, match="language must name a lang pack"):
        load_policy(policy_file)


def test_load_policy_vouches_no_skill_by_default(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\n")
    assert load_policy(policy_file).vouched_skill is None


def test_load_policy_reads_vouched_skill(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\nvouched_skill: org-facts\n")
    assert load_policy(policy_file).vouched_skill == "org-facts"


@pytest.mark.parametrize("value", ["../secrets", "org facts", "''", "3"])
def test_load_policy_rejects_a_vouched_skill_that_is_not_a_name(tmp_path: Path, value: str):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(f"deterministic_can_finalize: true\nvouched_skill: {value}\n")
    with pytest.raises(ValueError, match="vouched_skill must name a skill"):
        load_policy(policy_file)


def test_load_policy_reads_envelope_rewriters(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(
        "deterministic_can_finalize: true\nenvelope_rewriters:\n  - org.relay_form\n"
    )
    assert load_policy(policy_file).envelope_rewriters == ["org.relay_form"]
    policy_file.write_text("deterministic_can_finalize: true\n")
    assert load_policy(policy_file).envelope_rewriters == []


@pytest.mark.parametrize("value", ["org.relay_form", "[../x]", "[org relay]", "[3]"])
def test_load_policy_rejects_envelope_rewriters_that_are_not_module_names(
    tmp_path: Path, value: str
):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(f"deterministic_can_finalize: true\nenvelope_rewriters: {value}\n")
    with pytest.raises(ValueError, match="envelope_rewriters must list module names"):
        load_policy(policy_file)


def test_auto_unsubscribe_is_off_by_default_and_read_for_imap(tmp_path: Path):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("deterministic_can_finalize: true\n")
    assert load_policy(policy_file).auto_unsubscribe is False
    policy_file.write_text(
        "deterministic_can_finalize: true\nemail:\n  provider: imap\nauto_unsubscribe: true\n"
    )
    assert load_policy(policy_file).auto_unsubscribe is True


@pytest.mark.parametrize("body, error", [
    ("email:\n  provider: imap\nauto_unsubscribe: 'true'\n", "must be true or false"),
    ("email:\n  provider: outlook\nauto_unsubscribe: true\n", "needs email.provider imap"),
])
def test_load_policy_rejects_an_unusable_auto_unsubscribe(tmp_path: Path, body: str, error: str):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(f"deterministic_can_finalize: true\n{body}")
    with pytest.raises(ValueError, match=error):
        load_policy(policy_file)
