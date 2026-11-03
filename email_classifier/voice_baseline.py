"""The deployment's own archive-derived voice baseline (STU-1472).

`draft_delta.ARCHIVE`, `voice_counts.RULES` and `orchestrate.drafts.EVENING_FROM_HOUR`
used to be Python literals measured on the first deployment's `Éléments envoyés` archive, so a
deployment could not replace any of them without editing kernel files. `rules/voice.yaml`
is where a deployment's own values live now, alongside `rules/policy.yaml` and
`rules/web_sources.yaml`: this deployment's data, not a kernel default.

A fresh deployment with no archive measured yet ships no such file, or one missing a
section. `load()` returns `None` rather than raising or fabricating one, and every
reader downstream treats that as null, never a zero and never another deployment's numbers
borrowed as a default. This is `usd_per_drafted_conversation`'s rule (`cost.py`) and
`pilot_delta._lengths`'s, extended to a third source.

STU-1728 extends it to the two club-named literals that stayed in kernel code after
STU-1460: `reply_signature` (`draft_delta`) and `quote_markers` (`olm_to_corpus`). A
deployment that declares neither strips no signature and cuts only on the lang pack's
generic markers.
"""

import re
from pathlib import Path

import yaml

from email_classifier import deployment, lang

REPO_ROOT = deployment.ROOT
DEFAULT_PATH = REPO_ROOT / "rules" / "voice.yaml"


def load(path: Path = DEFAULT_PATH) -> dict | None:
    """The deployment's voice baseline, or `None` on one that has not measured one."""
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8")) or None


def reply_signature(baseline: dict | None) -> re.Pattern:
    """The deployment's sign-off line, or a pattern that matches nothing.

    `(?!)` and not `None`, so the caller's loop is the same either way, and not an
    empty alternation, which would match every line's zero-width start.
    """
    return re.compile((baseline or {}).get("reply_signature") or "(?!)")


def quote_markers(baseline: dict | None) -> tuple[re.Pattern, ...]:
    """The deployment's own quote markers, to append after the lang pack's; empty if none."""
    entries = (baseline or {}).get("quote_markers")
    return lang.compiled_quote_markers({"quote_markers": entries}) if entries else ()
