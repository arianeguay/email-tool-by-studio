"""A habit counted apart from a pasted block (STU-1138)."""

import re

from email_classifier.voice_counts import PASTE, RULES, _compile_rules, blocks, count, occurrences

# The form these tests count. The deployment's own rules come from `rules/voice.yaml`;
# what is tested here is the block logic, so the pattern is stated rather than borrowed.
SEMICOLON = re.compile(";")

# Long enough that two replies carrying it share more than PASTE characters of context.
PASTED = ("Nous offrons de la musique pour les jeunes au complexe Bois-de-Isabelle "
          "à Montréal avec CSVM ; https://exemple.test/inscriptions")


def test_a_block_pasted_across_replies_counts_no_typed_occurrence():
    measured = count([f"Bonjour, {PASTED} Bonne journée, EMC",
                      f"Bonjour, {PASTED} Bonne soirée, EMC"], SEMICOLON)
    assert measured["replies"] == 2
    assert measured["occurrences"] == 2
    assert measured["distinct"] == 0
    assert [block["occurrences"] for block in measured["pasted"]] == [2]


def test_the_same_form_typed_in_two_replies_counts_twice():
    measured = count(["Les inscriptions ouvrent lundi ; le paiement suit en ligne.",
                      "Merci de votre patience ; nous revenons vers vous demain."],
                     SEMICOLON)
    assert measured["distinct"] == 2
    assert measured["distinct_replies"] == 2
    assert measured["pasted"] == []


def test_a_form_repeated_inside_one_reply_is_that_reply_writing():
    """Never a block: an enumeration down one message is one person's decision."""
    measured = count([f"{PASTED} et encore {PASTED}"], SEMICOLON)
    assert measured["replies"] == 1
    assert measured["occurrences"] == 2
    assert measured["distinct"] == 2


def test_a_sample_names_the_block_it_collapsed():
    sample = count([f"Bonjour, {PASTED}", f"Bonsoir, {PASTED}"],
                   SEMICOLON)["pasted"][0]["sample"]
    assert "avec CSVM" in sample
    assert "https://exemple.test" in sample


def test_context_shorter_than_the_threshold_does_not_join_two_replies():
    """Two short replies agreeing on a handful of characters are not one block."""
    assert len(PASTED) > PASTE
    measured = count(["oui ; non", "oui ; peut-être"], SEMICOLON)
    assert measured["distinct"] == 2


def test_blocks_partition_every_occurrence():
    replies = [f"Bonjour, {PASTED}", f"Bonsoir, {PASTED}", "Merci ; à bientôt."]
    found = occurrences(replies, SEMICOLON)
    assert sorted(index for group in blocks(found) for index in group) == list(range(len(found)))


def test_every_rule_is_a_compiled_pattern():
    assert all(isinstance(pattern, re.Pattern) for pattern in RULES.values())


def test_a_case_insensitive_rule_carries_its_flag_across_the_yaml_boundary():
    """STU-1472: `(?i)` at the very start of the source string is how `rules/voice.yaml`
    states a case-insensitive rule; the compiled pattern must actually behave as one."""
    rules = _compile_rules({"vouvoiement": r"(?i)\b(vous|votre|vos)\b"})
    assert rules["vouvoiement"].search("VOTRE dossier")


def test_no_deployment_data_compiles_to_no_rules():
    """STU-1472: a fresh deployment with no `rules/voice.yaml` has nothing to count,
    which is an empty table -- never a claim of zero occurrences of an undefined form."""
    assert _compile_rules(None) == {}
    assert _compile_rules({}) == {}
