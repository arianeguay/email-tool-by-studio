"""Reading the V1 exit condition off artifacts, and refusing to guess (STU-1242)."""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from email_classifier import pilot_delta, pilot_gate

REPO_ROOT = Path(__file__).resolve().parents[1]

DAY = 86400.0
# A window one week wide, ending at a fixed instant so nothing here depends on today.
UNTIL = datetime(2026, 9, 2, tzinfo=timezone.utc).timestamp()
WINDOW = pilot_gate.Window(since=UNTIL - 7 * DAY, until=UNTIL)


def _round(at, cohort=pilot_delta.CARRYING, run_id="run"):
    return pilot_delta.Round(path=Path(f"reports/{run_id}.json"), run_id=run_id,
                             commit="abc", cohort=cohort, at=at, mailbox=None)


def _week_of_rounds():
    return [_round(WINDOW.since + n * DAY + 3600, run_id=f"run-{n}") for n in range(7)]


def _cron(at, code, source="cron"):
    entry = {"at": datetime.fromtimestamp(at, tz=timezone.utc).isoformat(), "exit": code}
    if source is not None:
        entry["source"] = source
    return entry


def _hourly(codes):
    return [_cron(WINDOW.since + n * DAY + 3600, code) for n, code in enumerate(codes)]


def _week(rounds=None, entries=None, journal=Path("state/health-checks.jsonl")):
    return pilot_gate.unattended_week(
        rounds if rounds is not None else _week_of_rounds(),
        entries if entries is not None else _hourly([0] * 7),
        WINDOW, journal, ["reports"])


# --- condition 1 -----------------------------------------------------------------


def test_a_full_week_of_rounds_under_a_clean_probe_is_met():
    assert _week().verdict == pilot_gate.MET


def test_a_window_with_no_cron_line_is_unmeasured_not_quiet():
    """Criterion 5's own rule: a window whose hours are not covered by hourly runs cannot
    settle whether it was unattended. Quiet and unwatched are the same silence, and
    reading one as the other is what STU-912 forbids."""
    hand_run = [_cron(WINDOW.since + DAY, 0, source="manual")]
    assert _week(entries=hand_run).verdict == pilot_gate.NOT_MEASURABLE


def test_a_line_with_no_source_settles_nothing_either_way():
    """It predates STU-1082's flag and belongs to nobody — counted, never scored."""
    condition = _week(entries=[_cron(WINDOW.since + DAY, 0, source=None)])
    assert condition.verdict == pilot_gate.NOT_MEASURABLE
    assert condition.detail["unattributed_lines"] == 1


def test_an_unreadable_journal_is_unmeasured_rather_than_an_empty_one():
    """`health_entries` returns None for a journal it cannot open and [] for an empty
    one. Both leave the criterion unsettled; only the second is a fact about the pilot."""
    condition = pilot_gate.unattended_week(_week_of_rounds(), None, WINDOW,
                                           Path("state/health-checks.jsonl"), ["reports"])
    assert condition.verdict == pilot_gate.NOT_MEASURABLE
    assert condition.detail["cron_runs"] is None


def test_health_entries_separates_an_absent_journal_from_an_empty_one(tmp_path):
    assert pilot_gate.health_entries(tmp_path / "nope.jsonl") is None
    empty = tmp_path / "health.jsonl"
    empty.write_text("", encoding="utf-8")
    assert pilot_gate.health_entries(empty) == []


def test_a_non_zero_cron_exit_fails_the_week_even_when_the_rounds_ran():
    """Measured on the serving host 2026-09-02: 36 of 58 cron runs exited 3. The rounds happened;
    the code that served them is not the code `main` holds, so no report in the window can
    be read against the repo — which is what criterion 5 says exit 3 costs."""
    condition = _week(entries=_hourly([0, 3, 3, 0, 3, 0, 0]))
    assert condition.verdict == pilot_gate.NOT_MET
    assert condition.detail["cron_exits"] == {"0": 4, "3": 3}


def test_a_short_window_is_not_met_rather_than_unmeasured():
    """Nothing is missing — the days are simply not there yet, and that is a fact about
    the pilot rather than about the reading."""
    condition = _week(rounds=_week_of_rounds()[:3])
    assert condition.verdict == pilot_gate.NOT_MET
    assert condition.detail["days_with_a_live_round"] == 3


def test_several_rounds_on_one_day_are_one_day():
    same_day = [_round(WINDOW.since + 3600 * n, run_id=f"r{n}") for n in range(1, 6)]
    assert _week(rounds=same_day).detail["days_with_a_live_round"] == 1


def test_a_cron_line_outside_the_window_does_not_count():
    stale = [_cron(WINDOW.since - DAY, 0)] + _hourly([0] * 7)
    assert _week(entries=stale).detail["cron_runs"] == 7


# --- condition 2 -----------------------------------------------------------------


def _reading(run_id, state, leaks=0):
    return {"run_id": run_id, "state": state, "leaks": leaks}


def test_a_clean_sweep_over_settled_runs_is_met():
    readings = [_reading("a", "settled"), _reading("b", "settled")]
    assert pilot_gate.no_pii_leak(readings, ["logs/anonymization"]).verdict == pilot_gate.MET


def test_a_surviving_value_fails_and_names_every_known_over_reporter():
    """An over-reporter is named, never subtracted: a reader who learns to wave the list
    through is how a real leak gets missed.

    Asserted off `RECALL_NOISE` rather than against a list written twice — the names
    change as the defects are fixed, and STU-1227 already left this one."""
    condition = pilot_gate.no_pii_leak([_reading("a", "settled", leaks=2)], ["x"])
    assert condition.verdict == pilot_gate.NOT_MET
    assert pilot_gate.RECALL_NOISE
    for name in pilot_gate.RECALL_NOISE:
        assert name in condition.summary


def test_a_fixed_over_reporter_is_not_left_in_the_caveat():
    """The caveat is a claim that the reading over-reports *today*. STU-1227 shipped the
    whole-word rule, so naming it would send a reader to dismiss hits on a defect that no
    longer exists."""
    assert not any("STU-1227" in name for name in pilot_gate.RECALL_NOISE)


def test_a_run_that_kept_no_sent_log_downgrades_a_clean_sweep():
    """"No PII leak over the window" is a claim about the window. A sweep that skipped
    part of it has made a smaller claim, not that one."""
    readings = [_reading("a", "settled"), _reading("b", "unsettled")]
    condition = pilot_gate.no_pii_leak(readings, ["x"])
    assert condition.verdict == pilot_gate.NOT_MEASURABLE
    assert condition.detail["unsettled_run_ids"] == ["b"]


def test_a_leak_outranks_an_unsettled_run():
    readings = [_reading("a", "settled", leaks=1), _reading("b", "unsettled")]
    assert pilot_gate.no_pii_leak(readings, ["x"]).verdict == pilot_gate.NOT_MET


def test_a_window_of_cleartext_by_policy_runs_settles_nothing():
    """A run that recorded no scope sent the mail in clear by policy and has no gate to
    fail — so a window made only of those is unmeasured, never clean."""
    readings = [_reading("a", "no scope"), _reading("b", "no scope")]
    condition = pilot_gate.no_pii_leak(readings, ["x"])
    assert condition.verdict == pilot_gate.NOT_MEASURABLE
    assert condition.detail["runs_without_scope"] == 2


def test_a_scopeless_run_beside_settled_ones_does_not_block_met():
    readings = [_reading("a", "settled"), _reading("b", "no scope")]
    assert pilot_gate.no_pii_leak(readings, ["x"]).verdict == pilot_gate.MET


# --- condition 3 -----------------------------------------------------------------


def test_a_thin_denominator_is_unmeasured_and_says_the_count():
    """STU-1182 measured three. Three drafts do not give a ratio a denominator, and this
    is the finding rather than a defect in the reading."""
    condition = pilot_gate.drafts_accepted_ratio(3, 10, ["reports"], None, None)
    assert condition.verdict == pilot_gate.NOT_MEASURABLE
    assert "3 conversation(s)" in condition.summary


def test_clearing_the_floor_offline_is_still_unmeasured():
    condition = pilot_gate.drafts_accepted_ratio(
        12, 10, ["reports"], None, "No Graph call was made.")
    assert condition.verdict == pilot_gate.NOT_MEASURABLE
    assert "No Graph call" in condition.summary


def test_a_ratio_is_taken_over_the_conversations_a_human_judged():
    table = {"conversations": 12, "judged": 10, "unchanged": 6, "ratio": 0.6,
             "by_outcome": {}}
    condition = pilot_gate.drafts_accepted_ratio(12, 10, ["reports"], table, None)
    assert condition.verdict == pilot_gate.MET
    assert "6 of 10" in condition.summary


def test_drafts_nobody_judged_yet_carry_no_ratio():
    """Awaiting review and abandoned are two different absences and neither is a verdict
    (STU-1100); a denominator that counted them would score a mid-review window as bad."""
    table = {"conversations": 12, "judged": 0, "unchanged": 0, "ratio": None,
             "by_outcome": {}}
    condition = pilot_gate.drafts_accepted_ratio(12, 10, ["reports"], table, None)
    assert condition.verdict == pilot_gate.NOT_MEASURABLE


def test_accepted_table_excludes_the_unjudged_from_the_denominator():
    rows = [
        {"outcome": "sent unchanged", "similarity": 0.99},
        {"outcome": "lightly edited", "similarity": 0.8},
        {"outcome": "awaiting review", "similarity": None},
        {"outcome": "abandoned, reason unknown", "similarity": None},
    ]
    table = pilot_gate.accepted_table(rows, "sent unchanged")
    assert (table["conversations"], table["judged"], table["unchanged"]) == (4, 2, 1)
    assert table["ratio"] == 0.5


def test_accepted_table_returns_no_ratio_rather_than_zero_when_nothing_was_judged():
    """The rule `_lengths` follows: a zero here would read as every draft rewritten."""
    rows = [{"outcome": "awaiting review", "similarity": None}]
    assert pilot_gate.accepted_table(rows, "sent unchanged")["ratio"] is None


# --- the verdict as a whole ------------------------------------------------------


def _condition(verdict):
    return pilot_gate.Condition("c", verdict, "s")


@pytest.mark.parametrize("verdicts,code", [
    ([pilot_gate.MET] * 3, pilot_gate.EXIT_MET),
    ([pilot_gate.MET, pilot_gate.NOT_MEASURABLE, pilot_gate.MET],
     pilot_gate.EXIT_NOT_MEASURABLE),
    ([pilot_gate.MET, pilot_gate.NOT_MEASURABLE, pilot_gate.NOT_MET],
     pilot_gate.EXIT_NOT_MET),
])
def test_a_failed_condition_outranks_an_unmeasured_one(verdicts, code):
    """`audit_run.py recall`'s shape: 1 on a fault, 2 on a run it could not settle. A
    found fault is actionable today; an unmeasured one is a trip back to the host."""
    assert pilot_gate.verdict_exit([_condition(v) for v in verdicts]) == code


def test_every_condition_names_where_it_was_read_from():
    """A verdict whose source cannot be named is the prose this issue replaces."""
    conditions = [
        _week(),
        pilot_gate.no_pii_leak([_reading("a", "settled")], ["logs/anonymization"]),
        pilot_gate.drafts_accepted_ratio(3, 10, ["reports", ".studio/runs"], None, None),
    ]
    for condition in conditions:
        assert condition.artifacts
        assert condition.as_record()["artifacts"] == condition.artifacts


# --- the window ------------------------------------------------------------------


def test_the_window_is_half_open_so_a_run_belongs_to_exactly_one():
    assert WINDOW.holds(WINDOW.since)
    assert not WINDOW.holds(WINDOW.until)


def test_a_bare_date_is_read_as_utc_not_as_the_readers_offset(monkeypatch):
    """Every timestamp read here is written by a host in UTC. A boundary that shifted by
    the reader's offset would move the window under a number someone else published."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pilot_gate_cli", REPO_ROOT / "scripts" / "pilot_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import time
    monkeypatch.setenv("TZ", "America/Toronto")
    time.tzset()
    try:
        assert module._moment("2026-09-02") == UNTIL
    finally:
        monkeypatch.undo()
        time.tzset()


def test_a_report_outside_the_window_is_not_in_it(tmp_path):
    path = tmp_path / "a.json"
    path.write_text(json.dumps({"run_id": "a", "provenance": {
        "git_commit": pilot_delta.RULES_COMMIT,
        "input_path": "webhook:push-notification",
        "anonymized_fields": ["body_text"]}}), encoding="utf-8")
    os.utime(path, (WINDOW.since - DAY, WINDOW.since - DAY))
    (placed,) = pilot_delta.rounds([path], REPO_ROOT)
    assert not WINDOW.holds(placed.at)
    assert placed.input_path == "webhook:push-notification"
    assert placed.anonymized_fields == ("body_text",)


# --- the record cannot overwrite the verdict (STU-1247) ---------------------------


def test_a_record_that_cannot_be_written_names_the_path_rather_than_raising(tmp_path):
    blocked = tmp_path / "logs"
    blocked.mkdir()
    blocked.chmod(0o555)
    try:
        failure = pilot_gate.write_record(blocked / "pilot-gate.json", "{}")
    finally:
        blocked.chmod(0o755)
    assert failure is not None and "pilot-gate.json" in failure


def test_a_record_that_writes_reports_no_failure(tmp_path):
    out = tmp_path / "nested" / "pilot-gate.json"
    assert pilot_gate.write_record(out, '{"a": 1}') is None
    assert out.read_text(encoding="utf-8") == '{"a": 1}\n'


def _cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "pilot_gate_cli", REPO_ROOT / "scripts" / "pilot_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_exit_code_stays_the_verdicts_when_the_record_cannot_be_written(tmp_path):
    """On the serving host `logs/` belongs to the service account STU-1116 created and the
    operator running `make pilot-gate` does not. Letting that propagate handed the verdict
    to `make`'s own exit 2 — which is a legitimate answer here — so a crash arrived wearing
    "a condition could not be measured" while all three had been."""
    reports = tmp_path / "reports"
    reports.mkdir()
    report = reports / "a.json"
    report.write_text(json.dumps({"run_id": "a", "provenance": {
        "git_commit": pilot_delta.RULES_COMMIT,
        "input_path": "webhook:push-notification"}}), encoding="utf-8")
    at = datetime.now(timezone.utc).timestamp() - DAY
    os.utime(report, (at, at))

    blocked = tmp_path / "logs"
    blocked.mkdir()
    blocked.chmod(0o555)
    try:
        code = _cli().main([
            "--reports", str(reports),
            "--runs-dir", str(tmp_path / "runs"),
            "--health-journal", str(tmp_path / "absent.jsonl"),
            "--keymap", str(tmp_path / "keymap.json"),
            "--offline", "-o", str(blocked / "pilot-gate.json"),
        ])
    finally:
        blocked.chmod(0o755)
    # One live round, no probe journal, no drafting traffic: nothing failed, two
    # conditions could not be measured. That is 2 on its own merits, not make's 2.
    assert code == pilot_gate.EXIT_NOT_MEASURABLE


def test_nothing_to_read_is_its_own_code_rather_than_an_unmeasured_condition(tmp_path):
    empty = tmp_path / "reports"
    empty.mkdir()
    assert _cli().main(["--reports", str(empty)]) == pilot_gate.EXIT_NOTHING_TO_READ
