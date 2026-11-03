"""Every path reference-deployment/README.md names exists (STU-2015).

The README is the template a new deployment copies from, so a path it names that is not
there sends that reader looking for a file the kernel never had.
"""

import re
from pathlib import Path

KERNEL = Path(__file__).resolve().parents[1]
DEPLOYMENT = KERNEL / "reference-deployment"
README = DEPLOYMENT / "README.md"

_PATH = re.compile(r"`([\w.\-]+(?:/[\w.\-]*)*\.(?:ya?ml|json|md|py)|[\w.\-]+/(?:[\w.\-]+/?)*)`")


def _named_paths() -> set[str]:
    # The section on what the deployment lacks names those files to say they are absent.
    text = README.read_text(encoding="utf-8")
    present, _, after = text.partition("## What it deliberately does not have")
    rest = after.partition("\n## ")[2]
    return set(_PATH.findall(present + rest))


def test_the_readme_names_paths():
    assert len(_named_paths()) >= 15


def test_every_path_the_readme_names_exists():
    missing = sorted(
        p for p in _named_paths()
        if not any((root / p).exists() for root in (DEPLOYMENT, DEPLOYMENT / "rules",
                                                    DEPLOYMENT / "eval", DEPLOYMENT / ".studio",
                                                    DEPLOYMENT / ".studio" / "skills", KERNEL))
    )
    assert missing == []
