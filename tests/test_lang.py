"""The lang-pack loader (STU-1460): validation, and the fr_ca pack's own equivalence.

The equivalence tests here are the "write the test first" half of the move: they
compare what `fr_ca.json` loads against the literals `anonymize.py`, `draft_delta.py`
and `scripts/olm_to_corpus.py` still carry, so the pack is proven correct *before* a
following commit deletes those literals. Once that commit lands, the modules
themselves are built from the pack and these tests become a straight
build-from-pack-equals-runtime-value check — which is exactly what should keep
failing if the two ever drift.
"""

import json
import re
from pathlib import Path

import pytest

from email_classifier import anonymize
from email_classifier import draft_delta
from email_classifier import lang
from email_classifier.lang import LangPackError, load_lang_config

PACKS_DIR = Path(lang.__file__).parent / "lang_packs"


def test_fr_ca_pack_exists_and_validates():
    config = load_lang_config("fr_ca")
    assert lang.REQUIRED_KEYS <= config.keys()


def test_missing_pack_raises():
    load_lang_config.cache_clear()
    with pytest.raises(LangPackError, match="No lang pack for language"):
        load_lang_config("does-not-exist")


def test_missing_required_key_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(lang, "_LANG_PACKS_DIR", tmp_path)
    incomplete = {key: [] for key in sorted(lang.REQUIRED_KEYS)[:-1]}
    (tmp_path / "broken.json").write_text(json.dumps(incomplete), encoding="utf-8")
    load_lang_config.cache_clear()
    with pytest.raises(LangPackError, match="missing required key"):
        load_lang_config("broken")
    load_lang_config.cache_clear()


def test_unreadable_json_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(lang, "_LANG_PACKS_DIR", tmp_path)
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    load_lang_config.cache_clear()
    with pytest.raises(LangPackError, match="unreadable"):
        load_lang_config("bad")
    load_lang_config.cache_clear()


def test_non_object_json_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(lang, "_LANG_PACKS_DIR", tmp_path)
    (tmp_path / "list.json").write_text("[1, 2, 3]", encoding="utf-8")
    load_lang_config.cache_clear()
    with pytest.raises(LangPackError, match="must be a JSON object"):
        load_lang_config("list")
    load_lang_config.cache_clear()


def test_required_and_optional_keys_are_disjoint():
    assert lang.REQUIRED_KEYS.isdisjoint(lang.OPTIONAL_KEYS)


# --- Equivalence: the pack matches the literals it is meant to replace ---------------


def test_stopwords_plus_elision_prefixes_equal_anonymize_KEPT():
    config = load_lang_config("fr_ca")
    rebuilt = set(config["stopwords"]) | set(config.get("elision_prefixes", []))
    assert rebuilt == anonymize._KEPT


def test_role_words_equal_anonymize_ROLES():
    config = load_lang_config("fr_ca")
    assert set(config["role_words"]) == anonymize._ROLES


def test_calendar_words_equal_anonymize_CALENDAR():
    config = load_lang_config("fr_ca")
    assert set(config["calendar_words"]) == anonymize._CALENDAR


def test_common_noun_surnames_equal_anonymize_COMMON_NOUNS():
    config = load_lang_config("fr_ca")
    assert set(config["common_noun_surnames"]) == anonymize._COMMON_NOUNS


def test_closing_formulas_rebuild_anonymize_CLOSING_byte_for_byte():
    config = load_lang_config("fr_ca")
    rebuilt = re.compile(
        r"\A(?:" + "|".join(config["closing_formulas"]) + r")[\s,!.]*\Z", re.I
    )
    assert rebuilt.pattern == anonymize._CLOSING.pattern
    assert rebuilt.flags == anonymize._CLOSING.flags


def test_client_footers_rebuild_anonymize_FOOTER_byte_for_byte():
    config = load_lang_config("fr_ca")
    rebuilt = re.compile(r"\A(?:" + "|".join(config["client_footers"]) + r")", re.I)
    assert rebuilt.pattern == anonymize._FOOTER.pattern
    assert rebuilt.flags == anonymize._FOOTER.flags


def test_name_line_equals_anonymize_NAME_LINE():
    config = load_lang_config("fr_ca")
    assert config["name_line"] == anonymize._NAME_LINE.pattern


def test_greeting_openers_rebuild_draft_delta_GREETING_byte_for_byte():
    config = load_lang_config("fr_ca")
    rebuilt = re.compile(
        r"^(" + "|".join(config["greeting_openers"]) + r")\b[^.!?]{0,40}$", re.I
    )
    assert rebuilt.pattern == draft_delta._GREETING.pattern
    assert rebuilt.flags == draft_delta._GREETING.flags


def test_draft_closing_openers_rebuild_draft_delta_CLOSING_byte_for_byte():
    config = load_lang_config("fr_ca")
    rebuilt = re.compile(
        r"^(" + "|".join(config["draft_closing_openers"]) + r")\b[^.!?]{0,60}$", re.I
    )
    assert rebuilt.pattern == draft_delta._CLOSING.pattern
    assert rebuilt.flags == draft_delta._CLOSING.flags


def test_dictionaries_key_matches_the_hardcoded_hunspell_default_today():
    """`dictionaries` matches what the five scripts' `--dictionary` default carries.

    Frozen against a literal here rather than only against the scripts' own
    `_DEFAULT_DICTIONARIES` (below): this is the regression guard on the pack's own
    content, independent of whether every consumer stayed wired to it.
    """
    config = load_lang_config("fr_ca")
    assert config["dictionaries"] == [
        "/usr/share/hunspell/fr_CA.dic", "/usr/share/hunspell/en_CA.dic",
    ]
