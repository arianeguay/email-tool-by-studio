"""What `classify-post` writes into the report beyond the verdicts (STU-843).

Its subject here is the cost leg (STU-1012): the fan-out already reports what each
item spent, and the report is where that has to land — the Studio log holding the
same counts is written beside the cwd, so a run launched from a worktree left its
only record there and a worktree since removed took it along.
"""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]

VERDICT = {
    "thread_id": "t2", "bucket": "B", "subtype": "club_question",
    "confidence": "high", "suggested_action": "needs_reply",
    "suggested_label": None, "reasoning": "une question de parent",
}


@pytest.fixture
def classify_post():
    path = REPO_ROOT / "scripts" / "classify_post.py"
    spec = importlib.util.spec_from_file_location("scripts_classify_post", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record(message_id, thread_id, escalated, index=None):
    return {
        "message_id": message_id, "thread_id": thread_id, "escalated": escalated,
        "escalation_index": index, "from_address": "a@b.ca",
        "date": "2026-06-12T08:15:00+00:00", "bucket": "C", "action": "label_archive",
        "confidence": "medium", "rule_id": "newsletter",
    }


def _payload(tmp_path, stage1, fanout):
    keymap = tmp_path / "keymap.json"
    keymap.write_text("{}", encoding="utf-8")
    pre = {
        "run_id": "run-1",
        "stage1": stage1,
        "provenance": {},
        "composition": {},
        "paths": {
            "keymap": str(keymap),
            "taxonomy": str(deployment.ROOT / "rules" / "taxonomy.yaml"),
            "verdict_store": str(tmp_path / "verdicts.json"),
            "report_dir": str(tmp_path / "reports"),
        },
    }
    outputs = {"classify-pre": pre}
    if fanout is not None:
        outputs["classify-verdicts"] = fanout
    return {"previous_outputs": outputs}


def _run(module, payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    module.main()
    out = json.loads(capsys.readouterr().out)
    return json.loads(Path(out["report_path"]).read_text(encoding="utf-8"))


def _usage(out):
    return {"by_model": {"claude-opus-4-8": {"completion_tokens": out,
                                             "total_tokens": out}}}


def test_the_report_carries_what_the_fanout_spent(classify_post, tmp_path,
                                                  monkeypatch, capsys):
    stage1 = [_record("m1", "t1", False),
              _record("m2", "t2", True, 0),
              _record("m3", "t3", True, 1)]
    fanout = {"results": [
        {"index": 0, "status": "success", "output": VERDICT, "token_usage": _usage(100)},
        {"index": 1, "status": "success", "output": {**VERDICT, "thread_id": "t3"},
         "token_usage": _usage(200)},
    ]}

    report = _run(classify_post, _payload(tmp_path, stage1, fanout), monkeypatch, capsys)

    spend = report["cost"]["classify"]
    assert spend["llm_calls"] == 2
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 300


def test_a_resumed_item_costs_this_run_nothing(classify_post, tmp_path,
                                               monkeypatch, capsys):
    """Its verdict was bought by an earlier run; counting it would report a call
    this one never spawned."""
    stage1 = [_record("m2", "t2", True, 0)]
    fanout = {"results": [
        {"index": 0, "status": "success", "output": VERDICT, "cached": True},
    ]}

    report = _run(classify_post, _payload(tmp_path, stage1, fanout), monkeypatch, capsys)

    assert report["cost"]["classify"]["llm_calls"] == 0
    assert report["cost"]["classify"]["unpriced_calls"] == 0


def test_an_item_that_died_before_reporting_is_still_a_call_it_made(
    classify_post, tmp_path, monkeypatch, capsys
):
    """The STU-1016 shape: the provider declined, the call was made anyway, and a
    run priced only on what answered reads as a cheap run."""
    stage1 = [_record("m2", "t2", True, 0),
              _record("m3", "t3", True, 1)]
    fanout = {"results": [
        {"index": 0, "status": "success", "output": VERDICT, "token_usage": _usage(100)},
        {"index": 1, "status": "failed", "error": "session limit reached"},
    ]}

    report = _run(classify_post, _payload(tmp_path, stage1, fanout), monkeypatch, capsys)

    spend = report["cost"]["classify"]
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (2, 1)


def test_a_failed_item_is_priced_from_the_record_its_child_reported(
    classify_post, tmp_path, monkeypatch, capsys
):
    """STU-1210: `classify` is one agent stage, so the count only moves once RALPH
    retries against a refusing provider — three attempts, all of them owed."""
    stage1 = [_record("m3", "t3", True, 0)]
    fanout = {"results": [
        {"index": 0, "status": "failed", "error": "session limit reached",
         "stages": [{"stage": "classify", "status": "failed", "attempts": 3}]},
    ]}

    report = _run(classify_post, _payload(tmp_path, stage1, fanout), monkeypatch, capsys)

    spend = report["cost"]["classify"]
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (3, 3)


def test_a_dead_fanout_leaves_a_report_with_an_empty_leg(classify_post, tmp_path,
                                                         monkeypatch, capsys):
    """A tolerated failure still writes a report (STU-392). The `call` stage never
    produced output at all, so there are no item results to count — which is not
    the same as the items above, whose failures the stage did report."""
    stage1 = [_record("m2", "t2", True, 0)]

    report = _run(classify_post, _payload(tmp_path, stage1, None), monkeypatch, capsys)

    assert report["cost"]["classify"]["llm_calls"] == 0
    assert report["verdicts"][0]["error"] == "studio_run_output_missing"


def test_a_set_aside_conversation_is_an_error_verdict_and_no_call(
        classify_post, tmp_path, monkeypatch, capsys):
    """STU-2109: `classify-pre` kept it out of the fan-out, so it has no result index."""
    stage1 = [{**_record("m2", "t2", True), "set_aside": True},
              _record("m3", "t3", True, 0)]
    fanout = {"results": [{"index": 0, "status": "success", "output": VERDICT}]}

    report = _run(classify_post, _payload(tmp_path, stage1, fanout), monkeypatch, capsys)

    by_id = {v["message_id"]: v for v in report["verdicts"]}
    assert by_id["m2"]["status"] == "error"
    assert by_id["m2"]["error"] == "set_aside_after_repeated_failures"
    assert by_id["m3"]["error"] != "set_aside_after_repeated_failures"
    assert report["totals"]["llm_calls"] == 1
