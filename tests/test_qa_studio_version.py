"""Which Studio version ran a QA measurement (STU-1241)."""

import subprocess
from pathlib import Path

from email_classifier import (qa_coverage, qa_coverage_gate, qa_erosion, qa_gate,
                              qa_live_sources, qa_studio_version)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch(monkeypatch, completed):
    """Stubs `studio --version` only; any other subprocess call is a test bug."""
    def fake_run(cmd, **kwargs):
        assert cmd == ["studio", "--version"], cmd
        return completed

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_the_version_is_read_off_stdout(monkeypatch):
    _patch(monkeypatch, _FakeCompleted(0, "0.19.0\n"))
    assert qa_studio_version.resolve(REPO_ROOT) == "0.19.0"


def test_a_studio_binary_that_exits_non_zero_is_not_a_version(monkeypatch):
    _patch(monkeypatch, _FakeCompleted(1, "", "boom"))
    assert qa_studio_version.resolve(REPO_ROOT) is None


def test_a_studio_binary_whose_output_has_no_version_is_unreadable(monkeypatch):
    _patch(monkeypatch, _FakeCompleted(0, "not a version\n"))
    assert qa_studio_version.resolve(REPO_ROOT) is None


def test_a_missing_studio_binary_records_nothing_rather_than_guessing(monkeypatch):
    """STU-912's direction: a measurement nobody could take must not read as one that
    came back empty."""
    def fake_run(cmd, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert qa_studio_version.resolve(REPO_ROOT) is None


def test_a_studio_binary_that_times_out_records_nothing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert qa_studio_version.resolve(REPO_ROOT) is None


def test_only_the_version_prefix_is_read_off_a_longer_string(monkeypatch):
    """`studio --version` may print more than the bare number; the field must not
    promise more than what it found."""
    _patch(monkeypatch, _FakeCompleted(0, "studio-cli 0.19.0 (build abc123)\n"))
    assert qa_studio_version.resolve(REPO_ROOT) == "0.19.0"


def test_every_qa_artifact_uses_the_one_field_name():
    """One key across the five files, so a reader of two of them is reading the same
    field — the same guarantee `qa_provider` gives its own field."""
    builders = (
        qa_gate.as_dict([], "anthropic", "0.19.0"),
        qa_coverage_gate.as_dict([], 1, "anthropic", "0.19.0"),
        qa_coverage.as_dict([], 1, "anthropic", "0.19.0"),
        qa_erosion.as_dict([], 3, 1, "anthropic", "0.19.0"),
        qa_live_sources.as_dict([], 1, "anthropic", "0.19.0"),
    )
    assert all(record[qa_studio_version.KEY] == "0.19.0" for record in builders)


def test_the_sentence_distinguishes_a_version_from_none():
    assert "`0.19.0`" in qa_studio_version.line("0.19.0")
    assert "cannot name" in qa_studio_version.line(None)
