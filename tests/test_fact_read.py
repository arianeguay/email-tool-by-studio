"""The draft-facts reading of a (draft, sent) pair (STU-1998)."""

import json

import yaml

from email_classifier import coverage_read, fact_read
from tests.test_drafts import load_yaml


def _fact(reading):
    return {"fact": f"a {reading} fact", "reading": reading, "evidence": ""}


def test_same_substance_in_other_words_reads_as_kept():
    output = {"facts": [_fact("kept"), _fact("dropped")], "added": [], "verdict": "kept"}
    assert fact_read.read_of(output) == {"verdict": "kept", "contradicted": []}


def test_a_reply_that_adds_a_fact_reads_as_extended():
    output = {"facts": [_fact("kept")], "added": ["the deadline"], "verdict": "extended"}
    assert fact_read.read_of(output)["verdict"] == "extended"


def test_a_correction_reads_as_contradicted_and_names_the_fact():
    output = {"facts": [_fact("kept"), _fact("contradicted")], "added": ["x"],
              "verdict": "contradicted"}
    assert fact_read.read_of(output) == {"verdict": "contradicted",
                                         "contradicted": ["a contradicted fact"]}


def test_a_verdict_that_disagrees_with_its_own_lists_is_incoherent_not_kept():
    output = {"facts": [_fact("contradicted")], "added": [], "verdict": "kept"}
    assert fact_read.read_of(output)["verdict"] == fact_read.INCOHERENT


def test_an_output_of_the_wrong_shape_is_an_error_kept_apart_from_incoherent():
    assert fact_read.read_of({})["verdict"] == fact_read.ERROR
    assert fact_read.read_of({"facts": [], "added": [], "verdict": "maybe"})["verdict"] \
        == fact_read.ERROR


def test_lists_serialised_into_strings_still_read():
    output = {"facts": json.dumps([_fact("kept")]), "added": "[]", "verdict": "kept"}
    assert fact_read.read_of(output)["verdict"] == "kept"


def test_annotate_reads_only_joined_rows_and_hands_the_reader_the_two_bodies(
        monkeypatch, tmp_path):
    items = []

    def run_stage(pipeline, stage, item_path, cwd, provider, label):
        items.append(yaml.safe_load(item_path.read_text(encoding="utf-8")))
        assert (pipeline, stage) == (fact_read.PIPELINE, fact_read.STAGE)
        return {"facts": [_fact("contradicted")], "added": [], "verdict": "contradicted"}

    monkeypatch.setattr(coverage_read, "run_stage", run_stage)
    rows = [{"thread_id": "T1"}, {"thread_id": "T2"}]

    tally = fact_read.annotate(rows, {"T1": "draft", "T2": "draft 2"}, {"T1": "sent"},
                               tmp_path)

    assert tally["contradicted"] == 1 and sum(tally.values()) == 1
    assert rows[0]["facts"]["verdict"] == "contradicted"
    assert "facts" not in rows[1]
    [item] = items
    assert (item["draft_body"], item["sent_body"]) == ("draft", "sent")
    # Its own run id: Studio journals the input, and the audited run's id there would
    # give `cost` two journals to choose between.
    assert item["run_id"].startswith("draft-facts-")


def test_the_stage_the_reader_keys_on_is_the_one_the_pipeline_runs():
    pipeline = load_yaml("pipelines/draft-facts.pipeline.yaml")
    assert pipeline["name"] == fact_read.PIPELINE
    [stage] = pipeline["stages"]
    assert stage["name"] == fact_read.STAGE
    assert stage["agent"] == "fact-reader"
    assert stage["context"]["include"] == ["input"]


def test_the_reader_has_no_history_and_no_tools():
    agent = load_yaml("agents/fact-reader.agent.yaml")
    assert not agent.get("skills") and not agent.get("tools")
    assert agent["model"].startswith("${" + fact_read.MODEL_ENV)


def test_the_contract_asks_for_the_three_halves_the_reader_checks():
    contract = load_yaml("contracts/draft-facts.contract.yaml")
    assert set(contract["schema"]["required_fields"]) == {"facts", "added", "verdict"}
    assert "post_validation" not in contract
