"""Every repo path CLAUDE.md names exists (STU-2046).

CLAUDE.md is loaded into every session on this repo, so a path it names that is not
there sends that session looking for a file the kernel never had. Deployment paths
(`rules/`, `eval/`, `.studio/agents/`) resolve against `reference-deployment/`.
"""

import re
from pathlib import Path

KERNEL = Path(__file__).resolve().parents[1]
DEPLOYMENT = KERNEL / "reference-deployment"
CLAUDE_MD = KERNEL / "CLAUDE.md"

_ROOTS = (
    "email_classifier/", "scripts/", "tests/", ".studio/", ".github/", "docs/",
    "reference-deployment/", "rules/", "eval/", "fixtures/",
)
_FILES = ("Makefile", "pyproject.toml", "README.md", "run_email.py", "apply_email.py")
# Written by a run into the deployment root, never committed.
_RUNTIME = (".studio/runs/",)

_TOKEN = re.compile(r"`([^`\s]+)`")


def _named_paths() -> set[str]:
    tokens = _TOKEN.findall(CLAUDE_MD.read_text(encoding="utf-8"))
    return {
        t for t in tokens
        if (t.startswith(_ROOTS) or t in _FILES)
        and not t.startswith(_RUNTIME)
        and not re.search(r"[<>*{}]", t)
    }


def test_claude_md_names_paths():
    assert len(_named_paths()) >= 50


def test_every_path_claude_md_names_exists():
    missing = sorted(
        p for p in _named_paths()
        if not any((root / p).exists() for root in (KERNEL, DEPLOYMENT))
    )
    assert missing == []
