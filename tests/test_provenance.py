from datetime import date

import subprocess
from pathlib import Path

from email_classifier.orchestrate.provenance import (
    _version,
    collect,
    resolved_provider,
    unusable_provider,
)
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.io")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_collect_records_provider_commit_and_rules_hash(tmp_path):
    repo = _repo(tmp_path)
    p = collect("mock", repo / "rules.yaml", repo)
    assert p["provider"] == "mock"
    assert len(p["git_commit"]) == 40
    assert p["git_dirty"] is False
    assert len(p["rules_sha256"]) == 64


def test_collect_flags_a_dirty_checkout(tmp_path):
    repo = _repo(tmp_path)
    (repo / "rules.yaml").write_text("rules: [x]\n", encoding="utf-8")
    assert collect("mock", repo / "rules.yaml", repo)["git_dirty"] is True


def test_collect_ignores_untracked_files(tmp_path):
    repo = _repo(tmp_path)
    (repo / "scratch.txt").write_text("agent scratch\n", encoding="utf-8")
    assert collect("mock", repo / "rules.yaml", repo)["git_dirty"] is False


def test_collect_degrades_outside_git_and_without_rules(tmp_path):
    p = collect("claude-code", tmp_path / "nope.yaml", tmp_path)
    p.pop("detector_stack")
    p.pop("kernel_commit")
    assert p == {
        "provider": "claude-code",
        "git_commit": None,
        "git_dirty": None,
        "rules_sha256": None,
        "learned_sha256": None,
        "anonymized_fields": [],
        "input_path": None,
        "input_sha256": None,
        "start_date": None,
        "mailbox": None,
    }


def test_collect_names_the_kernel_apart_from_the_deployment(tmp_path):
    """A deployment's commit is not the kernel's once the kernel is a pinned submodule."""
    repo = _repo(tmp_path)
    p = collect("mock", repo / "rules.yaml", repo)
    kernel = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert p["kernel_commit"] == kernel
    assert p["kernel_commit"] != p["git_commit"]


def test_collect_records_the_detector_stack(tmp_path):
    stack = collect("mock", tmp_path / "nope.yaml", tmp_path)["detector_stack"]
    assert set(stack) == {"gliner", "transformers", "torch"}
    assert all(v is None or isinstance(v, str) for v in stack.values())


def test_version_reads_an_installed_package_and_nulls_an_absent_one():
    assert _version("pytest")
    assert _version("no-such-package-stu919") is None


def test_collect_records_the_anonymized_fields(tmp_path):
    p = collect("mock", tmp_path / "nope.yaml", tmp_path, None, ["body_snippet"])
    assert p["anonymized_fields"] == ["body_snippet"]


def test_collect_hashes_the_learned_layer(tmp_path):
    repo = _repo(tmp_path)
    (repo / "rules.yaml").write_text("rules: []\n", encoding="utf-8")
    learned = repo / "learned.yaml"
    learned.write_text("rules: []\n", encoding="utf-8")
    p = collect("mock", repo / "rules.yaml", repo, learned)
    assert len(p["learned_sha256"]) == 64


def _studio(tmp_path, agent_provider=None, default_provider="claude-code"):
    """A `.studio/` holding just the two files the resolution order reads."""
    (tmp_path / "agents").mkdir(parents=True)
    agent = "name: classifier\n"
    if agent_provider:
        agent += f"provider: {agent_provider}\n"
    (tmp_path / "agents" / "classifier.agent.yaml").write_text(agent, encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        f"defaults:\n  provider: {default_provider}\n", encoding="utf-8")
    return tmp_path


def test_an_override_wins_over_the_agents_own_provider(tmp_path):
    studio = _studio(tmp_path, agent_provider="anthropic")
    assert resolved_provider(studio, "mock") == "mock"


def test_the_agents_own_provider_wins_over_the_config_default(tmp_path):
    """STU-1013: the classifier names `anthropic` for itself, and provenance has to
    record what ran rather than the default the rest of `.studio/` falls back to."""
    studio = _studio(tmp_path, agent_provider="anthropic")
    assert resolved_provider(studio) is not None
    assert resolved_provider(studio) == "anthropic"


def test_the_config_default_answers_for_an_agent_that_names_none(tmp_path):
    studio = _studio(tmp_path)
    assert resolved_provider(studio) == "claude-code"


def test_an_env_referenced_provider_resolves_the_way_studio_does(monkeypatch, tmp_path):
    """STU-1769: a pin like `${DRAFT_PROVIDER:-}` reaches Studio interpolated, so
    provenance records the value, not the reference, and empty falls through."""
    studio = _studio(tmp_path, agent_provider="${DRAFT_PROVIDER:-}",
                     default_provider="${STUDIO_DEFAULT_PROVIDER:-claude-code}")
    monkeypatch.delenv("DRAFT_PROVIDER", raising=False)
    monkeypatch.delenv("STUDIO_DEFAULT_PROVIDER", raising=False)
    assert resolved_provider(studio) == "claude-code"

    monkeypatch.setenv("STUDIO_DEFAULT_PROVIDER", "anthropic")
    assert resolved_provider(studio) == "anthropic"

    monkeypatch.setenv("DRAFT_PROVIDER", "ollama")
    assert resolved_provider(studio) == "ollama"

    monkeypatch.setenv("DRAFT_PROVIDER", "")
    assert resolved_provider(studio) == "anthropic"


def test_an_unreadable_studio_dir_resolves_to_null_rather_than_a_guess(tmp_path):
    assert resolved_provider(tmp_path / "no-such-dir") is None


def _config(tmp_path, providers: str) -> Path:
    studio = _studio(tmp_path, agent_provider="anthropic")
    (studio / "config.yaml").write_text(
        f"providers:\n{providers}defaults:\n  provider: claude-code\n", encoding="utf-8")
    return studio


def test_a_provider_whose_config_names_an_unset_variable_is_refused(monkeypatch, tmp_path):
    """STU-1128: Studio builds a provider on first use, so a missing key arrives as one
    dead conversation per escalation inside a run whose every stage is green."""
    studio = _config(tmp_path, "  anthropic:\n    apiKey: ${ANTHROPIC_API_KEY}\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    refusal = unusable_provider(studio, "anthropic")
    assert refusal is not None
    # Names the provider and the variable: the repair is one or the other.
    assert "anthropic" in refusal and "ANTHROPIC_API_KEY" in refusal


def test_the_variable_being_set_is_the_whole_of_what_is_asked(monkeypatch, tmp_path):
    studio = _config(tmp_path, "  anthropic:\n    apiKey: ${ANTHROPIC_API_KEY}\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert unusable_provider(studio, "anthropic") is None


def test_an_empty_value_counts_as_unset(monkeypatch, tmp_path):
    """`.env` sets keys it does not fill, so an empty string is what an unfilled one is."""
    studio = _config(tmp_path, "  anthropic:\n    apiKey: ${ANTHROPIC_API_KEY}\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert unusable_provider(studio, "anthropic") is not None


def _path_with(monkeypatch, tmp_path, *executables: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in executables:
        exe = bin_dir / name
        exe.write_text("#!/bin/sh\n", encoding="utf-8")
        exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def test_a_provider_that_needs_nothing_from_the_environment_passes(monkeypatch, tmp_path):
    """`claude-code: {}` is the enable switch and interpolates nothing."""
    _path_with(monkeypatch, tmp_path, "claude")
    studio = _config(tmp_path, "  claude-code: {}\n")
    assert unusable_provider(studio, "claude-code") is None


def test_claude_code_without_its_executable_on_path_is_refused(monkeypatch, tmp_path):
    """STU-1760: every escalation on the serving host died on `Executable not found in $PATH:
    "claude"` for 21 hours, and no config variable could have said so."""
    bin_dir = _path_with(monkeypatch, tmp_path)
    studio = _config(tmp_path, "  claude-code: {}\n  anthropic:\n    apiKey: ${ANTHROPIC_API_KEY}\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    refusal = unusable_provider(studio, "claude-code")
    assert refusal is not None
    assert "claude" in refusal and str(bin_dir) in refusal
    assert unusable_provider(studio, "anthropic") is None
    assert unusable_provider(studio, "mock") is None


def test_a_provider_the_config_does_not_describe_is_not_refused(tmp_path):
    """`mock` is registered by the CLI itself, so a config that says nothing about a
    provider is a config this guard has nothing to read — not grounds to refuse."""
    studio = _config(tmp_path, "  anthropic:\n    apiKey: ${ANTHROPIC_API_KEY}\n")
    assert unusable_provider(studio, "mock") is None
    assert unusable_provider(studio, None) is None


def test_an_unreadable_config_leaves_the_refusal_to_studio(tmp_path):
    """A config that will not parse fails the run loudly in the engine; guessing here
    would refuse every host whose `.studio/` this cannot see."""
    assert unusable_provider(tmp_path / "no-such-dir", "anthropic") is None


def test_the_real_config_refuses_only_the_provider_that_needs_a_key(monkeypatch, tmp_path):
    """Read off this repo's own `.studio/` rather than a fixture, so it tracks what the
    config actually says. Since STU-1133 dropped the classifier's pin both the default
    and the host override are `claude-code`, which needs nothing — so what the guard
    bites is the host that names `anthropic` and has no key, which is the shape the
    2026-08-30 failure had while the pin still made it the default."""
    studio = deployment.ROOT / ".studio"
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _path_with(monkeypatch, tmp_path, "claude")

    assert unusable_provider(studio, resolved_provider(studio, "anthropic")) is not None
    assert unusable_provider(studio, resolved_provider(studio)) is None
    assert unusable_provider(studio, resolved_provider(studio, "claude-code")) is None


def test_collect_records_the_floor_the_run_reached_back_to(tmp_path):
    """A report is the only artifact that can tell "out of scope" from "missed"
    for mail it does not hold (STU-1123)."""
    p = collect("mock", tmp_path / "nope.yaml", tmp_path, start_date=date(2026, 8, 19))
    assert p["start_date"] == "2026-08-19"


def test_collect_records_the_mailbox_the_run_addressed(tmp_path):
    """The field STU-1183 exists for: which store the report's ids resolve in."""
    p = collect("mock", tmp_path / "nope.yaml", tmp_path,
                mailbox="inbox@club.example")
    assert p["mailbox"] == "inbox@club.example"


def test_collect_nulls_the_mailbox_when_nothing_names_one(tmp_path):
    """Null rather than a guess at whichever box this host is usually pointed at.

    Every caller that reads no mailbox arrives here the same way: Gmail, which names
    no address at all, a delegated Graph grant addressing `/me`, and a run over a
    frozen dump, which read no mailbox today.
    """
    assert collect("mock", tmp_path / "nope.yaml", tmp_path)["mailbox"] is None
