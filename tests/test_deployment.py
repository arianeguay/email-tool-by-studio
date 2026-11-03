"""Where the kernel looks for the deployment it serves (STU-1995)."""

import os
import subprocess
import sys
from pathlib import Path

from email_classifier import deployment

KERNEL_TREE = Path(deployment.__file__).resolve().parents[1]


def _root_in_a_fresh_process(env_value: str | None) -> Path:
    env = {k: v for k, v in os.environ.items() if k != deployment.ROOT_ENV}
    if env_value is not None:
        env[deployment.ROOT_ENV] = env_value
    env["PYTHONPATH"] = str(KERNEL_TREE)
    out = subprocess.run(
        [sys.executable, "-c", "from email_classifier import deployment; print(deployment.ROOT)"],
        env=env, capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip())


def test_unset_keeps_the_single_checkout_layout():
    assert _root_in_a_fresh_process(None) == KERNEL_TREE


def test_the_variable_names_the_deployment(tmp_path):
    assert _root_in_a_fresh_process(str(tmp_path)) == tmp_path


def test_deployment_paths_follow_the_root(tmp_path):
    env = {**os.environ, deployment.ROOT_ENV: str(tmp_path), "PYTHONPATH": str(KERNEL_TREE)}
    out = subprocess.run(
        [sys.executable, "-c",
         "from email_classifier import lang, voice_baseline;"
         "print(lang.DEFAULT_POLICY); print(voice_baseline.DEFAULT_PATH)"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert out.stdout.split() == [
        str(tmp_path / "rules" / "policy.yaml"),
        str(tmp_path / "rules" / "voice.yaml"),
    ]


def test_vouched_skill_is_the_one_the_policy_names(tmp_path):
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "policy.yaml").write_text(
        "deterministic_can_finalize: true\nvouched_skill: org-facts\n"
    )
    env = {**os.environ, deployment.ROOT_ENV: str(tmp_path), "PYTHONPATH": str(KERNEL_TREE)}
    env.pop("EMAIL_CLASSIFIER_POLICY", None)
    out = subprocess.run(
        [sys.executable, "-c", "from email_classifier import anonymize; print(anonymize.SKILL)"],
        env=env, capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == str(tmp_path / ".studio" / "skills" / "org-facts.skill.md")
