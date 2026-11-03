import pytest
import yaml

from email_classifier import anonymize, deployment
from email_classifier.anonymize import Tokenizer
from email_classifier.core.contract import load_taxonomy
from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.orchestrate import batch, conversation
from tests.conftest import FindsTheChildInTheHeadOnly
from tests.test_anonymize import requires_gliner

TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")

CATCH_ALL = RuleMatch(
    rule_id="catch_all", action="escalate_to_llm", action_original=None,
    confidence="low", bucket=None,
)

RECORD = {
    "message_id": "msg_1", "thread_id": "thread_1", "from_address": "a@b.ca",
    "date": "2026-06-15T12:00:00+00:00",
    "rule_id": "catch_all", "action": "escalate_to_llm", "action_original": None,
    "confidence": "low", "bucket": None, "escalated": True,
}

CLASSIFICATION = {
    "thread_id": "thread_1", "bucket": "C", "subtype": min(TAXONOMY.allowed_subtypes["C"]),
    "confidence": "low", "suggested_action": "label_archive",
    "suggested_label": "archive/news", "reasoning": "Écrit par PERSON_1.",
}


# Format categories only: what these tests check is which fields get tokenized, and
# `persons=False` keeps them off the `ner` extra (see `test_anonymize.py`).
def email(message_id="msg_1", body="Bonjour, écris à marie@exemple.ca.", subject="Re: projet"):
    return EmailInput(
        message_id=message_id, thread_id="thread_1", from_address="a@b.ca",
        subject=subject, headers={"From": "Marie-Claire <marie@exemple.ca>"},
        body_snippet=body, date="2026-06-12T08:15:00+00:00", provider_category="primary",
    )


def solo(mail=None):
    mail = mail or email()
    return conversation.Escalation(head=mail, history=[], message_ids=(mail.message_id,))


def test_the_item_carries_only_the_child_prompt_fields_and_the_fingerprints():
    item = batch.escalation_item(solo(), [CATCH_ALL], Tokenizer(persons=False), [], "fp")
    assert set(item) == {
        "thread_id", "from_address", "subject", "headers", "body_snippet", "date",
        "provider_category", "thread_history", "prompt_fingerprint", "stage1_fingerprint",
    }
    assert item["thread_history"] == ""


def test_the_item_carries_the_conversation_the_run_saw():
    older = email("msg_0", body="Ma fille joue-t-elle samedi?")
    item = batch.escalation_item(
        conversation.Escalation(head=email(), history=[older], message_ids=("msg_1",)),
        [CATCH_ALL], Tokenizer(persons=False), [], "fp",
    )
    assert older.date in item["thread_history"]
    assert "Ma fille joue-t-elle samedi?" in item["thread_history"]


def test_the_transcript_is_tokenized_whenever_a_scope_is_set():
    older = email("msg_0", body="Écris à marie@exemple.ca.")
    item = batch.escalation_item(
        conversation.Escalation(head=email(), history=[older], message_ids=("msg_1",)),
        [CATCH_ALL], Tokenizer(persons=False), ["body_snippet"], "fp",
    )
    # Same token as the head's body: one tokenizer, one identity per value.
    token = item["body_snippet"].removeprefix("Bonjour, écris à ").rstrip(".")
    assert token.startswith("EMAIL_")
    assert token in item["thread_history"]
    assert "marie@exemple.ca" not in item["thread_history"]


@requires_gliner
def test_a_turn_cut_mid_name_does_not_mint_the_fragment():
    """STU-920's veto reads position, so each turn has to be tokenized on its own: a
    rendered transcript would bury every tail but the last mid-string and mint
    `Carolin` beside `Caroline` into the keymap the run persists."""
    cut = email("msg_0", body="Merci à l'avance,\n\nCarolin")
    item = batch.escalation_item(
        conversation.Escalation(head=email(), history=[cut], message_ids=("msg_1",)),
        [CATCH_ALL], Tokenizer(), ["body_snippet"], "fp",
    )
    assert "PERSON_X" in item["thread_history"]
    assert "Carolin" not in item["thread_history"]


def test_a_name_first_minted_in_the_head_is_redacted_in_the_history(monkeypatch):
    monkeypatch.setattr(anonymize, "_model", FindsTheChildInTheHeadOnly)
    older = email("msg_0", body="Je l'annonce à Rosalie ce soir.")
    head = email(body="Rosalie a reçu l'invitation.")
    item = batch.escalation_item(
        conversation.Escalation(head=head, history=[older], message_ids=("msg_1",)),
        [CATCH_ALL], Tokenizer(), ["body_snippet"], "fp",
    )
    assert item["body_snippet"] == "PERSON_1 a reçu l'invitation."
    assert "Je l'annonce à PERSON_1 ce soir." in item["thread_history"]


def test_only_the_scoped_fields_are_tokenized():
    item = batch.escalation_item(
        solo(), [CATCH_ALL], Tokenizer(persons=False), ["body_snippet"], "fp"
    )
    assert item["body_snippet"] == "Bonjour, écris à EMAIL_1."
    assert item["headers"] == {"From": "Marie-Claire <marie@exemple.ca>"}


def test_header_values_are_tokenized_under_the_full_scope():
    item = batch.escalation_item(
        solo(), [CATCH_ALL], Tokenizer(persons=False), ["headers", "body_snippet"], "fp"
    )
    assert item["headers"]["From"] == "Marie-Claire <EMAIL_1>"


def test_one_tokenizer_keeps_a_value_on_the_same_token_across_the_batch():
    tokenizer = Tokenizer(persons=False)
    first = batch.escalation_item(solo(), [CATCH_ALL], tokenizer, ["body_snippet"], "fp")
    second = batch.escalation_item(
        solo(email("msg_2", body="Salut, marie@exemple.ca, ça va?")), [CATCH_ALL],
        tokenizer, ["body_snippet"], "fp",
    )
    assert first["body_snippet"] == "Bonjour, écris à EMAIL_1."
    assert second["body_snippet"] == "Salut, EMAIL_1, ça va?"


def test_the_stage1_fingerprint_tracks_the_match_not_the_rule_file():
    other = RuleMatch(
        rule_id="newsletter", action="label_archive", action_original=None,
        confidence="high", bucket="C",
    )
    assert batch.stage1_fingerprint([CATCH_ALL]) == batch.stage1_fingerprint([CATCH_ALL])
    assert batch.stage1_fingerprint([CATCH_ALL]) != batch.stage1_fingerprint([other])


def test_the_stage1_fingerprint_covers_every_message_the_call_answers_for():
    other = RuleMatch(
        rule_id="newsletter", action="label_archive", action_original=None,
        confidence="high", bucket="C",
    )
    # A sibling dropping out of the conversation's escalation is a different question.
    assert batch.stage1_fingerprint([CATCH_ALL, other]) != batch.stage1_fingerprint([CATCH_ALL])


def _studio_dir(tmp_path):
    for relative in ("agents", "skills", "contracts", "pipelines"):
        (tmp_path / relative).mkdir()
    (tmp_path / "agents" / "classifier.agent.yaml").write_text("name: classifier\n")
    (tmp_path / "skills" / "taxonomy.skill.md").write_text("# taxonomy\n")
    return tmp_path


def test_the_prompt_fingerprint_busts_when_the_agent_changes(tmp_path):
    studio = _studio_dir(tmp_path)
    before = batch.prompt_fingerprint(studio, [])
    (studio / "agents" / "classifier.agent.yaml").write_text("name: classifier\ntemp: 1\n")
    assert batch.prompt_fingerprint(studio, []) != before


def test_the_prompt_fingerprint_busts_when_the_anonymization_scope_changes(tmp_path):
    studio = _studio_dir(tmp_path)
    assert batch.prompt_fingerprint(studio, []) != batch.prompt_fingerprint(
        studio, ["body_snippet"]
    )


# ── what a YAML source covers (STU-1149) ─────────────────────────────────────
#
# `studio_copy` and never `STUDIO`: these edit the file, and the checkout's own
# `.studio/` is state every other worker reads. The copy carries the real agent, so
# what is asserted is the production document and not a stub of it.

AGENT = "agents/classifier.agent.yaml"


def fingerprint(studio):
    return batch.prompt_fingerprint(studio, ["body_snippet"])


def test_a_comment_on_the_agent_replays_free(studio_copy):
    """The bust STU-1149 measured: a `# ...` line no model ever sees re-priced the
    whole grey zone, one stage-2 call per escalated conversation."""
    before = fingerprint(studio_copy)
    agent = studio_copy / AGENT
    agent.write_text(
        agent.read_text(encoding="utf-8") + "\n# Measured 2026-08-30: nothing.\n",
        encoding="utf-8",
    )
    assert fingerprint(studio_copy) == before


def test_re_ordering_the_agent_keys_replays_free(studio_copy):
    """The order decision, made rather than inherited: mapping keys are sorted, so
    moving `model` above `system_prompt` changes nothing that reaches a model."""
    agent = studio_copy / AGENT
    before = fingerprint(studio_copy)
    parsed = yaml.safe_load(agent.read_text(encoding="utf-8"))
    agent.write_text(
        yaml.safe_dump(dict(reversed(list(parsed.items()))), sort_keys=False),
        encoding="utf-8",
    )
    assert fingerprint(studio_copy) == before


@pytest.mark.parametrize("key, value", [
    # Every one of these is a value an agent run actually receives.
    ("system_prompt", "Classify the conversation, briefly."),
    ("model", "claude-haiku-4-5"),
    ("skills", ["taxonomy", "org-facts"]),
    ("prompt_cache", "off"),
])
def test_the_agents_own_values_still_bust(studio_copy, key, value):
    agent = studio_copy / AGENT
    before = fingerprint(studio_copy)
    parsed = yaml.safe_load(agent.read_text(encoding="utf-8"))
    parsed[key] = value
    agent.write_text(yaml.safe_dump(parsed), encoding="utf-8")
    assert fingerprint(studio_copy) != before


def test_re_ordering_the_contracts_required_fields_busts(studio_copy):
    """Sequences keep their order: a contract that lists its fields differently is a
    different contract, and canonicalizing mappings does not get to decide otherwise."""
    contract = studio_copy / "contracts" / "classification.contract.yaml"
    before = fingerprint(studio_copy)
    parsed = yaml.safe_load(contract.read_text(encoding="utf-8"))
    fields = parsed["schema"]["required_fields"]
    parsed["schema"]["required_fields"] = list(reversed(fields))
    contract.write_text(yaml.safe_dump(parsed), encoding="utf-8")
    assert fingerprint(studio_copy) != before


def test_a_comment_on_the_taxonomy_skill_still_busts(studio_copy):
    """The skill is not YAML and keeps byte hashing: it reaches the prompt verbatim,
    so a line that looks like a comment is prose the model reads."""
    skill = studio_copy / "skills" / "taxonomy.skill.md"
    before = fingerprint(studio_copy)
    skill.write_text(
        skill.read_text(encoding="utf-8") + "\n<!-- when in doubt, default to A -->\n",
        encoding="utf-8",
    )
    assert fingerprint(studio_copy) != before


def test_unparseable_yaml_falls_back_to_its_bytes(studio_copy):
    """Never under-cover: a `.studio/` file the loader will reject is not this
    function's error to report, and until it does the run misses the cache."""
    agent = studio_copy / AGENT
    agent.write_text("name: classifier\n  bad: [\n", encoding="utf-8")
    before = fingerprint(studio_copy)
    agent.write_text("name: classifier\n  bad: [\n# and a comment\n", encoding="utf-8")
    assert fingerprint(studio_copy) != before


def test_a_successful_item_becomes_an_llm_verdict_with_its_pii_restored():
    result = {"index": 0, "status": "success", "output": CLASSIFICATION}
    verdict = batch.llm_verdict(RECORD, result, {"PERSON_1": "Marie-Claire"}, TAXONOMY)
    assert (verdict.path, verdict.status, verdict.bucket) == ("llm", "ok", "C")
    assert verdict.message_id == "msg_1"
    assert verdict.reasoning == "Écrit par Marie-Claire."


def test_a_failed_item_becomes_a_tagged_error_verdict():
    result = {"index": 0, "status": "failed", "error": "Child run x failed: boom"}
    verdict = batch.llm_verdict(RECORD, result, {}, TAXONOMY)
    assert (verdict.status, verdict.error) == ("error", "studio_run_failed")
    assert verdict.error_detail == "Child run x failed: boom"


def test_an_absent_fan_out_result_becomes_a_missing_output_error():
    verdict = batch.llm_verdict(RECORD, None, {}, TAXONOMY)
    assert (verdict.status, verdict.error) == ("error", "studio_run_output_missing")


@pytest.mark.parametrize("output", [None, "not a dict"])
def test_an_item_with_no_usable_output_is_an_error(output):
    result = {"index": 0, "status": "success", "output": output}
    assert batch.llm_verdict(RECORD, result, {}, TAXONOMY).error == "studio_run_output_missing"


def test_an_output_that_fails_the_contract_is_rejected_rather_than_trusted():
    result = {"index": 0, "status": "success", "output": {**CLASSIFICATION, "bucket": "Z"}}
    verdict = batch.llm_verdict(RECORD, result, {}, TAXONOMY)
    assert (verdict.status, verdict.error) == ("error", "studio_run_output_invalid")


def test_a_stage1_decided_record_becomes_a_deterministic_verdict():
    record = {**RECORD, "rule_id": "newsletter", "action": "label_archive",
              "confidence": "high", "bucket": "C", "escalated": False}
    verdict = batch.deterministic_verdict(record)
    assert (verdict.path, verdict.status, verdict.rule_id) == ("deterministic", "ok", "newsletter")


def test_every_verdict_builder_carries_the_message_date():
    """`conversation.reconcile` reads recency off the verdict and holds a dateless thread
    rather than guessing (STU-940). A builder that stopped copying `date` would therefore
    stop reconciling conversations without failing anything — silently, on the hold side.
    """
    decided = {**RECORD, "action": "label_archive", "bucket": "C", "escalated": False}
    output = {**CLASSIFICATION, "reasoning": "r"}

    assert batch.deterministic_verdict(decided).date == RECORD["date"]
    assert batch.llm_verdict(
        RECORD, {"status": "success", "output": output}, {}, TAXONOMY
    ).date == RECORD["date"]
    assert batch.llm_verdict(RECORD, None, {}, TAXONOMY).date == RECORD["date"]


def test_results_are_indexed_and_a_missing_fan_out_yields_nothing():
    output = {"results": [{"index": 1, "status": "success"}, {"status": "success"}]}
    assert batch.results_by_index(output) == {1: {"index": 1, "status": "success"}}
    assert batch.results_by_index(None) == {}
    assert batch.results_by_index({"outputs": []}) == {}
