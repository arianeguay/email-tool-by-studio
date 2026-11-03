import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from email_classifier import cost
from email_classifier.core.contract import Taxonomy
from email_classifier.core.types import EmailInput
from email_classifier.orchestrate.stage2_client import (
    PROVIDER_UNAVAILABLE,
    StudioCliClassifier,
    preflight_anonymize_scope,
)
from tests.conftest import pin_gliner

REPO_ROOT = Path(__file__).resolve().parent.parent
# The client's own taxonomy, so a verdict below is valid or invalid for the reason its
# test names under any deployment: `keep_visible` is not allowed for bucket C.
TAXONOMY = Taxonomy(
    buckets=("A", "C"),
    allowed_actions={"A": frozenset({"keep_visible", "needs_reply"}),
                     "C": frozenset({"label_archive", "mark_for_delete"})},
    allowed_subtypes={"A": frozenset({"human_real"}),
                      "C": frozenset({"product_newsletter"})},
    clamp={"A": "keep_visible"},
)


def _email(thread_id="t1"):
    return EmailInput(
        message_id=thread_id, thread_id=thread_id, from_address="x@y.com", subject="S",
        headers={}, body_snippet="b", date="2026-06-10T00:00:00+00:00",
    )


def _valid_output(thread_id="t1"):
    return {
        "thread_id": thread_id, "bucket": "C", "subtype": "product_newsletter",
        "confidence": "medium", "suggested_action": "label_archive",
        "suggested_label": "Archive/News", "reasoning": "promotional",
    }


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, completed):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed)


def test_success_returns_classification(monkeypatch):
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success", "output": _valid_output()}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.ok
    assert result.classification.bucket == "C"
    assert result.classification.suggested_action == "label_archive"


def _usage(model="claude-opus-4-8", **counts):
    return {"by_model": {model: counts}}


def _attempts(n):
    """The `attempts` a Studio >= 0.21.0 stage carries for a loop that made `n`."""
    return {"attempts": n}


def test_success_carries_what_the_child_run_spent(monkeypatch):
    """STU-1012: the counts come back with the verdict, so the run can record them
    instead of leaving them in a Studio log beside a worktree. STU-1244: as the
    per-stage record `cost.leg` prices, not as a flat sum."""
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success",
                    "output": _valid_output(), **_attempts(1),
                    "token_usage": _usage(completion_tokens=100, total_tokens=100)}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.spend == {"stages": [{
        "stage": "classify", "status": "success", "attempts": 1,
        "token_usage": {"by_model": {"claude-opus-4-8": {"completion_tokens": 100,
                                                         "total_tokens": 100}}},
    }]}


def test_every_stage_of_the_child_run_is_carried(monkeypatch):
    """A `call`/`map` stage carries its children and leaves them out of `stages`, so
    keeping every stage counts what the child run spent exactly once, whatever its
    shape. `leg` sums them; nothing here pre-sums, or the same tokens price twice."""
    payload = {
        "id": "r1", "status": "success",
        "stages": [
            {"stage_name": "prep", "status": "success", **_attempts(0),
             "token_usage": _usage(completion_tokens=10)},
            {"stage_name": "classify", "status": "success", "output": _valid_output(),
             **_attempts(1), "token_usage": _usage(completion_tokens=90)},
        ],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    spend = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email()).spend
    assert [s["stage"] for s in spend["stages"]] == ["prep", "classify"]
    assert cost.leg([spend])["by_model"] == {
        "claude-opus-4-8": {"completion_tokens": 100}
    }


def test_a_retried_stage_reports_its_attempts(monkeypatch):
    """STU-1244: a call is a RALPH attempt, the unit `classify-batch` reports, and
    the kernel's own `attempts` on the stage (STU-1287) is how many the loop made."""
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success",
                    "output": _valid_output(), **_attempts(3),
                    "token_usage": _usage(completion_tokens=90)}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    spend = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email()).spend
    assert spend["stages"][0]["attempts"] == 3
    priced = cost.leg([spend])
    assert priced["llm_calls"] == 3
    assert priced["unrecorded_items"] == 0


def test_a_stage_that_ran_no_agent_reports_no_attempts(monkeypatch):
    """0 for a stage carrying no agent run at all.

    Studio >= 0.20.2 reports 0 for a script stage (STU-1245), so `leg` needs no list of
    agent stages to keep it out of the call count.
    """
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "prep", "status": "success", "attempts": 0},
                   {"stage_name": "classify", "status": "success",
                    "output": _valid_output(), **_attempts(1)}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    spend = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email()).spend
    assert [s["attempts"] for s in spend["stages"]] == [0, 1]


def test_a_stage_without_attempts_falls_back_to_the_unrecorded_floor(monkeypatch):
    """A Studio below 0.21.0 writes no `attempts`. A record built without them would
    price as zero calls, a leg that looks free; no record is the floor, one call per
    escalation, with `unrecorded_items` saying it is not a count of attempts."""
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success",
                    "output": _valid_output(), "tasks": [{"agent_runs": [{"attempt": 3}]}],
                    "token_usage": _usage(completion_tokens=90)}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.spend is None
    priced = cost.leg([result.spend or {}])
    assert (priced["llm_calls"], priced["unrecorded_items"]) == (1, 1)


def test_a_child_that_reported_no_tokens_carries_its_stages_anyway(monkeypatch):
    """The record is what the attempts are counted from, so it is kept even when
    nothing priced. `leg` reports the call unpriced — not absent, and not free."""
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success",
                    "output": _valid_output(), **_attempts(2)}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    spend = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email()).spend
    assert "token_usage" not in spend["stages"][0]
    priced = cost.leg([spend])
    assert (priced["llm_calls"], priced["unpriced_calls"]) == (2, 2)


def test_a_child_that_left_no_stages_carries_none(monkeypatch):
    """None and not an empty record: an empty one prices as zero calls, where the
    floor `leg` applies to a recordless item is one, counted in `unrecorded_items`."""
    payload = {"id": "r1", "status": "success", "stages": []}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    assert StudioCliClassifier(taxonomy=TAXONOMY).classify(_email()).spend is None


def test_nonzero_exit_is_studio_run_failed(monkeypatch):
    _patch_run(monkeypatch, _FakeCompleted(1, "", "boom"))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert not result.ok
    assert result.error == "studio_run_failed"


def test_unparseable_stdout_is_json_parse_error(monkeypatch):
    _patch_run(monkeypatch, _FakeCompleted(0, "not json at all"))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_output_json_parse_error"


def test_no_successful_stage_is_output_missing(monkeypatch):
    payload = {"id": "r1", "stages": [{"stage_name": "classify", "status": "failed"}]}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_output_missing"


def test_output_that_refails_validation_is_output_invalid(monkeypatch):
    bad = _valid_output()
    bad["suggested_action"] = "keep_visible"  # not allowed for bucket C (INV-08)
    payload = {"id": "r1", "stages": [{"stage_name": "classify", "status": "success", "output": bad}]}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_output_invalid"


def test_a_failed_run_is_priced_from_the_record_it_still_printed(monkeypatch):
    """STU-1244: the CLI prints the whole record and *then* exits 1, so a failed run
    has attempts to price. This is the STU-1016 shape — the provider answers with a
    notice, RALPH retries twice against the same wall, the stage fails on the
    contract — and pricing it as one call loses two thirds of what it burned."""
    payload = {
        "id": "r1", "status": "failed",
        "stages": [{"stage_name": "classify", "status": "failed",
                    **_attempts(3),
                    "token_usage": _usage(prompt_tokens=300)}],
    }
    _patch_run(monkeypatch, _FakeCompleted(1, json.dumps(payload), "boom"))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_failed"
    priced = cost.leg([result.spend])
    assert priced["llm_calls"] == 3
    assert priced["unrecorded_items"] == 0
    assert priced["by_model"]["claude-opus-4-8"]["prompt_tokens"] == 300


def test_a_provider_notice_still_carries_its_attempts(monkeypatch):
    """The failure worth counting most (STU-1016): the notice is the agent's own
    answer, so the child burned every attempt before the stage failed."""
    payload = {
        "id": "r1", "status": "failed",
        "stages": [{"stage_name": "classify", "status": "failed",
                    **_attempts(3),
                    "output": "You've hit your session limit"}],
    }
    _patch_run(monkeypatch, _FakeCompleted(1, json.dumps(payload)))
    classifier = StudioCliClassifier(taxonomy=TAXONOMY, alert_on_provider_failure=False)
    result = classifier.classify(_email())
    assert result.error == "provider_unavailable"
    assert cost.leg([result.spend])["llm_calls"] == 3


def test_no_successful_stage_is_priced_from_the_payload_in_hand(monkeypatch):
    payload = {"id": "r1", "stages": [{"stage_name": "classify", "status": "failed",
                                       **_attempts(2)}]}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_output_missing"
    assert cost.leg([result.spend])["llm_calls"] == 2


def test_an_output_that_refailed_validation_is_still_priced(monkeypatch):
    """The call was made and billed; only its answer was rejected."""
    bad = _valid_output()
    bad["suggested_action"] = "keep_visible"
    payload = {"id": "r1", "stages": [{"stage_name": "classify", "status": "success",
                                       "output": bad, **_attempts(1)}]}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_output_invalid"
    assert result.spend["stages"][0]["attempts"] == 1


@pytest.mark.parametrize("completed", [
    _FakeCompleted(0, "not json at all"),
    _FakeCompleted(1, "", "boom"),
])
def test_a_failure_with_no_record_falls_to_the_floor(monkeypatch, completed):
    """The half that stays on the floor, and says so: an unparseable stdout and a
    throw before the engine ran leave nothing to read. `leg` prices those as one
    unrecorded item each rather than as zero — a call that reported nothing is not
    a call nobody made."""
    _patch_run(monkeypatch, completed)
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.spend is None
    priced = cost.leg([result.spend or {}])
    assert (priced["llm_calls"], priced["unrecorded_items"]) == (1, 1)


def test_both_orchestrations_price_one_child_the_same(monkeypatch):
    """The end state of STU-1244, as far as a test without a mailbox reaches: one
    corpus reports the same `llm_calls` whichever orchestration ran it.

    `classify-batch` hands `leg` the record its `map:` item carried, built by the
    kernel's spawner; the subprocess path hands it the one derived here from the same
    child's `--json`. Given one child, the two legs must be the same object — the keys
    included, since a rename on either side is exactly what would part them again
    while both still looked like a record.

    The live half of the verification (`make run` against `make run-batch` over one
    frozen dump) is `claude:local` and costs a stage-2 pass per conversation; this
    pins the mechanism the two share.
    """
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success",
                    "output": _valid_output(), **_attempts(3),
                    "token_usage": _usage(prompt_tokens=90)}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    subprocess_item = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email()).spend

    # What the same child reaches `classify-post` as when it is a `map:` item.
    map_item = {"index": 0, "status": "success",
                "stages": [{"stage": "classify", "status": "success", "attempts": 3,
                            "token_usage": _usage(prompt_tokens=90)}]}

    assert subprocess_item["stages"] == map_item["stages"]
    assert (cost.leg([subprocess_item])
            == cost.leg([map_item]))


def test_missing_cli_is_studio_cli_missing(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", _raise)
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_cli_missing"


def test_timeout_is_studio_run_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="studio", timeout=1)
    monkeypatch.setattr(subprocess, "run", _raise)
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_timeout"


def test_provider_flag_is_passed_to_command(monkeypatch):
    captured = {}

    def _capture(cmd, *a, **k):
        captured["cmd"] = cmd
        payload = {"id": "r1", "stages": [
            {"stage_name": "classify", "status": "success", "output": _valid_output()}]}
        return _FakeCompleted(0, json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", _capture)
    StudioCliClassifier(taxonomy=TAXONOMY, provider="mock").classify(_email())
    assert "--provider" in captured["cmd"]
    assert "mock" in captured["cmd"]
    assert captured["cmd"][:3] == ["studio", "run", "classify"]


def test_failure_logs_studio_stderr(monkeypatch, caplog):
    import logging

    _patch_run(monkeypatch, _FakeCompleted(1, "", "boom-from-studio"))
    with caplog.at_level(logging.WARNING, logger="email_classifier.stage2"):
        result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_failed"
    assert "boom-from-studio" in caplog.text  # studio's stderr is surfaced, not swallowed


def test_timeout_logs_partial_output(monkeypatch, caplog):
    import logging

    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(
            cmd="studio", timeout=1, output="partial-stdout", stderr="partial-stderr"
        )

    monkeypatch.setattr(subprocess, "run", _raise)
    with caplog.at_level(logging.WARNING, logger="email_classifier.stage2"):
        result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_timeout"
    # the hang case must surface whatever studio/claude emitted before the kill
    assert "partial-stderr" in caplog.text
    assert "partial-stdout" in caplog.text


def test_failure_carries_stderr_tail_and_rc_in_error_detail(monkeypatch):
    _patch_run(monkeypatch, _FakeCompleted(2, "some-stdout", "boom-from-studio"))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_failed"
    assert "rc=2" in result.error_detail
    assert "boom-from-studio" in result.error_detail
    assert "some-stdout" in result.error_detail


def test_timeout_carries_partial_output_in_error_detail(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(
            cmd="studio", timeout=1, output="partial-stdout", stderr="partial-stderr"
        )

    monkeypatch.setattr(subprocess, "run", _raise)
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_run_timeout"
    assert "partial-stderr" in result.error_detail
    assert "partial-stdout" in result.error_detail


def test_parse_error_carries_stdout_tail_in_error_detail(monkeypatch):
    _patch_run(monkeypatch, _FakeCompleted(0, "not json at all", "warn-line"))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "studio_output_json_parse_error"
    assert "not json at all" in result.error_detail
    assert "warn-line" in result.error_detail


def _capture_call(monkeypatch, completed):
    """Records the studio command and the input YAML as written on disk."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = yaml.safe_load(Path(cmd[cmd.index("--input-file") + 1]).read_text())
        return completed

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def _ok_completed():
    return _FakeCompleted(0, json.dumps({
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success", "output": _valid_output()}],
    }))


def test_anonymize_fields_are_declared_to_studio(monkeypatch):
    captured = _capture_call(monkeypatch, _ok_completed())
    StudioCliClassifier(taxonomy=TAXONOMY, anonymize_fields=["body_snippet"]).classify(_email())
    assert "--anonymize" in captured["cmd"]
    # The transcript rides in the scope the bodies it is made of ride in (STU-944).
    assert captured["input"]["anonymize_fields"] == ["body_snippet", "thread_history"]
    assert captured["input"]["body_snippet"] == "b"  # sent cleartext; Studio tokenizes


def test_the_conversation_reaches_the_child_as_a_flat_field(monkeypatch):
    captured = _capture_call(monkeypatch, _ok_completed())
    StudioCliClassifier(taxonomy=TAXONOMY).classify(_email(), "[2026-06-01T00:00:00+00:00] p@x.test\nsalut")
    assert captured["input"]["thread_history"] == "[2026-06-01T00:00:00+00:00] p@x.test\nsalut"


def test_no_anonymization_without_scope(monkeypatch):
    captured = _capture_call(monkeypatch, _ok_completed())
    StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert "--anonymize" not in captured["cmd"]
    assert "anonymize_fields" not in captured["input"]


def _patch_version(monkeypatch, completed):
    """Stubs `studio --version` only; any other subprocess call is a test bug."""
    def fake_run(cmd, **kwargs):
        assert cmd == ["studio", "--version"], cmd
        return completed

    monkeypatch.setattr(subprocess, "run", fake_run)
    pin_gliner(monkeypatch)


def test_preflight_passes_without_a_scope(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no probe expected"))
    assert preflight_anonymize_scope([]) is None


def test_preflight_passes_on_a_studio_that_honours_the_scope(monkeypatch):
    _patch_version(monkeypatch, _FakeCompleted(0, "0.16.0\n"))
    assert preflight_anonymize_scope(["body_snippet"]) is None


def test_preflight_rejects_a_studio_predating_the_scope_plumbing(monkeypatch):
    _patch_version(monkeypatch, _FakeCompleted(0, "0.15.1\n"))
    error = preflight_anonymize_scope(["body_snippet"])
    assert "0.15.1" in error and "0.16.0" in error


def test_preflight_rejects_an_unreadable_version(monkeypatch):
    _patch_version(monkeypatch, _FakeCompleted(1, "", "boom"))
    assert "cannot read" in preflight_anonymize_scope(["body_snippet"])


def test_preflight_rejects_a_studio_that_cannot_be_launched(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(subprocess, "run", fake_run)
    pin_gliner(monkeypatch)
    assert "cannot read" in preflight_anonymize_scope(["body_snippet"])


def test_preflight_rejects_a_scope_with_no_name_detector_installed(monkeypatch):
    # Without this the refusal fires in `classify-pre`, once the mailbox is fetched and
    # a run id is open (STU-908). No version probe: the extra decides it on its own.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no probe expected"))
    pin_gliner(monkeypatch, present=False)
    assert "ner" in preflight_anonymize_scope(["body_snippet"])


# --- no mail in the failure log (STU-1010) -----------------------------------------


_BODY = (
    "Bonjour, ma fille Camille Tremblay est inscrite au U15 et je voulais savoir\n"
    "si la pratique de samedi est annulée. Merci!"
)
_SENDER = "genevieve.tremblay@example.test"
_SUBJECT = "Question sur la pratique de samedi"


def _parent_email():
    return EmailInput(
        message_id="m9", thread_id="t9", from_address=_SENDER, subject=_SUBJECT,
        headers={"Reply-To": _SENDER}, body_snippet=_BODY,
        date="2026-06-10T00:00:00+00:00",
    )


def _run_record(email, status="failed", stage_error="anthropic 529 overloaded"):
    """A studio `--json` record shaped like the real one: it echoes the input back."""
    return {
        "id": "r9", "status": status,
        "input": {
            "thread_id": email.thread_id, "from_address": email.from_address,
            "subject": email.subject, "headers": email.headers,
            "body_snippet": email.body_snippet, "thread_history": "",
        },
        "stages": [{"stage_name": "classify", "status": "failed", "error": stage_error}],
    }


def _assert_no_mail(text, email):
    for value in (email.body_snippet, email.from_address, email.subject):
        assert value not in text
        for line in value.splitlines():
            assert line not in text


def test_failure_log_carries_the_error_but_none_of_the_mail(monkeypatch, caplog):
    import logging

    email = _parent_email()
    _patch_run(monkeypatch, _FakeCompleted(
        1, json.dumps(_run_record(email)), "studio.errors.ProviderError: 529 overloaded"))
    with caplog.at_level(logging.WARNING, logger="email_classifier.stage2"):
        result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    assert result.error == "studio_run_failed"
    _assert_no_mail(caplog.text, email)
    _assert_no_mail(result.error_detail, email)
    # still as diagnosable as it was (STU-820): the error, the rc, the failing stage
    assert "ProviderError: 529 overloaded" in caplog.text
    assert "anthropic 529 overloaded" in caplog.text
    assert "rc=1" in result.error_detail
    assert "stage[classify]" in result.error_detail
    assert "t9" in caplog.text  # the thread handle stays: it names a conversation


def test_an_empty_stderr_failure_carries_the_agent_runs_error_but_none_of_the_mail(
    monkeypatch,
):
    """The shape the serving host stored for five escalations (STU-1497): the stage has no error
    of its own, and the cause sits on its agent run, next to an output that echoes mail."""
    email = _parent_email()
    record = _run_record(email, stage_error=None)
    record["stages"][0]["tasks"] = [{"task_name": "classify", "agent_runs": [
        {"agent_name": "classifier", "attempt": 1, "status": "failed", "tool_calls": 0,
         "output": {"body": email.body_snippet, "from": email.from_address},
         "error": 'Failed to spawn claude: Executable not found in $PATH: "claude". '
                  f"Is Claude Code installed? (subject: {email.subject})"},
    ]}]
    _patch_run(monkeypatch, _FakeCompleted(1, json.dumps(record), ""))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    assert result.error == "studio_run_failed"
    assert "stderr= |" in result.error_detail
    assert "agent[classifier#1] status=failed error=Failed to spawn claude" in (
        result.error_detail)
    _assert_no_mail(result.error_detail, email)


def test_a_run_record_cut_off_anywhere_still_loses_the_mail(monkeypatch, caplog):
    """A killed studio leaves its JSON half-written — and half a body is still a body."""
    import logging

    email = _parent_email()
    record = json.dumps(_run_record(email))
    # Every cut point, not one: the kill lands where it lands, and a truncation inside
    # `headers` leaks the sender as surely as one inside `body_snippet` leaks the body.
    for cut in range(1, len(record) + 1):
        def _raise(*a, _out=record[:cut], **k):
            raise subprocess.TimeoutExpired(
                cmd="studio", timeout=1, output=_out, stderr="killed after 180s")

        monkeypatch.setattr(subprocess, "run", _raise)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="email_classifier.stage2"):
            result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

        assert result.error == "studio_run_timeout"
        _assert_no_mail(caplog.text, email)
        _assert_no_mail(result.error_detail, email)
        assert "killed after 180s" in caplog.text


def test_a_stderr_quoting_the_input_is_scrubbed(monkeypatch, caplog):
    import logging

    email = _parent_email()
    trace = (
        "Traceback (most recent call last):\n"
        f"  ValidationError: body_snippet={email.body_snippet!r} from={email.from_address}"
    )
    _patch_run(monkeypatch, _FakeCompleted(1, "", trace))
    with caplog.at_level(logging.WARNING, logger="email_classifier.stage2"):
        result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    assert result.error == "studio_run_failed"
    _assert_no_mail(caplog.text, email)
    assert "ValidationError" in caplog.text


def test_unparseable_stdout_that_is_not_a_record_is_still_surfaced(monkeypatch):
    email = _parent_email()
    _patch_run(monkeypatch, _FakeCompleted(0, "usage: studio run [-h] PIPELINE", "warn"))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)
    assert result.error == "studio_output_json_parse_error"
    assert "usage: studio run" in result.error_detail  # STU-820: never a black box


def test_the_thread_transcript_is_scrubbed_too(monkeypatch, caplog):
    import logging

    email = _parent_email()
    history = "[2026-06-09T00:00:00+00:00] parent\nEt pour le tournoi de Sherbrooke?"
    record = _run_record(email)
    record["input"]["thread_history"] = history
    _patch_run(monkeypatch, _FakeCompleted(1, json.dumps(record), "boom"))
    with caplog.at_level(logging.WARNING, logger="email_classifier.stage2"):
        StudioCliClassifier(taxonomy=TAXONOMY).classify(email, history)
    assert "Sherbrooke" not in caplog.text


# --- a failure the provider caused, not the contract (STU-1016) ----------------------


_LIMIT_NOTICE = "You've hit your session limit · resets 6:50am (UTC)"


def _limit_record(email):
    """The run record the serving host produced under a spent session limit (run 119a588c).

    RALPH read a string where the contract wants an object, retried twice against the
    same wall, and the stage failed — so the notice arrives as an ordinary contract
    violation, with the CLI's answer sitting in `agent_output_raw`.
    """
    record = _run_record(email, stage_error="Expected object output, got string")
    record["stages"][0].update({
        "failures": [
            "Expected object output, got string",
            "validator 'classification' produced unparseable output: ...",
        ],
        "agent_output_raw": _LIMIT_NOTICE,
        "tool_calls_count": 0,
        "total_tokens": 0,
    })
    return record


@pytest.fixture
def alert_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "email_classifier.orchestrate.stage2_client.alerts.notify", calls.append
    )
    return calls


def test_a_spent_session_limit_is_named_and_alerted(monkeypatch, alert_calls):
    email = _parent_email()
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(_limit_record(email))))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    # Reported as the provider's fault, not as the contract violation it arrived as.
    assert result.error == "provider_unavailable"
    assert result.error_detail == _LIMIT_NOTICE
    assert len(alert_calls) == 1
    assert _LIMIT_NOTICE in alert_calls[0]


def test_the_alert_and_the_detail_carry_no_mail(monkeypatch, alert_calls):
    """The notice is quoted; the record it was found in echoed the whole email back."""
    email = _parent_email()
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(_limit_record(email))))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    _assert_no_mail(alert_calls[0], email)
    _assert_no_mail(result.error_detail, email)


def test_a_nonzero_exit_printing_the_notice_is_named_too(monkeypatch, alert_calls):
    """The limit does not always let the run record get written."""
    _patch_run(monkeypatch, _FakeCompleted(1, "", _LIMIT_NOTICE))
    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert result.error == "provider_unavailable"
    assert len(alert_calls) == 1


def test_one_alert_per_run_not_per_unclassified_email(monkeypatch, alert_calls):
    """A spent limit answers every conversation of the run the same way."""
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(_limit_record(_parent_email()))))
    classifier = StudioCliClassifier(taxonomy=TAXONOMY)

    for thread in ("t1", "t2", "t3"):
        assert classifier.classify(_email(thread)).error == "provider_unavailable"

    assert len(alert_calls) == 1
    # A limit still standing at the next run announces itself again: that silence is new.
    StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert len(alert_calls) == 2


def test_an_ordinary_stage_failure_is_still_a_contract_violation(monkeypatch, alert_calls):
    """The alert must stay rare enough to mean something: no notice, no alert."""
    email = _parent_email()
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(_run_record(email))))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    assert result.error == "studio_run_output_missing"
    assert alert_calls == []


def test_mail_quoting_a_limit_notice_does_not_raise_one(monkeypatch, alert_calls):
    """A parent forwarding the notice is mail, not the provider answering."""
    email = replace(_parent_email(), body_snippet=f"Est-ce normal? « {_LIMIT_NOTICE} »")
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(_run_record(email))))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    assert result.error == "studio_run_output_missing"
    assert alert_calls == []


def test_a_fired_agent_timeout_is_not_a_provider_outage(monkeypatch, alert_calls):
    """`timeout_ms` on the classify stage (STU-1495) fails an attempt with the runner's
    own message. It is a wedged child, not the provider saying "not right now", so it
    must not page as an outage."""
    email = _parent_email()
    record = _run_record(email, stage_error="agent run failed")
    record["stages"][0]["agent_runs"] = [
        {"agent_name": "classifier", "attempt": 1, "status": "failed",
         "error": "Agent timed out after 180000ms"},
    ]
    _patch_run(monkeypatch, _FakeCompleted(1, json.dumps(record)))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    assert result.error != PROVIDER_UNAVAILABLE
    assert alert_calls == []


# The `anthropic` provider's shape of the same fault (STU-1013), copied from the record
# `studio run classify` printed under an invalid key. The CLI answers in prose; the API
# raises, and the engine records it on the agent run. Held unescaped here because
# `--json` is what escapes it once, and `json.dumps` below stands in for that.
_API_REFUSAL = (
    '401 {"type":"error","error":{"type":"authentication_error",'
    '"message":"API key is invalid."},"request_id":null}'
)


def _api_error_record(email):
    record = _run_record(email, stage_error="agent run failed")
    record["stages"][0]["agent_runs"] = [
        {"agent_name": "classifier", "attempt": 1, "status": "failed",
         "error": _API_REFUSAL},
    ]
    return record


def test_a_dead_api_key_is_the_provider_refusing_not_a_contract_violation(
    monkeypatch, alert_calls
):
    """Switching the classifier to `anthropic` moved this fault from the agent's output
    to the agent run's error, and RALPH does not retry it — so without this the webhook
    logged and dropped every escalated conversation in silence."""
    email = _parent_email()
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(_api_error_record(email))))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(email)

    assert result.error == "provider_unavailable"
    # The status and the type, never the message: the shape is what was measured.
    assert result.error_detail == "HTTP 401 authentication_error"
    assert len(alert_calls) == 1
    assert "HTTP 401 authentication_error" in alert_calls[0]
    _assert_no_mail(alert_calls[0], email)


def test_a_stage_error_naming_a_status_without_the_envelope_is_not_one(
    monkeypatch, alert_calls
):
    """`_run_record`'s own default reads like a provider fault and is not shaped like
    one: matching prose about a status is how a real contract bug gets filed as a wait."""
    email = _parent_email()
    _patch_run(monkeypatch, _FakeCompleted(
        0, json.dumps(_run_record(email, stage_error="anthropic 529 overloaded"))))

    assert StudioCliClassifier(taxonomy=TAXONOMY).classify(email).error == "studio_run_output_missing"
    assert alert_calls == []


def test_a_bad_classification_is_not_read_as_a_provider_notice(monkeypatch, alert_calls):
    """A well-shaped object came back: the only free text left is the model's prose."""
    bad = _valid_output()
    bad["suggested_action"] = "keep_visible"  # not allowed for bucket C (INV-08)
    bad["reasoning"] = f"the sender writes that they hit your session limit"
    payload = {"id": "r1", "stages": [
        {"stage_name": "classify", "status": "success", "output": bad}]}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))

    result = StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())

    assert result.error == "studio_run_output_invalid"
    assert alert_calls == []


def _capture_env(monkeypatch, captured):
    def _run(cmd, *a, **k):
        captured["env"] = k.get("env")
        payload = {"id": "r1", "stages": [
            {"stage_name": "classify", "status": "success", "output": _valid_output()}]}
        return _FakeCompleted(0, json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", _run)


def test_a_model_reaches_the_child_as_the_env_the_agent_interpolates(monkeypatch):
    """`classifier.agent.yaml` reads its `model` from `${CLASSIFY_MODEL}` (STU-1045)."""
    captured = {}
    _capture_env(monkeypatch, captured)
    StudioCliClassifier(taxonomy=TAXONOMY, model="claude-haiku-4-5").classify(_email())
    assert captured["env"]["CLASSIFY_MODEL"] == "claude-haiku-4-5"


def test_no_model_leaves_the_child_env_alone(monkeypatch):
    """Unset, the agent's `${CLASSIFY_MODEL}` resolves to '' and the engine falls back to
    `defaults.model` — production never names a model at the classifier."""
    captured = {}
    _capture_env(monkeypatch, captured)
    StudioCliClassifier(taxonomy=TAXONOMY).classify(_email())
    assert captured["env"] is None


def test_a_harness_arm_does_not_page_when_the_provider_refuses(monkeypatch):
    """The alert says escalated mail is going unclassified — true of a run, false of a
    bake-off deliberately pointing an unvetted model at committed fixtures (STU-1045)."""
    from email_classifier import alerts

    sent = []
    monkeypatch.setattr(alerts, "notify", lambda message: sent.append(message))
    _patch_run(monkeypatch, _FakeCompleted(1, "you have hit your session limit", ""))
    result = StudioCliClassifier(taxonomy=TAXONOMY, alert_on_provider_failure=False).classify(_email())
    assert result.error == "provider_unavailable"
    assert sent == []


def test_a_run_still_pages_when_the_provider_refuses(monkeypatch):
    from email_classifier import alerts

    sent = []
    monkeypatch.setattr(alerts, "notify", lambda message: sent.append(message))
    _patch_run(monkeypatch, _FakeCompleted(1, "you have hit your session limit", ""))
    assert StudioCliClassifier(taxonomy=TAXONOMY).classify(_email()).error == "provider_unavailable"
    assert len(sent) == 1
