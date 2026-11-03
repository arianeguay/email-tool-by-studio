"""Reviewer-versus-draft comparison (STU-1100)."""

import pytest

from email_classifier import draft_delta as dd
from email_classifier import voice_baseline


@pytest.fixture
def signed_off(monkeypatch):
    """A deployment whose replies close on `EMC`: the sign-off is the deployment's own
    (`rules/voice.yaml`), so the closing-block logic is tested against one set here."""
    monkeypatch.setattr(dd, "_SIGNATURE",
                        voice_baseline.reply_signature({"reply_signature": r"^EMC$"}))


def test_markup_and_whitespace_are_not_an_edit():
    ours = "Bonjour,\n\nLes inscriptions sont ouvertes.\n\nEMC"
    theirs = "<p>Bonjour,</p>   <p>Les inscriptions sont ouvertes.</p><p>EMC</p>"

    assert dd.outcome(dd.similarity(ours, theirs)) == dd.UNCHANGED_LABEL


def test_a_reply_that_answers_something_else_is_replaced():
    ours = "Pourriez-vous nous indiquer la date de naissance de votre enfant?"
    theirs = ("Il faut faire l'inscription aux sélections via Spordle. Le lien est "
              "disponible sur notre site internet dans l'onglet sélections.")

    assert dd.outcome(dd.similarity(ours, theirs)) == dd.REPLACED_LABEL


@pytest.mark.parametrize("text,expected", [
    ("Bonjour, les inscriptions pour la saison sont ouvertes. Merci!", "fr"),
    ("Hello, registration for the season is open. Please let us know.", "en"),
])
def test_language_is_read_off_function_words(text, expected):
    assert dd.language(text) == expected


def test_a_text_it_cannot_call_is_unknown_rather_than_guessed():
    """Folding an unscorable draft into a language puts it behind that language's number."""
    assert dd.language("EMC 12U 15U-18U Altius") == "unknown"


def test_an_unsent_draft_is_its_own_outcome_and_carries_no_ratio():
    """The mailbox cannot say why a draft was abandoned, so nothing here claims to."""
    rows = dd.compare({"t1": "Bonjour, voici la réponse."}, {})

    assert rows[0]["outcome"] == dd.ABANDONED_LABEL
    assert rows[0]["similarity"] is None


def test_the_summary_splits_by_language():
    rows = dd.compare(
        {
            "fr": "Bonjour, les inscriptions sont ouvertes pour la saison. Merci.",
            "en": "Hello, registration for the season is open with us. Thanks.",
        },
        {
            "fr": "Bonjour, les inscriptions sont ouvertes pour la saison. Merci.",
            "en": "Hello, registration for the season is open with us. Thanks.",
        },
    )
    summary = dd.summarise(rows)

    assert summary["conversations"] == 2
    assert summary["by_outcome"][dd.UNCHANGED_LABEL] == 2
    assert summary["by_language"]["fr"][dd.UNCHANGED_LABEL] == 1
    assert summary["by_language"]["en"][dd.UNCHANGED_LABEL] == 1


def test_a_draft_still_in_the_folder_is_pending_not_abandoned():
    """A review still under way must not read as a round of bad drafts."""
    rows = dd.compare({"t1": "Bonjour, voici la réponse."}, {}, still_drafted={"t1"})

    assert rows[0]["outcome"] == dd.PENDING_LABEL
    assert rows[0]["similarity"] is None


def test_the_answering_part_drops_the_greeting_and_the_whole_closing_block(signed_off):
    """`Bonne soirée,` then `EMC` is two lines of furniture, not one."""
    reply = "Bonjour,\n\nLes sélections ont lieu le 14 septembre.\n\nBonne soirée,\n\nEMC"

    assert dd.answer_part(reply) == "Les sélections ont lieu le 14 septembre."


def test_a_reply_that_never_closes_keeps_everything_after_the_greeting():
    """Over-reporting our own length is the safe direction for a length finding."""
    assert dd.answer_part("Bonjour,\n\nOui.") == "Oui."


def test_a_greeting_carrying_the_answer_is_not_furniture(signed_off):
    """`Bonne nouvelle, ...` opens on a closing word and is the answer itself."""
    reply = "Bonjour,\n\nBonne nouvelle, les inscriptions sont ouvertes.\n\nEMC"

    assert dd.answer_part(reply) == "Bonne nouvelle, les inscriptions sont ouvertes."


def test_a_round_that_drafted_nothing_reports_null_rather_than_zero():
    """A median over no drafts is not a short round, and 0 would read as the rules working."""
    voice = dd.summarise([])["voice"]

    assert voice["ours"]["answer_chars_median"] is None
    assert voice["sent"]["under_200_chars_share"] is None


def test_an_unjoined_draft_carries_its_own_length_and_no_sent_one(signed_off):
    rows = dd.compare({"t1": "Bonjour,\n\nOui, c'est possible.\n\nEMC"}, {})

    assert rows[0]["ours_answer_chars"] == len("Oui, c'est possible.")
    assert rows[0]["sent_answer_chars"] is None


def test_comma_ratio_is_null_when_the_base_form_never_occurs():
    """A round with no `et` at all has shown nothing to judge, not the comma missing."""
    assert dd.comma_ratio(["Bonjour, voici la réponse."], "et", "comma_before_et") is None
    assert dd.comma_ratio([], "et", "comma_before_et") is None


def test_comma_ratio_is_null_when_the_deployment_has_no_such_rule(monkeypatch):
    """STU-1472: a fresh deployment's RULES is empty until it states its own. A rule
    nobody defined has nothing to judge either, same as a form that never occurs."""
    monkeypatch.setattr(dd.voice_counts, "RULES", {})
    assert dd.comma_ratio(["Malheureusement, ce n'est pas possible."],
                          "fronted_opener", "comma_after_fronted_opener") is None


def test_a_deployment_with_no_archive_reports_the_archive_side_null(monkeypatch):
    """STU-1472: a synthetic deployment with no archive file gets `null` where the first deployment's
    numbers used to be, never a zero and never another deployment's numbers borrowed as a default."""
    monkeypatch.setattr(dd, "ARCHIVE", None)
    voice = dd.summarise([])["voice"]

    assert voice["archive"] is None


def test_a_round_with_no_texts_reports_no_comma_ratios():
    """Omitting the raw bodies is the same as an empty round, never a crash."""
    voice = dd.summarise([])["voice"]

    assert voice["ours"]["commas"] == {"fronted_opener": None, "et": None, "que_qui": None}
    assert voice["sent"]["commas"] == {"fronted_opener": None, "et": None, "que_qui": None}
