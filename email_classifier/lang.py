"""Per-language detection vocabulary, loaded rather than hardcoded (STU-1460).

`anonymize.py` carried ~180 French/English literals — stopwords, club-mailbox role
nouns, the calendar, Quebec surnames that double as common nouns, closing formulas,
mail-client footers — plus the generic half of `scripts/olm_to_corpus.py`'s quote
markers and `email_classifier/draft_delta.py`'s greeting/closing openers. Nothing
loaded them; a second francophone deployment could not reuse this kernel without
either forking the module or inheriting another deployment's vocabulary.

One JSON file per language lives in `email_classifier/lang_packs/`, keyed by the
language code a deployment names (`fr_ca` today). Loading follows
`studio-foundation/wiki-creator`'s `wiki_creator/lang.py`: `REQUIRED_KEYS` /
`OPTIONAL_KEYS` declare the contract, and `load_lang_config` raises rather than
falling back — a deployment with a broken or missing pack must not run person
detection on an empty vocabulary, the reason `anonymize.missing_detector()` refuses
rather than degrading (STU-890).

Detection only. A deployment's voice skill and the sign-off and quote markers in its
`rules/voice.yaml` are not lang-pack keys and never will be: they say how one
organization writes, not what French looks like, and two francophone clubs do not
write alike.
"""

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from email_classifier import deployment
from email_classifier.stage1.policy import Policy, load_policy

_LANG_PACKS_DIR = Path(__file__).parent / "lang_packs"
DEFAULT_POLICY = deployment.ROOT / "rules" / "policy.yaml"
POLICY_ENV = "EMAIL_CLASSIFIER_POLICY"
_DOCS = "docs/lang-packs.md"

# Populated in the shipped pack (`fr_ca`): every list a working deployment genuinely
# needs. A missing one silently corrupts person detection or the archive-measurement
# scripts that fingerprint it. See docs/lang-packs.md for what each key feeds.
REQUIRED_KEYS = frozenset({
    "stopwords",
    "role_words",
    "calendar_words",
    "common_noun_surnames",
    "closing_formulas",
    "client_footers",
    "quote_markers",
    "dictionaries",
})

# A pack may omit these — a language that has no elisions, or vocabulary that a
# deployment measured but hasn't wired anywhere. Absent, a caller degrades to empty.
OPTIONAL_KEYS = frozenset({
    "name_line",
    "elision_prefixes",
    "greeting_openers",
    "draft_closing_openers",
})

_QUOTE_MARKER_FLAGS = {"M": re.M, "I": re.I, "S": re.S}


class LangPackError(Exception):
    """A lang pack is missing, unreadable, or incomplete.

    Raised loudly at load time so an unsupported or broken pack fails with an
    actionable message instead of silently running person detection on an empty
    vocabulary — the same direction `anonymize.missing_detector()` already refuses
    in (STU-890).
    """


@lru_cache(maxsize=8)
def load_lang_config(language: str) -> dict:
    """Load and validate `email_classifier/lang_packs/<language>.json`.

    Raises `LangPackError` if the file is missing, unreadable, not a JSON object, or
    missing a required key (see `REQUIRED_KEYS` / docs/lang-packs.md). There is no
    fallback language: a deployment that names a pack that doesn't exist stops
    rather than running on somebody else's vocabulary.

    Cached per language for the life of the process — the packs are read-only data,
    not something a run edits.
    """
    path = _LANG_PACKS_DIR / f"{language}.json"
    if not path.exists():
        raise LangPackError(
            f"No lang pack for language '{language}': {path} does not exist. "
            f"Create it (see {_DOCS})."
        )
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise LangPackError(f"Lang pack {path} is unreadable ({exc}). See {_DOCS}.") from exc
    if not isinstance(cfg, dict):
        raise LangPackError(
            f"Lang pack {path} must be a JSON object, got {type(cfg).__name__}. See {_DOCS}."
        )
    missing = sorted(REQUIRED_KEYS - cfg.keys())
    if missing:
        raise LangPackError(
            f"Lang pack {path} (language '{language}') is missing required key(s): "
            f"{', '.join(missing)}. See {_DOCS} for each key's role."
        )
    return cfg


def deployment_policy() -> Policy:
    """The running deployment's policy, for every module that builds its vocabulary at
    import time and so cannot be handed one: `$EMAIL_CLASSIFIER_POLICY` if set, else
    `rules/policy.yaml`."""
    return load_policy(Path(os.environ.get(POLICY_ENV) or DEFAULT_POLICY))


def deployment_config() -> dict:
    """The pack the running deployment's `policy.language` names (STU-1723). A policy
    that omits the key means `fr_ca`."""
    return load_lang_config(deployment_policy().language)


def compiled_quote_markers(config: dict) -> tuple[re.Pattern, ...]:
    """`config["quote_markers"]` as compiled patterns, in file order.

    Each entry is `{"pattern": <regex>, "flags": [<"M"|"I"|"S">, ...]}` rather than a
    bare string, because the markers this pack covers do not all want the same
    flags — the quoted-header block is line-anchored but not case-insensitive, the
    signature-block separators are both.
    """
    markers = []
    for entry in config["quote_markers"]:
        flags = 0
        for flag in entry.get("flags", []):
            flags |= _QUOTE_MARKER_FLAGS[flag]
        markers.append(re.compile(entry["pattern"], flags))
    return tuple(markers)
