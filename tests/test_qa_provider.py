"""Which provider a QA measurement ran on (STU-1175)."""


import yaml

from email_classifier import (coverage_read, qa_coverage, qa_coverage_gate,
                              qa_erosion, qa_gate, qa_live_sources, qa_provider)
from email_classifier import deployment

STUDIO = deployment.ROOT / ".studio"


def _studio(tmp_path, default, agent_provider=None):
    studio = tmp_path / ".studio"
    (studio / "agents").mkdir(parents=True)
    (studio / "config.yaml").write_text(
        yaml.safe_dump({"defaults": {"provider": default}}), encoding="utf-8")
    agent = {"name": "draft-reviewer"} | (
        {"provider": agent_provider} if agent_provider else {})
    (studio / "agents" / "draft-reviewer.agent.yaml").write_text(
        yaml.safe_dump(agent), encoding="utf-8")
    return tmp_path


def test_a_flag_that_was_passed_is_what_ran(tmp_path):
    root = _studio(tmp_path, "claude-code")
    assert qa_provider.resolve(root, qa_provider.REVIEWER, "anthropic") == "anthropic"


def test_no_flag_falls_through_to_the_config_default(tmp_path):
    """The reviewer trio passes none, so this is the branch every `make qa-gate` takes —
    and `defaults.provider` is a value any host or any later commit can move, which is
    the whole reason the run has to write down what it resolved to."""
    root = _studio(tmp_path, "claude-code")
    assert qa_provider.resolve(root, qa_provider.REVIEWER) == "claude-code"


def test_an_agent_naming_its_own_provider_beats_the_default(tmp_path):
    """Kept because the middle term is the point (STU-1133 removed the classifier's pin,
    and collapsing the term while it happens to be empty is how the next one goes
    unrecorded)."""
    root = _studio(tmp_path, "claude-code", agent_provider="anthropic")
    assert qa_provider.resolve(root, qa_provider.REVIEWER) == "anthropic"


def test_an_empty_flag_is_not_an_answer(tmp_path):
    """`--provider ""` is documented as "ask for the config default", so it is the
    absence of the first term rather than a provider named the empty string."""
    root = _studio(tmp_path, "claude-code")
    assert qa_provider.resolve(root, qa_provider.REVIEWER, "") == "claude-code"


def test_a_tree_that_cannot_answer_records_nothing_rather_than_guessing(tmp_path):
    """STU-912's direction: a measurement nobody could take must not read as one that
    came back empty."""
    assert qa_provider.resolve(tmp_path / "nowhere", qa_provider.REVIEWER) is None


def test_the_agents_named_are_the_ones_the_pipelines_run():
    """A name that drifted would resolve some other agent's pin and record it silently."""
    assert (STUDIO / "agents" / f"{qa_provider.REVIEWER}.agent.yaml").exists()
    assert (STUDIO / "agents" / f"{qa_provider.READER}.agent.yaml").exists()


def test_this_tree_resolves_both_agents():
    """Not an assertion about which provider: that is the host's, and it moves. Only
    that the resolution answers at all, where a null would be an unrecorded run."""
    assert qa_provider.resolve(deployment.ROOT, qa_provider.REVIEWER)
    assert qa_provider.resolve(deployment.ROOT, qa_provider.READER) is not None


def test_the_coverage_default_is_what_the_reader_resolves_to():
    """`coverage_read.DEFAULT_PROVIDER` is the flag the coverage pair passes, so a run
    that takes it records that and not `defaults.provider`."""
    assert qa_provider.resolve(
        deployment.ROOT, qa_provider.READER, coverage_read.DEFAULT_PROVIDER
    ) == coverage_read.DEFAULT_PROVIDER


def test_every_qa_artifact_uses_the_one_field_name():
    """One key across the five files, so a reader of two of them is reading the same
    field. Named on the module rather than spelled out per harness, which is how five
    spellings of the same fact happen."""
    builders = (
        qa_gate.as_dict([], "anthropic", "0.19.0"),
        qa_coverage_gate.as_dict([], 1, "anthropic", "0.19.0"),
        qa_coverage.as_dict([], 1, "anthropic", "0.19.0"),
        qa_erosion.as_dict([], 3, 1, "anthropic", "0.19.0"),
        qa_live_sources.as_dict([], 1, "anthropic", "0.19.0"),
    )
    assert all(record[qa_provider.KEY] == "anthropic" for record in builders)


def test_the_sentence_distinguishes_a_provider_from_none():
    assert "`anthropic`" in qa_provider.line("anthropic")
    assert "cannot name" in qa_provider.line(None)
