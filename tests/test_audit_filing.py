"""What a push run files for itself, and what filing deliberately leaves unanswered.

STU-1084: the gate STU-900 built holds the next run until every report has an entry, and
nothing wrote one — so the backlog was cleared by hand six times over. Filing closes the
gap without closing the question: the entry carries every field the run's own artifacts
determine, `verdict` stays visibly unanswered, and `coverage` counts those.
"""

import datetime
import json
import os

from pathlib import Path

from email_classifier import cost, deployment, retention
from email_classifier.core.contract import load_taxonomy
from email_classifier.audit import (
    UNREVIEWED,
    UNREVIEWED_VERDICT,
    check_audit_entries,
    check_report,
    unreviewed_runs,
)
from email_classifier.audit_filing import file_unaudited
from email_classifier.orchestrate.provenance import LIVE_INPUT
from scripts import reconcile_audits

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")
from tests.test_audit import audit_entry, det_verdict, error_verdict, llm_verdict


# One priced drafting execution. A single call, so `check_cache_hits` exempts it: these
# tests are about what the note says, not about the ratio.
DRAFTING_SPEND = cost.leg([{"token_usage": {"by_model": {"claude-opus-4-8": {
    "input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 5,
    "cache_creation_input_tokens": 2}}}}])


def push_report(run_id="r1", verdicts=None, **provenance):
    verdicts = [det_verdict()] if verdicts is None else verdicts
    det = sum(1 for v in verdicts if v["path"] == "deterministic")
    llm = sum(1 for v in verdicts if v["path"] == "llm")
    errors = sum(1 for v in verdicts if v["status"] == "error")
    threads = {v["thread_id"] for v in verdicts if v["path"] == "llm"}
    return {
        "run_id": run_id,
        "totals": {"emails": len(verdicts), "deterministic": det, "llm": llm,
                   "errors": errors, "llm_calls": len(threads)},
        "ratio_deterministic": det / len(verdicts) if verdicts else 0.0,
        "by_bucket": {"C": det, "A": llm},
        "by_action": {},
        "verdicts": verdicts,
        "provenance": {"input_path": LIVE_INPUT, "provider": "anthropic",
                       "git_commit": "abc123", **provenance},
    }


def tree(tmp_path, reports=(), audited=(), skipped=(), frontier=None, drafts=()):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    for report in reports:
        (reports_dir / f"{report['run_id']}.json").write_text(
            json.dumps(report), encoding="utf-8")
    log = tmp_path / "audit_log.json"
    log.write_text(json.dumps(list(audited)), encoding="utf-8")
    marker = tmp_path / "skipped.json"
    marker.write_text(json.dumps(list(skipped)), encoding="utf-8")
    frontier_log = tmp_path / "frontier.jsonl"
    if frontier is not None:
        frontier_log.write_text(
            "".join(json.dumps(line) + "\n" for line in frontier), encoding="utf-8")
    drafts_log = tmp_path / "stages.jsonl"
    if drafts:
        drafts_log.write_text(
            "".join(json.dumps(line) + "\n" for line in drafts), encoding="utf-8")
    return dict(reports_dir=reports_dir, audit_log=log, skipped=marker,
                frontier_log=frontier_log, drafts_log=drafts_log)


def file_it(paths):
    return file_unaudited(paths["reports_dir"], paths["audit_log"], paths["skipped"],
                          paths["frontier_log"], paths["drafts_log"], TAXONOMY)


def entries(paths):
    return json.loads(paths["audit_log"].read_text(encoding="utf-8"))


class TestWhatGetsFiled:
    def test_a_push_run_files_its_own_entry(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        assert file_it(paths) == ["r1"]
        assert [e["run_id"] for e in entries(paths)] == ["r1"]

    def test_the_filed_entry_passes_the_shape_check(self, tmp_path):
        """`log-check` runs over the result, so filing must not be what breaks it."""
        paths = tree(tmp_path, reports=[push_report()], frontier=[])
        file_it(paths)
        assert check_audit_entries(entries(paths)) == []

    def test_a_make_run_is_left_to_the_human_gate(self, tmp_path):
        """Only the push path floods. A run off a dump has a person already looking."""
        paths = tree(tmp_path, reports=[push_report(input_path="dumps/d.json")])
        assert file_it(paths) == []
        assert entries(paths) == []

    def test_an_already_audited_run_is_not_filed_over(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()],
                     audited=[audit_entry(run_id="r1", verdict="clean")])
        assert file_it(paths) == []
        assert entries(paths)[0]["verdict"] == "clean"

    def test_a_reasoned_skip_still_excuses_a_run(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()],
                     skipped=[{"run_id": "r1", "date": "2026-08-29", "reason": "no stage 2"}])
        assert file_it(paths) == []

    def test_a_reasonless_marker_excuses_nothing(self, tmp_path):
        """Same rule as the gate (STU-900): a marker naming no reason is not a decision."""
        paths = tree(tmp_path, reports=[push_report()],
                     skipped=[{"run_id": "r1", "date": "2026-08-29"}])
        assert file_it(paths) == ["r1"]

    def test_filing_is_idempotent(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        assert file_it(paths) == ["r1"]
        assert file_it(paths) == []
        assert len(entries(paths)) == 1

    def test_an_earlier_unfiled_run_is_picked_up_by_a_later_one(self, tmp_path):
        """Filing that failed leaves the report on disk; the walk is what heals it."""
        paths = tree(tmp_path, reports=[push_report("r1"), push_report("r2")])
        assert file_it(paths) == ["r1", "r2"]

    def test_a_report_naming_no_run_is_not_filed(self, tmp_path):
        paths = tree(tmp_path)
        (paths["reports_dir"] / "odd.json").write_text(
            json.dumps({"provenance": {"input_path": LIVE_INPUT}}), encoding="utf-8")
        assert file_it(paths) == []


class TestTheVerdictStaysUnanswered:
    def test_the_verdict_says_nobody_read_it(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        assert entries(paths)[0]["verdict"].startswith(UNREVIEWED)

    def test_a_filed_entry_reads_as_unreviewed(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        assert unreviewed_runs(entries(paths)) == ["r1"]

    def test_a_written_verdict_clears_it(self, tmp_path):
        assert unreviewed_runs([audit_entry(verdict="clean, 3 messages")]) == []

    def test_appending_below_the_sentinel_still_owes_a_reading(self):
        """The direction an audit has to fail in: unread until the field is replaced."""
        entry = audit_entry(verdict=f"{UNREVIEWED} — filed\n\nlooks fine to me")
        assert unreviewed_runs([entry]) == ["r1"]


class TestTheFieldsAreTheRunsOwn:
    def test_the_counts_come_off_the_report(self, tmp_path):
        report = push_report(verdicts=[det_verdict("m1"), llm_verdict("m2"),
                                       error_verdict("m3")])
        paths = tree(tmp_path, reports=[report], frontier=[])
        file_it(paths)
        entry = entries(paths)[0]
        assert entry["emails"] == 3
        assert entry["errors"] == 1
        assert entry["error_tags"] == {"studio_run_timeout": 1}
        assert entry["by_bucket"] == {"C": 1, "A": 2}
        assert entry["provider"] == "anthropic"
        assert entry["git_commit"] == "abc123"

    def test_the_dump_names_the_live_input_rather_than_guessing(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        assert entries(paths)[0]["dump"] == LIVE_INPUT

    def test_a_reports_violations_are_counted_not_hidden(self, tmp_path):
        report = push_report()
        report["totals"]["emails"] = 99
        paths = tree(tmp_path, reports=[report])
        file_it(paths)
        assert entries(paths)[0]["violations"] == len(check_report(report, TAXONOMY)) == 1

    def test_rule_staleness_is_null_with_a_note_saying_why(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()], frontier=[])
        file_it(paths)
        entry = entries(paths)[0]
        assert entry["never_fires"] is None
        assert entry["unexercised_by_this_run"] is None
        assert "audit_run.py rules" in entry["schema_note"]

    def test_the_catch_all_share_survives(self, tmp_path):
        report = push_report(verdicts=[det_verdict("m1"), llm_verdict("m2")])
        paths = tree(tmp_path, reports=[report], frontier=[])
        file_it(paths)
        assert entries(paths)[0]["catch_all_share"] == 0.5

    def test_the_frontier_log_supplies_the_downgrade_count(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()],
                     frontier=[{"run_id": "r1", "action_original": "delete"}])
        file_it(paths)
        assert entries(paths)[0]["policy_downgrades"] == 1

    def test_no_frontier_log_nulls_the_count_and_says_so(self, tmp_path):
        """STU-912's rule: a family that never ran is its own outcome, never a zero."""
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        entry = entries(paths)[0]
        assert entry["policy_downgrades"] is None
        assert "frontier" in entry["schema_note"]
        assert check_audit_entries([entry]) == []


class TestWhatFilingMustNotSwallow:
    def test_an_unreadable_report_does_not_cost_the_others_their_entry(self, tmp_path):
        """The write is after the loop, so raising here would file nothing at all."""
        paths = tree(tmp_path, reports=[push_report("r1"), push_report("r2")])
        (paths["reports_dir"] / "truncated.json").write_text('{"run_id": "r3"',
                                                             encoding="utf-8")
        assert file_it(paths) == ["r1", "r2"]

    def test_no_frontier_log_says_the_violation_count_is_partial(self, tmp_path):
        """STU-912's rule: a count from a family that never ran is not a clean count."""
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        note = entries(paths)[0]["schema_note"]
        assert "violations counts the report-internal checks only" in note

    def test_a_filed_entry_protects_no_artifact_from_the_prune(self, tmp_path):
        """Otherwise every shelf freezes: one exempting entry per notification batch."""
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        protected, _ = retention.audit_protection(entries(paths), tmp_path)
        assert protected == set()

    def test_a_reviewed_entry_protects_its_artifacts_again(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        reviewed = entries(paths)
        reviewed[0]["verdict"] = "clean — read 2026-08-30"
        assert retention.audit_protection(reviewed, tmp_path)[0] == {"r1"}


class TestReconcileAcrossAPull:
    def test_a_filed_entry_the_pull_lacks_is_not_lost_work(self):
        """It is derived from a report still on disk; `make audit-file` writes it back."""
        filed = json.dumps([audit_entry(run_id="r1", verdict=UNREVIEWED_VERDICT)])
        assert reconcile_audits.lost_entries(filed, "[]") == []

    def test_a_human_verdict_the_pull_lacks_still_refuses(self):
        written = json.dumps([audit_entry(run_id="r1", verdict="clean, 3 messages")])
        lost = reconcile_audits.lost_entries(written, "[]")
        assert [e["run_id"] for e in lost] == ["r1"]


class TestWhatTheEntryRefusesToClaim:
    def test_an_escalating_run_that_priced_nothing_says_the_ratio_was_not_read(self, tmp_path):
        """STU-1057's family, and `check` exits 2 on it — an integer cannot carry that."""
        report = push_report(verdicts=[llm_verdict("m1")])
        paths = tree(tmp_path, reports=[report], frontier=[{"run_id": "r1"}])
        file_it(paths)
        assert "cache hit ratio was not read" in entries(paths)[0]["schema_note"]

    def test_a_priced_run_does_not_carry_that_note(self, tmp_path):
        report = push_report(verdicts=[llm_verdict("m1")])
        report["cost"] = {cost.CLASSIFY_LEG: cost.leg([{"token_usage": {"by_model": {
            "claude-opus-4-8": {"input_tokens": 10, "output_tokens": 1,
                                "cache_read_input_tokens": 5,
                                "cache_creation_input_tokens": 2}}}}])}
        paths = tree(tmp_path, reports=[report], frontier=[{"run_id": "r1"}])
        file_it(paths)
        assert "cache hit ratio was not read" not in entries(paths)[0]["schema_note"]

    def test_a_killed_drafting_execution_says_the_spend_is_a_floor(self, tmp_path):
        """STU-1157: the entry outlives the report, so it is the last place to say it."""
        paths = tree(tmp_path, reports=[push_report()],
                     drafts=[{"run_id": "r1", "status": "started"},
                             {"run_id": "r1", "status": "started"},
                             {"run_id": "r1", "status": "completed", "drafted": 4,
                              "tokens": DRAFTING_SPEND}])
        file_it(paths)
        note = entries(paths)[0]["schema_note"]
        assert "1 execution(s) of the drafting leg under r1" in note
        assert "floors" in note

    def test_a_drafting_leg_that_closed_every_execution_does_not(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()],
                     drafts=[{"run_id": "r1", "status": "started"},
                             {"run_id": "r1", "status": "completed", "drafted": 4,
                              "tokens": DRAFTING_SPEND}])
        file_it(paths)
        assert "drafting leg" not in entries(paths)[0]["schema_note"]

    def test_a_run_whose_only_execution_was_killed_is_not_a_run_that_never_drafted(
            self, tmp_path):
        """It priced nothing, so there is no leg to read the count off of."""
        paths = tree(tmp_path, reports=[push_report()],
                     drafts=[{"run_id": "r1", "status": "started"}])
        file_it(paths)
        assert "1 execution(s) of the drafting leg under r1" in \
            entries(paths)[0]["schema_note"]

    def test_a_log_that_names_another_run_does_not_supply_a_zero(self, tmp_path):
        """A run that downgraded nothing wrote lines; one the log misses wrote none."""
        paths = tree(tmp_path, reports=[push_report()],
                     frontier=[{"run_id": "someone-else", "action_original": "delete"}])
        file_it(paths)
        entry = entries(paths)[0]
        assert entry["policy_downgrades"] is None
        assert "frontier" in entry["schema_note"]

    def test_a_malformed_frontier_line_nulls_the_count_rather_than_aborting(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        paths["frontier_log"].write_text('{"run_id": "r1"}\n{oops\n', encoding="utf-8")
        assert file_it(paths) == ["r1"]
        assert entries(paths)[0]["policy_downgrades"] is None

    def test_the_date_is_the_reports_own_not_the_day_of_the_walk(self, tmp_path):
        """`make audit-file` catching up on a week must not file it all as one day."""
        paths = tree(tmp_path, reports=[push_report()])
        report = paths["reports_dir"] / "r1.json"
        old = datetime.datetime(2026, 8, 20, 12, 0).timestamp()
        os.utime(report, (old, old))
        file_it(paths)
        assert entries(paths)[0]["date"] == "2026-08-20"

    def test_the_write_leaves_no_temp_file_behind(self, tmp_path):
        paths = tree(tmp_path, reports=[push_report()])
        file_it(paths)
        assert not list(paths["audit_log"].parent.glob("*.tmp"))
