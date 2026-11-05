"""Appointments planned as calendar events (STU-2089): policy, selection, checks, plan."""

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from email_classifier import calendar_plan as cp
from email_classifier.stage1.policy import CalendarPolicy, load_policy
from tests.conftest import stub_detector
from tests.test_mailbox_full import load_script

TAXONOMY = Path(__file__).resolve().parents[1] / "reference-deployment" / "rules" / "taxonomy.yaml"
RULES = TAXONOMY.with_name("rules.yaml")
CAL = CalendarPolicy(subtypes=("recital_confirmation",), entity="calendar.studio",
                     timezone="America/Toronto", default_minutes=60)
BODY = "Bonjour, votre essai est confirmé pour le mercredi 4 novembre à 14h00. Réf. AB-1207."


def write_policy(tmp_path, calendar=None, scope="full"):
    data = {
        "deterministic_can_finalize": True,
        "email": {"provider": "gmail"},
        "anonymization": {"scope": scope},
        "timezone": "America/Toronto",
    }
    if calendar is not None:
        data["calendar"] = calendar
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def item(message_id="m1", body=BODY, date="2026-10-20T13:00:00+00:00",
         sender="booking@example-music.test"):
    return {"message_id": message_id, "thread_id": f"t-{message_id}", "subject": "Essai",
            "from_address": sender, "date": date, "body_text": body}


def extraction(**overrides):
    out = {"kind": "confirmation", "title": "Essai de violon", "date": "11-04",
           "weekday": "wednesday", "start_time": "14:00", "end_time": None,
           "date_evidence": "le mercredi 4 novembre à 14h00", "previous_date": None,
           "previous_start_time": None, "reference": "AB-1207", "location": None,
           "confidence": "high"}
    return {**out, **overrides}


def run_plan(pending, outputs, journal=(), keymap=None):
    results = {i: {"index": i, "status": "success", "output": o} for i, o in enumerate(outputs)}
    return cp.plan(pending, results, list(journal), CAL, keymap or {}, "run-1")


# ── policy ───────────────────────────────────────────────────────────────────

def test_no_calendar_block_is_off(tmp_path):
    assert load_policy(write_policy(tmp_path)).calendar is None


def test_the_block_inherits_the_policy_clock_and_an_hour(tmp_path):
    policy = load_policy(write_policy(
        tmp_path, {"subtypes": ["recital_confirmation"], "entity": "calendar.studio"}))
    assert policy.calendar == CAL


@pytest.mark.parametrize("block, field", [
    ({"entity": "calendar.studio"}, "subtypes"),
    ({"subtypes": [], "entity": "calendar.studio"}, "subtypes"),
    ({"subtypes": ["x"]}, "entity"),
    ({"subtypes": ["x"], "entity": "calendar.studio", "rules": "physio"}, "rules"),
    ({"subtypes": ["x"], "entity": "calendar.studio", "rules": [""]}, "rules"),
    ({"subtypes": ["x"], "entity": "calendar.studio", "default_minutes": 0}, "default_minutes"),
    ({"subtypes": ["x"], "entity": "calendar.studio", "default_minutes": True}, "default_minutes"),
    ({"subtypes": ["x"], "entity": "calendar.studio", "timezone": "Mars/Base"}, "timezone"),
    (["x"], "calendar"),
])
def test_a_malformed_block_is_refused_at_load(tmp_path, block, field):
    with pytest.raises(ValueError, match=field):
        load_policy(write_policy(tmp_path, block))


# ── selection ────────────────────────────────────────────────────────────────

REPORT = {"run_id": "run-1", "verdicts": [
    {"message_id": "late", "status": "ok", "subtype": "recital_confirmation",
     "date": "2026-10-21T10:00:00+00:00"},
    {"message_id": "early", "status": "ok", "subtype": "recital_confirmation",
     "date": "2026-10-20T10:00:00+00:00"},
    {"message_id": "other", "status": "ok", "subtype": "newsletter", "date": "2026-10-20"},
    {"message_id": "broken", "status": "error", "subtype": None, "date": "2026-10-20"},
    {"message_id": "done", "status": "ok", "subtype": "recital_confirmation",
     "date": "2026-10-19"},
    {"message_id": "ruled", "status": "ok", "path": "deterministic", "subtype": None,
     "rule_id": "tuition_processor_receipt", "date": "2026-10-22T10:00:00+00:00"},
    {"message_id": "unrelated", "status": "ok", "path": "deterministic", "subtype": None,
     "rule_id": "cantabile_newsletter_archive", "date": "2026-10-22T11:00:00+00:00"},
]}


def test_targets_are_listed_subtypes_oldest_first_minus_the_handled():
    assert cp.targets(REPORT, CAL.subtypes, {"done"}) == ["early", "late"]


def test_a_listed_rule_selects_the_row_it_decided_and_no_other():
    assert cp.targets(REPORT, CAL.subtypes, {"done"}, ("tuition_processor_receipt",)) == [
        "early", "late", "ruled"]


def test_a_listed_rule_does_not_select_an_llm_row_it_escalated():
    report = {"verdicts": [{"message_id": "llm", "status": "ok", "path": "llm",
                            "subtype": "newsletter", "rule_id": "catch_all", "date": ""}]}
    assert cp.targets(report, CAL.subtypes, set(), ("catch_all",)) == []


def test_both_journals_count_as_handled():
    events = [{"message_id": "a", "key": "ref:x"}]
    review = [{"message_id": "b"}]
    assert cp.handled_message_ids(events, review) == {"a", "b"}


# ── calendar-pre ─────────────────────────────────────────────────────────────

def _email(message_id, date):
    return {"message_id": message_id, "thread_id": f"t-{message_id}",
            "from_address": "booking@example-music.test", "subject": "Essai", "headers": {},
            "body_snippet": BODY, "date": date, "provider_category": None}


def run_pre(tmp_path, monkeypatch, capsys, calendar):
    stub_detector(monkeypatch)
    pre = load_script("calendar_pre")
    (tmp_path / "report.json").write_text(json.dumps(REPORT), encoding="utf-8")
    (tmp_path / "corpus.json").write_text(json.dumps(
        [_email(v["message_id"], v["date"]) for v in REPORT["verdicts"]]), encoding="utf-8")
    ctx = {"corpus": str(tmp_path / "corpus.json"), "report": str(tmp_path / "report.json"),
           "policy": str(write_policy(tmp_path, calendar)), "taxonomy": str(TAXONOMY),
           "rules": str(RULES), "learned": str(tmp_path / "learned.yaml"),
           "keymap": str(tmp_path / "anon" / "keymap.json"),
           "events": str(tmp_path / "cal" / "events.jsonl"),
           "review": str(tmp_path / "cal" / "review.jsonl")}
    cp.append_rows(Path(ctx["events"]), [{"message_id": "done", "key": "ref:old", "op": "create"}])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"additional_context": yaml.safe_dump(ctx)})))
    pre.main()
    return json.loads(capsys.readouterr().out)


def test_pre_without_a_block_hands_over_nothing(tmp_path, monkeypatch, capsys):
    out = run_pre(tmp_path, monkeypatch, capsys, None)
    assert (out["enabled"], out["needs_extract"], out["pending"]) == (False, False, [])


def test_pre_skips_the_journaled_message_and_tokenizes_the_rest(tmp_path, monkeypatch, capsys):
    out = run_pre(tmp_path, monkeypatch, capsys,
                  {"subtypes": ["recital_confirmation"], "entity": "calendar.studio"})
    assert [i["message_id"] for i in out["pending"]] == ["early", "late"]
    assert out["already_handled"] == 1
    assert out["pending"][0]["from_address"].startswith("EMAIL_")
    assert out["pending"][0]["body_text"] == BODY


def test_pre_selects_the_row_a_listed_rule_decided(tmp_path, monkeypatch, capsys):
    out = run_pre(tmp_path, monkeypatch, capsys,
                  {"subtypes": ["recital_confirmation"], "entity": "calendar.studio",
                   "rules": ["tuition_processor_receipt"]})
    assert [i["message_id"] for i in out["pending"]] == ["early", "late", "ruled"]


@pytest.mark.parametrize("rule", ["dentist_rule", "catch_all"])
def test_pre_refuses_a_rule_that_decides_nothing(tmp_path, monkeypatch, capsys, rule):
    with pytest.raises(SystemExit, match="calendar.rules"):
        run_pre(tmp_path, monkeypatch, capsys,
                {"subtypes": ["recital_confirmation"], "entity": "calendar.studio",
                 "rules": [rule]})


def test_pre_refuses_a_subtype_the_taxonomy_lacks(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit, match="taxonomy"):
        run_pre(tmp_path, monkeypatch, capsys,
                {"subtypes": ["dentist_visit"], "entity": "calendar.studio"})


# ── checks ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("overrides, reason", [
    ({"confidence": "low"}, "low confidence"),
    ({"date_evidence": "le jeudi 5 novembre"}, "date_evidence"),
    ({"date_evidence": ""}, "date_evidence"),
    ({"weekday": "thursday"}, "is a wednesday"),
    ({"weekday": "jeudi"}, "is a wednesday"),
    ({"weekday": "miércoles"}, "not a day name"),
    ({"date": "4 novembre"}, "YYYY-MM-DD"),
    ({"date": "2026-02-30"}, "calendar date"),
    ({"start_time": None}, "HH:MM"),
    ({"start_time": "2pm"}, "HH:MM"),
    ({"end_time": "13:00"}, "not after"),
    ({"kind": "reminder"}, "kind"),
    ({"confidence": "certain"}, "confidence"),
])
def test_each_refusal_lands_in_review_not_the_plan(overrides, reason):
    planned, events, review = run_plan([item()], [extraction(**overrides)])
    assert (planned, events) == ([], [])
    assert len(review) == 1 and reason in review[0]["reason"]
    assert review[0]["message_id"] == "m1"


def test_evidence_matches_across_case_and_whitespace():
    planned, _, review = run_plan([item()], [extraction(date_evidence="LE MERCREDI  4 novembre\nà 14h00")])
    assert review == [] and len(planned) == 1


def test_a_weekday_in_the_message_language_is_accepted():
    planned, _, review = run_plan([item()], [extraction(weekday="mercredi")])
    assert review == [] and len(planned) == 1


def test_a_missing_year_is_the_next_occurrence_after_the_message():
    msg = item(body="Rendez-vous le 15 janvier à 09:30.", date="2026-12-10T15:00:00+00:00")
    planned, _, _ = run_plan([msg], [extraction(
        date="01-15", weekday="friday", start_time="09:30",
        date_evidence="le 15 janvier à 09:30")])
    assert planned[0]["event"]["start"] == "2027-01-15T09:30:00-05:00"


def test_a_written_year_is_kept():
    planned, _, _ = run_plan([item()], [extraction(date="2026-11-04")])
    assert planned[0]["event"]["start"].startswith("2026-11-04T14:00")


# ── localization across the 1 November 2026 DST change ──────────────────────

@pytest.mark.parametrize("day, weekday, start, end", [
    ("10-31", "saturday", "2026-10-31T10:00:00-04:00", "2026-10-31T11:00:00-04:00"),
    ("11-02", "monday", "2026-11-02T10:00:00-05:00", "2026-11-02T11:00:00-05:00"),
])
def test_toronto_offsets_either_side_of_the_change(day, weekday, start, end):
    planned, _, _ = run_plan([item(body=f"Rendez-vous {day} 10:00")], [extraction(
        date=day, weekday=weekday, start_time="10:00", date_evidence=f"{day} 10:00")])
    assert (planned[0]["event"]["start"], planned[0]["event"]["end"]) == (start, end)


def test_the_default_hour_is_absolute_on_the_fall_back_night():
    planned, _, _ = run_plan([item(body="Rendez-vous 11-01 01:30")], [extraction(
        date="11-01", weekday="sunday", start_time="01:30", date_evidence="11-01 01:30")])
    assert planned[0]["event"]["start"] == "2026-11-01T01:30:00-04:00"
    assert planned[0]["event"]["end"] == "2026-11-01T01:30:00-05:00"


def test_a_stated_end_is_kept():
    planned, _, _ = run_plan([item()], [extraction(end_time="15:15")])
    assert planned[0]["event"]["end"] == "2026-11-04T15:15:00-05:00"


# ── plan and journal ─────────────────────────────────────────────────────────

def test_a_confirmation_is_created_under_its_reference():
    planned, events, _ = run_plan([item()], [extraction()])
    assert planned == [{
        "op": "create", "key": "ref:AB-1207", "message_id": "m1",
        "entity": "calendar.studio", "previous": None, "uid": None,
        "event": {"title": "Essai de violon", "start": "2026-11-04T14:00:00-05:00",
                  "end": "2026-11-04T15:00:00-05:00", "location": None,
                  "reference": "AB-1207", "sender_domain": "example-music.test"},
    }]
    assert events == [{"run_id": "run-1", "status": "planned", **planned[0]}]


def test_no_reference_keys_on_the_message_id():
    planned, _, _ = run_plan([item()], [extraction(reference=None)])
    assert planned[0]["key"] == "msg:m1"


def test_tokens_are_restored_into_the_planned_event():
    planned, _, _ = run_plan([item()], [extraction(location="chez PERSON_2")],
                             keymap={"PERSON_2": "Zelda Quintal"})
    assert planned[0]["event"]["location"] == "chez Zelda Quintal"


def test_not_an_appointment_is_journaled_without_a_plan():
    planned, events, review = run_plan([item()], [{"kind": "not_appointment",
                                                   "confidence": "high"}])
    assert (planned, review) == ([], [])
    assert events[0]["op"] == "none" and events[0]["key"] == "msg:m1"


def test_a_failed_child_is_neither_planned_nor_journaled():
    results = {0: {"index": 0, "status": "failed"}}
    assert cp.plan([item()], results, [], CAL, {}, "run-1") == ([], [], [])


def test_the_last_row_of_a_key_is_its_state():
    rows = [{"key": "ref:x", "op": "create"}, {"key": "ref:x", "op": "create", "uid": "u-9"}]
    assert cp.state(rows)["ref:x"]["uid"] == "u-9"


def _journaled(**event):
    base = {"title": "Essai", "start": "2026-11-04T14:00:00-05:00",
            "end": "2026-11-04T15:00:00-05:00", "location": None, "reference": "AB-1207",
            "sender_domain": "example-music.test"}
    return {"key": "ref:AB-1207", "op": "create", "message_id": "m0", "status": "applied",
            "uid": "u-1", "event": {**base, **event}}


RESCHEDULE_BODY = "Votre essai du 4 novembre à 14h00 est déplacé au mardi 10 novembre à 16:00."


def test_a_reschedule_moves_the_event_its_reference_names():
    planned, _, _ = run_plan([item("m2", RESCHEDULE_BODY)], [extraction(
        kind="reschedule", date="11-10", weekday="tuesday", start_time="16:00",
        date_evidence="au mardi 10 novembre à 16:00")], journal=[_journaled()])
    assert planned[0]["op"] == "move" and planned[0]["key"] == "ref:AB-1207"
    assert planned[0]["uid"] == "u-1"
    assert planned[0]["previous"] == {"start": "2026-11-04T14:00:00-05:00",
                                      "end": "2026-11-04T15:00:00-05:00"}
    assert planned[0]["event"]["start"] == "2026-11-10T16:00:00-05:00"


def test_a_reschedule_without_reference_matches_sender_and_previous_start():
    journal = [{**_journaled(reference=None), "key": "msg:m0"}]
    planned, _, review = run_plan([item("m2", RESCHEDULE_BODY)], [extraction(
        kind="reschedule", reference=None, date="11-10", weekday="tuesday",
        start_time="16:00", date_evidence="au mardi 10 novembre à 16:00",
        previous_date="11-04", previous_start_time="14:00")], journal=journal)
    assert review == []
    assert planned[0]["key"] == "msg:m0" and planned[0]["op"] == "move"


def test_another_senders_event_at_that_time_is_not_matched():
    journal = [{**_journaled(reference=None, sender_domain="elsewhere.test"), "key": "msg:m0"}]
    planned, _, review = run_plan([item("m2", RESCHEDULE_BODY)], [extraction(
        kind="reschedule", reference=None, date="11-10", weekday="tuesday",
        start_time="16:00", date_evidence="au mardi 10 novembre à 16:00",
        previous_date="11-04", previous_start_time="14:00")], journal=journal)
    assert planned == [] and "no planned event" in review[0]["reason"]


def test_a_cancellation_deletes_the_event_and_carries_its_uid():
    body = "Le cours du 4 novembre à 14h00 (réf. AB-1207) est annulé."
    planned, _, _ = run_plan([item("m3", body)], [extraction(
        kind="cancellation", date_evidence="Le cours du 4 novembre à 14h00")],
        journal=[_journaled()])
    assert planned[0]["op"] == "delete" and planned[0]["uid"] == "u-1"
    assert planned[0]["event"]["start"] == "2026-11-04T14:00:00-05:00"


def test_a_cancellation_of_a_deleted_event_goes_to_review():
    body = "Le cours du 4 novembre à 14h00 (réf. AB-1207) est annulé."
    deleted = {**_journaled(), "op": "delete"}
    planned, _, review = run_plan([item("m3", body)], [extraction(
        kind="cancellation", date_evidence="Le cours du 4 novembre à 14h00")],
        journal=[_journaled(), deleted])
    assert planned == [] and "no planned event" in review[0]["reason"]


def test_a_confirmation_and_its_reschedule_in_one_run():
    planned, _, _ = run_plan(
        [item("m1"), item("m2", RESCHEDULE_BODY)],
        [extraction(), extraction(kind="reschedule", date="11-10", weekday="tuesday",
                                  start_time="16:00",
                                  date_evidence="au mardi 10 novembre à 16:00")])
    assert [p["op"] for p in planned] == ["create", "move"]


def test_a_repeated_confirmation_plans_nothing_more():
    planned, events, _ = run_plan([item("m4")], [extraction()],
                                  journal=[_journaled(title="Essai de violon")])
    assert planned == [] and events[0]["op"] == "none" and events[0]["key"] == "msg:m4"


# ── calendar-post ────────────────────────────────────────────────────────────

def test_post_writes_both_journals_and_a_rerun_plans_nothing_twice(
    tmp_path, monkeypatch, capsys
):
    post = load_script("calendar_post")
    sent = []
    monkeypatch.setattr(post.alerts, "notify", sent.append)
    paths = {"policy": str(write_policy(tmp_path, {"subtypes": ["recital_confirmation"],
                                                   "entity": "calendar.studio"})),
             "keymap": str(tmp_path / "keymap.json"),
             "events": str(tmp_path / "cal" / "events.jsonl"),
             "review": str(tmp_path / "cal" / "review.jsonl"),
             "stage_log": str(tmp_path / "cal" / "stages.jsonl")}
    pre = {"run_id": "run-1", "pending": [item("m1"), item("m2")], "paths": paths}
    fanout = {"results": [
        {"index": 0, "status": "success", "output": extraction()},
        {"index": 1, "status": "success", "output": extraction(confidence="low")},
    ]}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"previous_outputs": {
        "calendar-pre": pre, "calendar-fanout": fanout}})))
    post.main()
    out = json.loads(capsys.readouterr().out)
    assert (out["planned"], out["review"], out["failed"]) == (1, 1, 0)
    assert [r["message_id"] for r in cp.read_rows(Path(paths["events"]))] == ["m1"]
    assert [r["message_id"] for r in cp.read_rows(Path(paths["review"]))] == ["m2"]
    # STU-2116: the refusal is alerted, not just written to the review file.
    [alert] = sent
    assert "1 appointment(s) went to review" in alert
    assert "booking@example-music.test · Essai: low confidence" in alert
    # STU-2105: the fan-out's spend, on the line `audit_run.py cost` reads.
    [line] = cp.read_rows(Path(paths["stage_log"]))
    assert (line["run_id"], line["stage"], line["status"]) == ("run-1", "calendar", "completed")
    assert line["tokens"]["llm_calls"] == 2

    handled = cp.handled_message_ids(cp.read_rows(Path(paths["events"])),
                                     cp.read_rows(Path(paths["review"])))
    report = {"verdicts": [{"message_id": m, "status": "ok",
                            "subtype": "recital_confirmation"} for m in ("m1", "m2")]}
    assert cp.targets(report, CAL.subtypes, handled) == []


def test_post_with_no_refusal_sends_no_alert(tmp_path, monkeypatch, capsys):
    post = load_script("calendar_post")
    sent = []
    monkeypatch.setattr(post.alerts, "notify", sent.append)
    paths = {"policy": str(write_policy(tmp_path, {"subtypes": ["recital_confirmation"],
                                                   "entity": "calendar.studio"})),
             "keymap": str(tmp_path / "keymap.json"),
             "events": str(tmp_path / "events.jsonl"), "review": str(tmp_path / "review.jsonl"),
             "stage_log": str(tmp_path / "stages.jsonl")}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"previous_outputs": {
        "calendar-pre": {"run_id": "run-1", "pending": [item()], "paths": paths},
        "calendar-fanout": {"results": [
            {"index": 0, "status": "success", "output": extraction()}]}}})))
    post.main()
    assert json.loads(capsys.readouterr().out)["planned"] == 1
    assert sent == []


def test_post_with_nothing_pending_writes_nothing(tmp_path, monkeypatch, capsys):
    post = load_script("calendar_post")
    paths = {"policy": str(write_policy(tmp_path)), "keymap": "",
             "events": str(tmp_path / "events.jsonl"), "review": str(tmp_path / "review.jsonl"),
             "stage_log": str(tmp_path / "stages.jsonl")}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"previous_outputs": {
        "calendar-pre": {"run_id": "run-1", "pending": [], "paths": paths}}})))
    post.main()
    assert json.loads(capsys.readouterr().out)["plan"] == []
    assert not (tmp_path / "events.jsonl").exists()
    assert not (tmp_path / "stages.jsonl").exists()
