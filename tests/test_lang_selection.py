"""A deployment's `policy.language` really selects the pack person detection runs on (STU-1723).

`anonymize.py` builds its vocabulary at import time, so the selection cannot be tested
by handing a function a policy: each case runs in a fresh interpreter whose
`$EMAIL_CLASSIFIER_POLICY` names a deployment policy, with a hand-authored `en_us` pack
that shares no word with `fr_ca`'s role list. A word only one pack vetoes is the probe:
the detector stub claims it, and whether `person_spans` keeps it says which pack loaded.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from email_classifier import lang
from email_classifier.lang import LangPackError

REPO_ROOT = Path(lang.__file__).resolve().parents[1]

EN_US = {
    "stopwords": ["the", "and"],
    "role_words": ["zorblax"],
    "calendar_words": ["monday"],
    "common_noun_surnames": [],
    "closing_formulas": ["thanks"],
    "client_footers": ["sent from"],
    "quote_markers": [{"pattern": "^On .* wrote:", "flags": ["M"]}],
    "dictionaries": [],
    "name_line": "\\A([A-Z][a-z'-]+(?:[ \\t]+[A-Z][a-z'-]+){0,2})[\\s,]*\\Z",
}

# `Zorblax` is a role word in en_us only; `athlètes` is one in fr_ca only. The stub
# detector claims both, so the surviving spans are the pack's own veto list at work.
PROBE = """
import json, sys
from pathlib import Path
from email_classifier import lang
lang._LANG_PACKS_DIR = Path(sys.argv[1])
from email_classifier import anonymize

TEXT = "Zorblax and Athlètes"

class Claims:
    def predict_entities(self, text, labels, threshold):
        return [
            {"start": text.index(w), "end": text.index(w) + len(w), "text": w}
            for w in ("Zorblax", "Athlètes") if w in text
        ]

anonymize._model = Claims
print(json.dumps([TEXT[s:e] for s, e in anonymize.person_spans(TEXT)]))
"""


def _claimed(tmp_path: Path, language: str | None) -> list[str]:
    packs = tmp_path / "packs"
    packs.mkdir()
    (packs / "en_us.json").write_text(json.dumps(EN_US))
    (packs / "fr_ca.json").write_text((Path(lang.__file__).parent / "lang_packs" / "fr_ca.json").read_text())
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "deterministic_can_finalize: true\n" + (f"language: {language}\n" if language else "")
    )
    out = subprocess.run(
        [sys.executable, "-c", PROBE, str(packs)],
        cwd=REPO_ROOT,
        env={"PATH": "/usr/bin", "PYTHONPATH": str(REPO_ROOT), "EMAIL_CLASSIFIER_POLICY": str(policy)},
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_a_policy_naming_en_us_runs_person_detection_on_that_pack(tmp_path):
    assert _claimed(tmp_path, "en_us") == ["Athlètes"]


def test_a_policy_naming_fr_ca_runs_person_detection_on_that_pack(tmp_path):
    assert _claimed(tmp_path, "fr_ca") == ["Zorblax"]


def test_a_policy_without_the_key_is_fr_ca(tmp_path):
    assert _claimed(tmp_path, None) == ["Zorblax"]


def test_a_policy_naming_a_missing_pack_stops_rather_than_falling_back(tmp_path, monkeypatch):
    policy = tmp_path / "policy.yaml"
    policy.write_text("deterministic_can_finalize: true\nlanguage: xx_yy\n")
    monkeypatch.setenv(lang.POLICY_ENV, str(policy))
    with pytest.raises(LangPackError, match="xx_yy"):
        lang.deployment_config()


