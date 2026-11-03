"""The environment wins over `.env`, and an unresolved `op://` value refuses (STU-2012).

`op run --env-file .env -- make <target>` resolved each reference and the Makefile's
`-include .env` wrote the literal reference back over it, so a Graph login went out
against `.../op://Dev/...`. Each case runs the real Makefile against a throwaway `.env`.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).parent.parent / "Makefile"
PROBE = 'probe: ; @printf "%s\\n" "$$OUTLOOK_TENANT_ID"'

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _make(tmp_path: Path, env_file: str, *goals: str, **env: str):
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    base = {k: v for k, v in os.environ.items() if k != "OUTLOOK_TENANT_ID"}
    return subprocess.run(
        ["make", "-s", "-C", str(tmp_path), "-f", str(MAKEFILE), "--eval", PROBE, *goals],
        env={**base, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent), **env},
        capture_output=True,
        text=True,
    )


def test_an_unresolved_reference_refuses_and_names_the_command(tmp_path):
    result = _make(tmp_path, "OUTLOOK_TENANT_ID=op://Dev/item/field\n", "probe")
    assert result.returncode != 0
    assert "OUTLOOK_TENANT_ID" in result.stderr
    assert "op run --env-file .env -- make probe" in result.stderr
    assert "op://" not in result.stdout


def test_the_environment_beats_the_reference_in_env(tmp_path):
    result = _make(
        tmp_path, "OUTLOOK_TENANT_ID=op://Dev/item/field\n", "probe",
        OUTLOOK_TENANT_ID="resolved$value",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "resolved$value"


def test_a_plain_env_file_still_loads(tmp_path):
    result = _make(tmp_path, "OUTLOOK_TENANT_ID=plain\n", "probe")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "plain"


def test_an_offline_goal_runs_without_op_run(tmp_path):
    (tmp_path / ".env").write_text("OUTLOOK_TENANT_ID=op://Dev/item/field\n")
    result = subprocess.run(
        ["make", "-n", "-C", str(tmp_path), "-f", str(MAKEFILE), "clean"],
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(tmp_path.parent)},
        capture_output=True,
        text=True,
    )
    assert "op://" not in result.stderr
    assert result.returncode == 0, result.stderr
