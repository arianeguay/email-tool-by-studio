"""README and .env.example must name the same environment variables (STU-958)."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_README_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=", re.M)
_SPAN = re.compile(r"`([A-Z][A-Z0-9_]{2,})`")
_ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=", re.M)
_MAKE_VAR = re.compile(r"^([A-Z][A-Z0-9_]*)\s*[?:]?=", re.M)
# `PERSON_1`, `NAME_X`: anonymizer placeholders, the one other all-caps span in the prose.
_PLACEHOLDER = re.compile(r"_(X|\d+)$")


def _readme_env_names() -> set[str]:
    readme = (REPO_ROOT / "README.md").read_text()
    make_vars = set(_MAKE_VAR.findall((REPO_ROOT / "Makefile").read_text()))
    named = set(_README_ASSIGNMENT.findall(readme)) | set(_SPAN.findall(readme))
    return {n for n in named if n not in make_vars and not _PLACEHOLDER.search(n)}


def test_readme_variables_are_in_env_example():
    documented = set(_ENV_LINE.findall((REPO_ROOT / ".env.example").read_text()))
    missing = sorted(_readme_env_names() - documented)
    assert not missing, f"named in README, absent from .env.example: {', '.join(missing)}"
