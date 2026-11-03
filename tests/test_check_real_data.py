import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import check_real_data as guard

SCRIPT = Path(guard.__file__)


@pytest.fixture
def config(tmp_path, monkeypatch):
    keymap = tmp_path / "keymap.json"
    keymap.write_text(json.dumps({"PERSON_1": "Gaspard Fictif", "EMAIL_1": "gaspard@exemple.test", "PERSON_2": "Al"}))
    terms = tmp_path / "terms.txt"
    terms.write_text("# team names\nLes Hiboux Bleus\n")
    cfg = tmp_path / "real-data"
    cfg.write_text(f"{keymap}\n# comment\n{terms}\n")
    monkeypatch.setenv("EMAIL_CLASSIFIER_REAL_DATA", str(cfg))
    return cfg


def scan(cfg, lines):
    terms = guard.load_terms(cfg)
    return guard.scan_lines(lines, guard.matcher(terms), terms)


def test_hit_is_named_by_its_source_entry_never_the_term(config):
    hits = scan(config, [("tests/t.py", 4, 'sender = "Gaspard Fictif"'), ("a.md", 9, "Les Hiboux Bleus won")])
    assert hits == [("tests/t.py", 4, "keymap.json:PERSON_1"), ("a.md", 9, "terms.txt:2")]


def test_invented_value_and_partial_word_pass(config):
    assert scan(config, [("t.py", 1, 'sender = "Marie Exemple"'), ("t.py", 2, "gaspard@exemple.testing")]) == []


def test_a_bang_line_drops_a_term_that_names_nobody(config):
    config.write_text(config.read_text() + "!Les Hiboux Bleus\n")
    assert "Les Hiboux Bleus" not in guard.load_terms(config)


def test_terms_shorter_than_the_floor_are_skipped(config):
    assert "Al" not in guard.load_terms(config)


@pytest.mark.parametrize("setup", ["missing_config", "missing_source", "empty"])
def test_fails_closed_when_nothing_was_read(tmp_path, setup):
    cfg = tmp_path / "real-data"
    if setup == "missing_source":
        cfg.write_text(f"{tmp_path / 'gone.json'}\n")
    elif setup == "empty":
        (tmp_path / "terms.txt").write_text("# nothing\n")
        cfg.write_text(f"{tmp_path / 'terms.txt'}\n")
    with pytest.raises(guard.Unusable):
        guard.load_terms(cfg)


def test_added_lines_track_paths_and_line_numbers():
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -3,0 +4,2 @@\n+one\n+two\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n"
    )
    assert list(guard.added_lines(diff)) == [("x.py", 4, "one"), ("x.py", 5, "two")]


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def test_hooks_refuse_a_commit_end_to_end(tmp_path, config):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@exemple.test")
    _git(repo, "config", "user.name", "t")
    hooks = repo / "hooks"
    hooks.mkdir()
    for name, arg in (("pre-commit", "--staged"), ("commit-msg", '--message "$1"')):
        hook = hooks / name
        hook.write_text(f'#!/bin/sh\nexec {sys.executable} {SCRIPT} {arg}\n')
        hook.chmod(0o755)
    _git(repo, "config", "core.hooksPath", str(hooks))

    (repo / "t.py").write_text('x = "Gaspard Fictif"\n')
    _git(repo, "add", "t.py")
    refused = _git(repo, "commit", "-q", "-m", "add")
    assert refused.returncode == 1 and "t.py:1" in refused.stderr and "Gaspard" not in refused.stderr

    (repo / "t.py").write_text('x = "Marie Exemple"\n')
    _git(repo, "add", "t.py")
    assert _git(repo, "commit", "-q", "-m", "Thanks Gaspard Fictif").returncode == 1
    assert _git(repo, "commit", "-q", "-m", "add").returncode == 0
