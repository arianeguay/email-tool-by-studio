import json

from email_classifier.orchestrate.report import (
    EmailVerdict,
    build_report,
    render_human,
    report_to_dict,
)


def _det(id_, bucket="A", action="keep_visible"):
    return EmailVerdict(
        message_id=id_, thread_id=id_, path="deterministic", status="ok", bucket=bucket,
        suggested_action=action, confidence="high", rule_id="some_rule",
        subtype=None, reasoning=None, error=None,
    )


def _llm(id_, bucket="C", action="label_archive", thread_id=None):
    return EmailVerdict(
        message_id=id_, thread_id=thread_id or id_, path="llm", status="ok", bucket=bucket,
        suggested_action=action, confidence="medium", rule_id="catch_all",
        subtype="newsletter", reasoning="looks promotional", error=None,
    )


def _llm_err(id_, error="studio_run_timeout"):
    return EmailVerdict(
        message_id=id_, thread_id=id_, path="llm", status="error", bucket=None,
        suggested_action=None, confidence=None, rule_id="catch_all",
        subtype=None, reasoning=None, error=error,
    )


def test_totals_and_ratio():
    report = build_report("run1", [_det("a"), _det("b"), _det("c"), _llm("d")])
    assert report.totals == {
        "emails": 4, "deterministic": 3, "llm": 1, "llm_calls": 1, "errors": 0,
    }
    assert report.ratio_deterministic == 0.75


def test_by_bucket_and_action_count_ok_verdicts_only():
    verdicts = [
        _det("a", bucket="A", action="keep_visible"),
        _det("b", bucket="A", action="keep_visible"),
        _llm("c", bucket="C", action="label_archive"),
        _llm_err("d"),
    ]
    report = build_report("run1", verdicts)
    assert report.by_bucket == {"A": 2, "C": 1}
    assert report.by_action == {"keep_visible": 2, "label_archive": 1}
    assert report.totals["errors"] == 1


def test_empty_ratio_is_zero():
    report = build_report("run1", [])
    assert report.ratio_deterministic == 0.0
    assert report.totals == {
        "emails": 0, "deterministic": 0, "llm": 0, "llm_calls": 0, "errors": 0,
    }


def test_report_to_dict_is_json_serializable():
    report = build_report("run1", [_det("a"), _llm("b")])
    payload = report_to_dict(report)
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["run_id"] == "run1"
    assert len(payload["verdicts"]) == 2
    assert payload["verdicts"][0]["path"] == "deterministic"


def test_render_human_shows_ratio_and_escalated_lines():
    report = build_report("abc", [_det("a"), _det("b"), _llm("c"), _llm_err("d")])
    text = render_human(report)
    assert "run abc" in text
    assert "(50%)" in text                 # 2 of 4 deterministic
    assert "c   catch_all -> LLM -> C / label_archive" in text
    assert "ERROR (studio_run_timeout)" in text


def test_provenance_travels_to_the_dict_and_the_human_render():
    provenance = {
        "provider": "mock", "git_commit": "abcdef1234567890", "git_dirty": True,
        "rules_sha256": "0123456789abcdef",
    }
    report = build_report("run1", [_det("a")], provenance)
    assert report_to_dict(report)["provenance"] == provenance
    assert "Provider: mock | commit: abcdef12 +dirty" in render_human(report)


def test_render_human_marks_a_report_without_provenance():
    assert "Provenance: (unrecorded)" in render_human(build_report("run1", [_det("a")]))


def test_composition_travels_to_the_dict_and_the_human_render():
    composition = {
        "unsubscribable": 1, "recurring": 1, "addressable": 2, "ratio_addressable": 0.5,
    }
    report = build_report("run1", [_det("a"), _llm("b")], None, composition)
    assert report_to_dict(report)["composition"] == composition
    assert (
        "Addressable: 2/2 (unsubscribable 1, recurring sender 1) | coverage 50%"
        in render_human(report)
    )


def test_render_human_omits_the_composition_line_when_unmeasured():
    assert "Addressable:" not in render_human(build_report("run1", [_det("a")]))


def test_llm_calls_counts_conversations_not_escalated_messages():
    """Three escalated messages on two threads are two stage-2 calls (STU-944)."""
    report = build_report("run1", [
        _det("a"),
        _llm("b", thread_id="t1"),
        _llm("c", thread_id="t1"),
        _llm("d", thread_id="t2"),
    ])
    assert report.totals["llm"] == 3
    assert report.totals["llm_calls"] == 2
    assert report_to_dict(report)["totals"]["llm_calls"] == 2


def test_llm_calls_counts_a_call_that_errored():
    """The call was made either way — an error tag is not a refund."""
    report = build_report("run1", [_llm_err("a")])
    assert report.totals["llm_calls"] == 1


def test_render_human_separates_calls_from_escalated_messages():
    report = build_report("abc", [
        _det("a"), _llm("b", thread_id="t1"), _llm("c", thread_id="t1"),
    ])
    text = render_human(report)
    assert "Stage 2 calls: 1 (one per conversation) | escalated messages: 2" in text
    assert "3 messages | 1 deterministic (33% of messages)" in text
