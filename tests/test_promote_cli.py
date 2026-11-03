"""End-to-end tests for `make promote` (scripts/promote_rules.py)."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

from email_classifier.learn.promote import load_blacklist, load_learned
from tests.conftest import REPO_ROOT, RULES_DIR

SCRIPT = REPO_ROOT / "scripts" / "promote_rules.py"
SENDER = "centris@mlsmatrix.test"


def _store(path: Path, sender: str = SENDER, action: str = "label_archive") -> Path:
    lines = [
        {"run_id": run_id, "timestamp": "2026-08-18T00:00:00+00:00", "message_id": mid,
         "from_address": sender, "bucket": "C", "suggested_action": action,
         "confidence": "high"}
        for run_id, mid in [("run-1", "m1"), ("run-1", "m2"), ("run-2", "m3")]
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _run(tmp_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT),
         "--store", str(tmp_path / "verdicts.jsonl"),
         "--rules", str(RULES_DIR / "rules.yaml"),
         "--learned", str(tmp_path / "learned.yaml"),
         "--blacklist", str(tmp_path / "blacklist.yaml"), *extra],
        capture_output=True, text=True, check=True,
    )


def test_promote_writes_the_learned_layer(tmp_path):
    _store(tmp_path / "verdicts.jsonl")
    result = _run(tmp_path)

    assert "PROMOTE" in result.stdout
    emitted = yaml.safe_load((tmp_path / "learned.yaml").read_text())["rules"]
    assert [r["conditions"]["from_address"] for r in emitted] == [SENDER]
    assert emitted[0]["confidence"] == "medium"


def test_dry_run_writes_nothing(tmp_path):
    _store(tmp_path / "verdicts.jsonl")
    _run(tmp_path, "--dry-run")
    assert not (tmp_path / "learned.yaml").exists()


def test_revoke_drops_the_rule_and_bars_re_promotion(tmp_path):
    _store(tmp_path / "verdicts.jsonl")
    _run(tmp_path)
    assert load_learned(tmp_path / "learned.yaml")

    _run(tmp_path, "--revoke", SENDER.upper())

    assert load_learned(tmp_path / "learned.yaml") == []
    assert load_blacklist(tmp_path / "blacklist.yaml") == {SENDER}

    _run(tmp_path)  # a later promotion run must not bring it back
    assert load_learned(tmp_path / "learned.yaml") == []


def test_empty_store_leaves_the_layer_alone(tmp_path):
    _store(tmp_path / "verdicts.jsonl")
    _run(tmp_path)
    (tmp_path / "verdicts.jsonl").write_text("", encoding="utf-8")

    result = _run(tmp_path)

    assert "nothing to promote" in result.stdout
    assert len(load_learned(tmp_path / "learned.yaml")) == 1


def test_revoke_works_with_an_empty_store(tmp_path):
    _store(tmp_path / "verdicts.jsonl")
    _run(tmp_path)
    (tmp_path / "verdicts.jsonl").write_text("", encoding="utf-8")

    _run(tmp_path, "--revoke", SENDER)

    assert load_learned(tmp_path / "learned.yaml") == []
