"""Settling anonymization recall on a run with no input artifact (STU-1036).

The prescribed method used to be a replay of `provenance.input_path`. A push run
records `webhook:push-notification` and has no input on any disk, so on the path that
writes most reports the check could not run at all. What the run sends is recorded
instead, and the audit reads that.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from email_classifier.anonymize import load_sent, record_sent, sent_path
from email_classifier.audit import payload_text, recall, studio_payloads, studio_thread_id
from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "audit_run.py"
RUN = "5927bafb-2df7-4b25-8ec5-4094cc372f60"


def payload(**over) -> dict:
    return {
        "thread_id": "AAQkAGVjY2Mw",
        "from_address": "EMAIL_1",
        "subject": "Possible de payer en deux versements?",
        "headers": {},
        "body_snippet": "Bonjour,\n\nPERSON_5 s'inscrit au U15.\n\nPERSON_4\n",
        "date": "2026-08-28T20:25:28+00:00",
        "provider_category": "primary",
        "thread_history": "",
        **over,
    }


# --- the record itself -----------------------------------------------------------


def test_the_run_records_what_it_sent_beside_its_own_keymap(tmp_path):
    keymap = tmp_path / "logs" / "anonymization" / "keymap.json"
    path = sent_path(keymap, RUN)
    assert path.name == f"{RUN}-sent.jsonl"
    assert path.parent == keymap.parent


def test_two_stages_under_one_run_id_both_land_in_it(tmp_path):
    """`mailbox-full` tokenizes twice under one run id; the run is their union."""
    path = tmp_path / f"{RUN}-sent.jsonl"
    record_sent(path, [payload(subject="classify")])
    record_sent(path, [payload(subject="drafts")])
    assert [p["subject"] for p in load_sent(path)] == ["classify", "drafts"]


def test_a_stage_that_sent_nothing_still_writes_the_file(tmp_path):
    """Clean and unknown are different answers; without the file they are one absence."""
    path = tmp_path / f"{RUN}-sent.jsonl"
    record_sent(path, [])
    assert load_sent(path) == []


def test_a_write_failure_does_not_kill_the_run_that_paid_for_the_payloads(tmp_path):
    """`pipeline.run` records after the escalation loop — the spend is already out."""
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    record_sent(blocked / "logs" / f"{RUN}-sent.jsonl", [payload()])


# --- what the scan reads ---------------------------------------------------------


def test_identifiers_are_not_scanned_as_text():
    """A Graph thread handle and a prompt hash are not values a scope ever claimed."""
    text = payload_text(payload(prompt_fingerprint="deadbeef", run_id=RUN))
    assert "AAQkAGVjY2Mw" not in text
    assert "deadbeef" not in text
    assert "PERSON_5" in text


def test_a_nested_value_is_scanned_too():
    """A leak that only a header carries is still a leak."""
    text = payload_text(payload(headers={"Reply-To": "parent@example.com"}))
    assert "parent@example.com" in text


# --- the two readings ------------------------------------------------------------


def test_a_clean_payload_reaches_a_verdict_with_zero_minted_entries():
    """The hole the 5927bafb audit hit: every value carried in from an earlier round.

    A minted-entry count of zero is a legitimate run, and matching it against a replay
    proved nothing. The scan does not consult it.
    """
    result = recall([payload()], {"PERSON_4": "Marie Tremblay", "PERSON_5": "Alicia"})
    assert result["leaks"] == 0
    assert result["keymap_values"] == 2
    assert result["payloads"] == 1


def test_a_format_pattern_survivor_is_a_leak():
    result = recall([payload(body_snippet="joignable au 514-555-0142")], {})
    assert result["format_hits"]["phone"] == ["514-555-0142"]
    assert result["leaks"] == 1


def test_an_antispam_code_list_is_not_a_phone():
    """STU-1994: run `7491c521` read a BCL code as a phone number."""
    result = recall([payload(headers={
        "x-microsoft-antispam": "BCL:0;ARA:13230040|18002099003|376014|1800799024;",
    })], {})
    assert result["format_hits"] == {}
    assert result["leaks"] == 0


def test_a_role_address_is_not_a_leak():
    """`is_machine_address` lets one through by design (STU-902)."""
    result = recall([payload(from_address="no-reply@club.example")], {})
    assert result["format_hits"] == {}


def test_a_name_left_in_clear_is_caught_by_the_keymap_reading_alone():
    """No format pattern covers a person's name — the other reading is why it exists."""
    result = recall([payload(body_snippet="Merci, Marie Tremblay")],
                    {"PERSON_4": "Marie Tremblay"})
    assert result["format_hits"] == {}
    assert result["leaks"] == 1
    [hit] = result["keymap_values_in_clear"]
    assert (hit["token"], hit["value"], hit["occurrences"]) == ("PERSON_4", "Marie Tremblay", 1)
    assert "Merci, Marie Tremblay" in hit["excerpt"]


def test_a_value_inside_a_longer_word_is_not_a_hit():
    """The tokenizer matches a keymap value on whole words (STU-1046), so a substring is
    text it never had a chance to redact. Measured on run `1452f339` (the serving host,
    2026-09-01): `sam` inside `samedi` was one of that run's two reported hits, and
    nothing leaked.
    """
    result = recall(
        [payload(body_snippet="se joindre aux entraînements du samedi matin")],
        {"PERSON_66": "sam"},
    )
    assert result["keymap_values_in_clear"] == []
    assert result["leaks"] == 0


def test_the_match_is_case_sensitive_like_the_rule_it_audits():
    """`marc` the ordinary noun is not `Marc` the person — the reason the tokenizer's
    rule is case-sensitive, applied to the reading that audits it."""
    result = recall([payload(body_snippet="Sam viendra")], {"PERSON_66": "sam"})
    assert result["keymap_values_in_clear"] == []


def test_a_value_standing_as_its_own_word_is_still_a_hit():
    """The half that must not be lost: over-reporting was wrong, under-reporting is worse."""
    result = recall([payload(body_snippet="Merci, sam")], {"PERSON_66": "sam"})
    [hit] = result["keymap_values_in_clear"]
    assert (hit["token"], hit["value"], hit["occurrences"]) == ("PERSON_66", "sam", 1)


def test_the_excerpt_surrounds_the_whole_word_hit_and_not_an_earlier_substring():
    """`blob.index` would walk back to the first substring occurrence and print an
    excerpt around a position the hit was not at."""
    result = recall(
        [payload(body_snippet="Le samedi, puis: Merci, sam et bonne fin de semaine")],
        {"PERSON_66": "sam"},
    )
    [hit] = result["keymap_values_in_clear"]
    assert hit["occurrences"] == 1
    assert "Merci, sam" in hit["excerpt"]


def test_a_keymap_value_from_another_run_that_never_appears_is_not_reported():
    result = recall([payload()], {"PERSON_9": "Someone Else"})
    assert result["keymap_values_in_clear"] == []


def test_a_role_address_in_the_keymap_is_not_a_leak_either():
    """The same STU-902 rule the format reading applies, applied to this one."""
    result = recall([payload(from_address="no-reply@club.example")],
                    {"EMAIL_35": "no-reply@club.example"})
    assert result["keymap_values_in_clear"] == []
    assert result["leaks"] == 0


def test_a_keymap_value_that_is_a_token_is_a_keymap_defect_not_a_leak():
    """STU-981: re-minting an already-written token. Counted, never called a leak."""
    result = recall([payload(body_snippet="Merci,\n\nPERSON_53\n")],
                    {"PERSON_106": "PERSON_53"})
    assert result["keymap_values_that_are_tokens"] == [
        {"token": "PERSON_106", "value": "PERSON_53"}]
    assert result["keymap_values_in_clear"] == []
    assert result["leaks"] == 0


# --- the Studio-log fallback -----------------------------------------------------


def event(name: str, **rest) -> dict:
    return {"event": name, "run_id": "child", **rest}


def test_the_fallback_reads_what_this_side_handed_the_child():
    events = [
        event("pipeline_start", input=payload()),
        event("stage_context", context_keys=["input"]),
        event("tool_call_start", tool="org_memory-read", params={"name": "PERSON_4"}),
        event("tool_call_complete", tool="org_memory-read", result={"body": "Marie"}),
    ]
    payloads = studio_payloads(events)
    assert len(payloads) == 2
    # `tool_call_complete` deliberately left out: a tool's answer is redacted by the
    # module that serves it, which is a different gate.
    assert all("Marie" not in payload_text(p) for p in payloads)


def test_a_child_log_names_the_conversation_it_opened_on():
    assert studio_thread_id([event("pipeline_start", input=payload())]) == "AAQkAGVjY2Mw"
    assert studio_thread_id([event("stage_start", stage="classify")]) is None


# --- the CLI ---------------------------------------------------------------------


def report(tmp_path: Path, *, fields: list[str] | None = None) -> Path:
    path = tmp_path / "reports" / f"{RUN}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": RUN,
        "provenance": {"input_path": "webhook:push-notification",
                       "anonymized_fields": ["body_snippet"] if fields is None else fields},
        "totals": {"emails": 1, "llm": 1},
        "verdicts": [{"message_id": "m1", "thread_id": "AAQkAGVjY2Mw", "path": "llm"}],
    }), encoding="utf-8")
    return path


def run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "recall",
         "--report", str(report(tmp_path)),
         "--keymap", str(tmp_path / "logs" / "anonymization" / "keymap.json"),
         "--runs-dir", str(tmp_path / ".studio" / "runs"), *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )


def write_keymap(tmp_path: Path, keymap: dict) -> Path:
    path = tmp_path / "logs" / "anonymization" / "keymap.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keymap), encoding="utf-8")
    return path


def test_cli_settles_a_push_run_off_its_own_record(tmp_path):
    keymap = write_keymap(tmp_path, {"PERSON_4": "Marie Tremblay"})
    record_sent(sent_path(keymap, RUN), [payload()])
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["leaks"] == 0
    assert out["sent_log"].endswith(f"{RUN}-sent.jsonl")


def test_cli_exits_1_on_a_leak(tmp_path):
    keymap = write_keymap(tmp_path, {"PERSON_4": "Marie Tremblay"})
    record_sent(sent_path(keymap, RUN), [payload(body_snippet="Merci, Marie Tremblay")])
    proc = run_cli(tmp_path)
    assert proc.returncode == 1
    [hit] = json.loads(proc.stdout)["keymap_values_in_clear"]
    assert hit["value"] == "Marie Tremblay"


def test_cli_falls_back_to_a_child_log_matched_on_the_conversation(tmp_path):
    """A classify child log carries no run id of ours — only the thread it opens on."""
    write_keymap(tmp_path, {})
    log = tmp_path / ".studio" / "runs" / "2026-08-28T20h25m-classify-f16324be.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps(event("pipeline_start", input=payload())) + "\n",
                   encoding="utf-8")
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["studio_logs"] == [str(log)]
    assert "sibling run" in out["attribution_caveat"]


def test_cli_exits_2_when_nothing_records_what_was_sent(tmp_path):
    """A scan that read nothing is not a clean scan."""
    write_keymap(tmp_path, {})
    proc = run_cli(tmp_path)
    assert proc.returncode == 2
    assert "never as clean" in proc.stderr


def test_cli_reads_a_run_that_escalated_nothing_as_clean(tmp_path):
    """A stage that sent nothing left an empty file, which is a verdict, not a gap."""
    keymap = write_keymap(tmp_path, {"PERSON_4": "Marie Tremblay"})
    record_sent(sent_path(keymap, RUN), [])
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert (out["payloads"], out["leaks"]) == (0, 0)


def test_cli_asks_nothing_of_a_run_that_claimed_no_scope(tmp_path):
    """No scope, no gate — and deliberately no record of the mail it sent in clear."""
    write_keymap(tmp_path, {})
    path = report(tmp_path, fields=[])
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "recall", "--report", str(path),
         "--keymap", str(tmp_path / "logs" / "anonymization" / "keymap.json")],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 0
    assert "no recall to settle" in proc.stderr
    assert proc.stdout == ""


def test_cli_skips_a_child_log_it_cannot_parse(tmp_path):
    """A log Studio is still writing has a half-flushed line, and is not this run's."""
    write_keymap(tmp_path, {})
    runs = tmp_path / ".studio" / "runs"
    runs.mkdir(parents=True)
    (runs / "half-written.jsonl").write_text('{"event": "pipeline_st', encoding="utf-8")
    (runs / "classify.jsonl").write_text(
        json.dumps(event("pipeline_start", input=payload())) + "\n", encoding="utf-8")
    proc = run_cli(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["payloads"] == 1
    assert "half-written.jsonl" in proc.stderr


def test_cli_reads_one_real_log_once_through_two_paths(tmp_path):
    """A worktree's .studio/runs is a symlink to the main checkout's, and the default
    --runs-dir names both: reading twice doubles every number an auditor triages on."""
    write_keymap(tmp_path, {"PERSON_4": "Marie Tremblay"})
    runs = tmp_path / ".studio" / "runs"
    runs.mkdir(parents=True)
    (runs / "classify.jsonl").write_text(
        json.dumps(event("pipeline_start",
                         input=payload(body_snippet="Merci, Marie Tremblay"))) + "\n",
        encoding="utf-8")
    alias = tmp_path / "worktree-runs"
    alias.symlink_to(runs, target_is_directory=True)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "recall", "--report", str(report(tmp_path)),
         "--keymap", str(tmp_path / "logs" / "anonymization" / "keymap.json"),
         "--runs-dir", str(runs), str(alias)],
        capture_output=True, text=True, cwd=REPO_ROOT)
    assert proc.returncode == 1
    out = json.loads(proc.stdout)
    assert out["payloads"] == 1
    assert [h["occurrences"] for h in out["keymap_values_in_clear"]] == [1]
