"""Tests for email_classifier.audit — run-audit checks over report/log/dump dicts."""

from pathlib import Path

import pytest

from email_classifier.core.contract import load_taxonomy
from email_classifier.audit import (
    check_audit_entries,
    check_cache_hits,
    check_dump_consistency,
    check_log_consistency,
    check_report,
    check_schema_note_pins,
    check_window_verdicts,
    draft_activity,
    metrics,
    rule_stats,
    unaudited_runs,
)
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")
# Subtype vocabularies are each deployment's own; bucket A exists in every one.
A_SUBTYPE = sorted(TAXONOMY.allowed_subtypes["A"])[0]


def det_verdict(mid="m1", rule_id="kobo_promo", bucket="C", action="label_archive"):
    return {
        "message_id": mid, "thread_id": f"t-{mid}", "path": "deterministic",
        "status": "ok", "bucket": bucket, "suggested_action": action,
        "confidence": "high", "rule_id": rule_id, "subtype": None,
        "reasoning": None, "error": None,
    }


def llm_verdict(mid="m2", bucket="A", action="keep_visible", subtype=A_SUBTYPE,
                thread_id=None):
    return {
        "message_id": mid, "thread_id": thread_id or f"t-{mid}", "path": "llm",
        "status": "ok", "bucket": bucket, "suggested_action": action,
        "confidence": "high", "rule_id": "catch_all", "subtype": subtype,
        "reasoning": "why", "error": None,
    }


def error_verdict(mid="m3", tag="studio_run_timeout"):
    return {
        "message_id": mid, "thread_id": f"t-{mid}", "path": "llm",
        "status": "error", "bucket": None, "suggested_action": None,
        "confidence": None, "rule_id": "catch_all", "subtype": None,
        "reasoning": None, "error": tag,
    }


def make_report(verdicts):
    det = sum(1 for v in verdicts if v["path"] == "deterministic")
    return {
        "run_id": "run-1",
        "totals": {
            "emails": len(verdicts),
            "deterministic": det,
            "llm": len(verdicts) - det,
            "llm_calls": len({v["thread_id"] for v in verdicts if v["path"] == "llm"}),
            "errors": sum(1 for v in verdicts if v["status"] == "error"),
        },
        "ratio_deterministic": det / len(verdicts) if verdicts else 0.0,
        "by_bucket": {}, "by_action": {},
        "verdicts": verdicts,
    }


def log_entry(mid, action, rule_id="kobo_promo", action_original=None, run_id="run-1"):
    return {
        "run_id": run_id, "timestamp": "2026-08-18T00:00:00+00:00",
        "message_id": mid, "thread_id": f"t-{mid}", "rule_id": rule_id,
        "action": action, "action_original": action_original,
        "confidence": "high", "bucket": "C",
    }


class TestCheckReport:
    def test_clean_report_passes(self):
        report = make_report([det_verdict(), llm_verdict(), error_verdict()])
        assert check_report(report, TAXONOMY) == []

    def test_duplicate_message_id(self):
        report = make_report([det_verdict("m1"), det_verdict("m1")])
        assert any("duplicate message_id" in e for e in check_report(report, TAXONOMY))

    def test_totals_mismatch(self):
        report = make_report([det_verdict()])
        report["totals"]["emails"] = 5
        assert any("totals.emails" in e for e in check_report(report, TAXONOMY))

    def test_llm_calls_mismatch(self):
        report = make_report([llm_verdict("m1", thread_id="t"), llm_verdict("m2", thread_id="t")])
        assert report["totals"]["llm_calls"] == 1
        report["totals"]["llm_calls"] = 2
        assert any("totals.llm_calls" in e for e in check_report(report, TAXONOMY))

    def test_a_report_without_llm_calls_is_not_a_violation(self):
        """Written before STU-989: the key is missing, not wrong."""
        report = make_report([llm_verdict("m1")])
        del report["totals"]["llm_calls"]
        assert check_report(report, TAXONOMY) == []

    def test_inv08_violation(self):
        report = make_report([llm_verdict(bucket="A", action="mark_for_delete")])
        assert any("not allowed for bucket 'A'" in e for e in check_report(report, TAXONOMY))

    def test_llm_verdict_from_non_catch_all_rule(self):
        v = llm_verdict()
        v["rule_id"] = "kobo_promo"
        assert any("not catch_all" in e for e in check_report(make_report([v]), TAXONOMY))

    def test_deterministic_verdict_from_catch_all(self):
        v = det_verdict(rule_id="catch_all")
        assert any("chokepoint" in e for e in check_report(make_report([v]), TAXONOMY))

    def test_unknown_error_tag(self):
        report = make_report([error_verdict(tag="mystery_failure")])
        assert any("unknown error tag" in e for e in check_report(report, TAXONOMY))

    def test_llm_ok_without_reasoning(self):
        v = llm_verdict()
        v["reasoning"] = None
        assert any("missing subtype/reasoning" in e for e in check_report(make_report([v]), TAXONOMY))


class TestCheckLogConsistency:
    def test_matching_log_passes(self):
        report = make_report([det_verdict("m1"), llm_verdict("m2")])
        log = [log_entry("m1", "label_archive"),
               log_entry("m2", "escalate_to_llm", rule_id="catch_all")]
        assert check_log_consistency(report, log) == []

    def test_no_entries_for_run(self):
        report = make_report([det_verdict("m1")])
        log = [log_entry("m1", "label_archive", run_id="other-run")]
        assert check_log_consistency(report, log) == ["no frontier-log entries for run run-1"]

    def test_no_entries_for_zero_verdict_run_is_consistent(self):
        """STU-1334: 0 frontier-log lines matching 0 verdicts is not a violation."""
        report = make_report([])
        log = [log_entry("m1", "label_archive", run_id="other-run")]
        assert check_log_consistency(report, log) == []

    def test_missing_log_line(self):
        report = make_report([det_verdict("m1"), det_verdict("m2")])
        log = [log_entry("m1", "label_archive")]
        assert any("absent from frontier log" in e for e in check_log_consistency(report, log))

    def test_escalation_mismatch(self):
        report = make_report([det_verdict("m1")])
        log = [log_entry("m1", "escalate_to_llm", rule_id="catch_all")]
        assert any("escalation mismatch" in e for e in check_log_consistency(report, log))


class TestCheckDumpConsistency:
    def test_exact_match_passes(self):
        report = make_report([det_verdict("m1")])
        assert check_dump_consistency(report, [{"message_id": "m1"}]) == []

    def test_dump_message_without_verdict(self):
        report = make_report([det_verdict("m1")])
        dump = [{"message_id": "m1"}, {"message_id": "m9"}]
        assert any("without a verdict" in e for e in check_dump_consistency(report, dump))


class TestMetrics:
    def test_error_tags_and_downgrades(self):
        report = make_report([det_verdict("m1"), error_verdict("m2")])
        log = [log_entry("m1", "label_archive", action_original="mark_for_delete"),
               log_entry("m2", "escalate_to_llm", rule_id="catch_all")]
        m = metrics(report, log)
        assert m["error_tags"] == {"studio_run_timeout": 1}
        assert m["policy_downgrades"] == 1

    def test_absent_log_nulls_the_downgrade_count_with_its_reason(self):
        """STU-912: a dropped key is filled back in as a zero nothing can flag."""
        m = metrics(make_report([det_verdict("m1")]), None, "logs/frontier.jsonl")
        assert m["policy_downgrades"] is None
        assert "logs/frontier.jsonl" in m["schema_note"]

    def test_a_computed_downgrade_count_carries_no_note(self):
        m = metrics(make_report([det_verdict("m1")]), [log_entry("m1", "label_archive")])
        assert m["policy_downgrades"] == 0
        assert "schema_note" not in m

    def test_llm_calls_is_derived_not_read_from_totals(self):
        """A pre-STU-989 report still reports its real LLM cost, not a missing key."""
        report = make_report([
            llm_verdict("m1", thread_id="t"), llm_verdict("m2", thread_id="t"),
        ])
        del report["totals"]["llm_calls"]
        assert metrics(report)["totals"]["llm_calls"] == 1

    def test_by_subtype_counts_one_family_once(self):
        report = make_report([
            llm_verdict("m1", subtype="security_account"),
            llm_verdict("m2", subtype="security_account"),
            llm_verdict("m3", subtype="human_real"),
            det_verdict("m4"),
        ])
        assert metrics(report)["by_subtype"] == {"human_real": 1, "security_account": 2}


class TestRuleStats:
    def test_fired_and_catch_all_share(self):
        report = make_report([det_verdict("m1"), det_verdict("m2"), llm_verdict("m3")])
        stats = rule_stats(report, ["kobo_promo", "spa_spam", "catch_all"], {"kobo_promo"})
        assert stats["fired"]["kobo_promo"] == 2
        assert stats["catch_all_share"] == pytest.approx(1 / 3)

    def test_quiet_rule_split_by_corpus(self):
        report = make_report([det_verdict("m1")])
        rule_ids = ["kobo_promo", "spa_spam", "rbc_alert", "catch_all"]
        stats = rule_stats(report, rule_ids, {"kobo_promo", "spa_spam"})
        assert stats["unexercised_by_this_run"] == ["spa_spam"]
        assert stats["never_fires"] == ["rbc_alert"]


class TestUnauditedRuns:
    def test_all_audited(self):
        assert unaudited_runs(["r1", "r2"], [{"run_id": "r2"}, {"run_id": "r1"}]) == []

    def test_reports_order_preserved(self):
        assert unaudited_runs(["r1", "r2", "r3"], [{"run_id": "r2"}]) == ["r1", "r3"]

    def test_empty_audit_log(self):
        assert unaudited_runs(["r1"], []) == ["r1"]

    def test_audit_entry_without_report_is_not_reported(self):
        assert unaudited_runs(["r1"], [{"run_id": "r1"}, {"run_id": "gone"}]) == []

    def test_a_skip_marker_excuses_a_run(self):
        skipped = [{"run_id": "r1", "date": "2026-08-27", "reason": "smoke run, MAX=1"}]
        assert unaudited_runs(["r1", "r2"], [{"run_id": "r2"}], skipped) == []

    def test_a_marker_naming_no_reason_excuses_nothing(self):
        assert unaudited_runs(["r1"], [], [{"run_id": "r1", "reason": "  "}]) == ["r1"]
        assert unaudited_runs(["r1"], [], [{"run_id": "r1"}]) == ["r1"]


def draft_row(run_id="r1", message_id="m1", status="ok", **overrides):
    return {"timestamp": "2026-08-28T00:00:00+00:00", "run_id": run_id,
            "message_id": message_id, "thread_id": f"t-{message_id}",
            "draft_id": f"d-{message_id}", "updated": False, "status": status,
            "error": None, **overrides}


class TestDraftActivity:
    def test_only_the_named_run_is_counted(self):
        entries = [draft_row(), draft_row(run_id="r2", message_id="m2")]
        activity = draft_activity(entries, "r1")
        assert (activity["created"], activity["updated"]) == (1, 0)
        assert activity["problems"] == []

    def test_an_update_in_place_is_not_a_new_draft(self):
        entries = [draft_row(), draft_row(message_id="m2", updated=True)]
        activity = draft_activity(entries, "r1")
        assert (activity["created"], activity["updated"]) == (1, 1)

    def test_every_non_ok_row_is_named_with_its_draft_id(self):
        entries = [draft_row(message_id="m1", status="error", draft_id=None, error="boom"),
                   draft_row(message_id="m2", status="superseded"),
                   draft_row(message_id="m3", status="vanished"),
                   draft_row(message_id="m4", status="deleted")]
        problems = draft_activity(entries, "r1")["problems"]
        assert [(p["status"], p["draft_id"]) for p in problems] == [
            ("error", None), ("superseded", "d-m2"), ("vanished", "d-m3"),
            ("deleted", "d-m4")]
        assert problems[0]["error"] == "boom"

    def test_a_sent_draft_is_counted_apart_from_a_vanished_one(self):
        """STU-1549: both left Drafts, only one was lost."""
        entries = [draft_row(message_id="m1", status="sent"),
                   draft_row(message_id="m2", status="vanished"),
                   draft_row(message_id="m3", status="vanished")]
        activity = draft_activity(entries, "r1")
        assert (activity["sent"], activity["vanished"]) == (1, 2)
        assert [p["message_id"] for p in activity["problems"]] == ["m2", "m3"]

    def test_a_row_naming_no_run_is_unattributable_not_this_run_s(self):
        entries = [draft_row(), draft_row(run_id=None, message_id="m2", status="deleted")]
        activity = draft_activity(entries, "r1")
        assert activity["created"] == 1
        assert activity["problems"] == []
        assert activity["unattributable"] == {"rows": 1, "draft_ids": ["d-m2"]}

    def test_a_draft_id_orphaned_twice_is_listed_once(self):
        entries = [draft_row(run_id=None, message_id="m1", draft_id="d1"),
                   draft_row(run_id=None, message_id="m1", draft_id="d1", updated=True)]
        assert draft_activity(entries, "r1")["unattributable"] == {"rows": 2,
                                                                  "draft_ids": ["d1"]}

    def test_an_unattributable_row_carrying_no_draft_is_still_counted(self):
        entries = [draft_row(run_id=None, status="error", draft_id=None)]
        assert draft_activity(entries, "r1")["unattributable"] == {"rows": 1,
                                                                  "draft_ids": []}


def audit_entry(**overrides):
    entry = {
        "run_id": "r1", "date": "2026-08-19", "dump": "dumps/d.json",
        "provider": "claude-code", "git_commit": "abc123",
        "emails": 10, "ratio_deterministic": 0.9, "catch_all_share": 0.1,
        "errors": 0, "error_tags": {}, "policy_downgrades": 0,
        "violations": 0, "never_fires": [], "unexercised_by_this_run": [],
        "by_bucket": {}, "verdict": "clean", "new_issues": [],
    }
    entry.update(overrides)
    return entry


class TestCheckWindowVerdicts:
    VERDICT = "Reviewed in window audit audits/windows/w.md (read in aggregate)."

    def test_named_file_listing_the_run_passes(self, tmp_path):
        (tmp_path / "audits/windows").mkdir(parents=True)
        (tmp_path / "audits/windows/w.md").write_text("runs: r1")
        assert check_window_verdicts([audit_entry(verdict=self.VERDICT)], tmp_path) == []

    def test_missing_file_is_a_violation(self, tmp_path):
        errors = check_window_verdicts([audit_entry(verdict=self.VERDICT)], tmp_path)
        assert errors == ["entry r1: window audit audits/windows/w.md does not exist"]

    def test_file_that_does_not_list_the_run_is_a_violation(self, tmp_path):
        (tmp_path / "audits/windows").mkdir(parents=True)
        (tmp_path / "audits/windows/w.md").write_text("runs: r2")
        errors = check_window_verdicts([audit_entry(verdict=self.VERDICT)], tmp_path)
        assert errors == ["entry r1: window audit audits/windows/w.md does not list this run"]

    def test_ordinary_verdict_is_ignored(self, tmp_path):
        assert check_window_verdicts([audit_entry()], tmp_path) == []


class TestCheckAuditEntries:
    def test_valid_entry(self):
        assert check_audit_entries([audit_entry()]) == []

    def test_renamed_key_reports_both_sides(self):
        entry = audit_entry()
        entry["dead_rules"] = entry.pop("never_fires")
        drift = check_audit_entries([entry])
        assert any("missing key never_fires" in d for d in drift)
        assert any("unknown key dead_rules" in d for d in drift)

    def test_wrong_type(self):
        drift = check_audit_entries([audit_entry(error_tags=[], emails="10")])
        assert any("error_tags is list, expected dict" in d for d in drift)
        assert any("emails is str, expected int" in d for d in drift)

    def test_bool_is_not_a_number(self):
        drift = check_audit_entries([audit_entry(violations=True)])
        assert any("violations is bool" in d for d in drift)

    def test_int_accepted_for_ratio(self):
        assert check_audit_entries([audit_entry(ratio_deterministic=1)]) == []

    def test_null_metric_needs_schema_note(self):
        drift = check_audit_entries([audit_entry(never_fires=None)])
        assert drift == ["entry 0 (r1): never_fires is null without a schema_note"]

    def test_null_metric_with_schema_note(self):
        entry = audit_entry(never_fires=None, schema_note="pre-STU-825: never computed")
        assert check_audit_entries([entry]) == []

    def test_a_null_downgrade_count_is_a_valid_entry_with_a_note(self):
        """The audit log takes what `metrics` emits for an absent log (STU-912)."""
        entry = audit_entry(policy_downgrades=None, schema_note="no frontier log on disk")
        assert check_audit_entries([entry]) == []
        assert check_audit_entries([audit_entry(policy_downgrades=None)]) == [
            "entry 0 (r1): policy_downgrades is null without a schema_note"
        ]

    def test_null_not_tolerated_on_other_keys(self):
        drift = check_audit_entries([audit_entry(by_bucket=None, schema_note="note")])
        assert any("by_bucket is NoneType, expected dict" in d for d in drift)

    def test_git_commit_null_needs_no_note(self):
        assert check_audit_entries([audit_entry(git_commit=None)]) == []

    def test_entry_index_in_message(self):
        drift = check_audit_entries([audit_entry(), audit_entry(run_id="r2", verdict=None)])
        assert drift == ["entry 1 (r2): verdict is NoneType, expected str"]


RULES_AT = {("a78d79d8", "rules/rules.yaml"): "4b4c159fa8b9" + "0" * 52}


def resolver(mapping):
    return lambda sha, path: mapping.get((sha, path))


class TestCheckSchemaNotePins:
    """The pinned sha of a schema_note claim, re-derived rather than trusted (STU-914)."""

    def test_a_pin_that_hashes_to_the_quoted_hash_passes(self):
        note = ("the run pins rules_sha256 4b4c159f, byte-identical to "
                "rules/rules.yaml at a78d79d8, so the staleness replay is exact.")
        entry = audit_entry(schema_note=note)
        assert check_schema_note_pins([entry], resolver(RULES_AT)) == []

    def test_a_pin_that_hashes_to_something_else_names_both_hashes(self):
        note = ("the run pins rules_sha256 deadbeef, byte-identical to "
                "rules/rules.yaml at a78d79d8.")
        drift = check_schema_note_pins([audit_entry(schema_note=note)], resolver(RULES_AT))
        assert drift == ["entry 0 (r1): schema_note pins rules/rules.yaml at a78d79d8, "
                         "which hashes to 4b4c159fa8b9, but the note quotes deadbeef"]

    def test_an_unreachable_sha_is_a_violation_not_a_skip(self):
        note = "byte-identical to rules/rules.yaml at 0badc0de (4b4c159f)."
        drift = check_schema_note_pins([audit_entry(schema_note=note)], resolver(RULES_AT))
        assert drift == ["entry 0 (r1): schema_note pins rules/rules.yaml at 0badc0de, "
                         "a commit not reachable in this repo"]

    def test_the_hash_may_be_quoted_after_the_pin(self):
        note = "replayed against rules/rules.yaml at a78d79d8 (4b4c159f), not the run's."
        assert check_schema_note_pins([audit_entry(schema_note=note)], resolver(RULES_AT)) == []

    def test_the_nearest_hash_is_the_asserted_one(self):
        # A note that names a second hash to say the replay ran against a *different*
        # rule set asserts the one it wrote next to the pin, not the one it disowns.
        note = ("replayed against rules/rules.yaml at a78d79d8 (4b4c159f), the HEAD this "
                "audit ran against, not the rule set the run pinned (rules_sha256 0badc0de)")
        assert check_schema_note_pins([audit_entry(schema_note=note)], resolver(RULES_AT)) == []

    def test_prose_without_a_pin_passes(self):
        note = ("this run predates the rules/ tree at e4b70d48, and ran on a dirty tree "
                "(provenance.git_dirty true) at 04097b2.")
        assert check_schema_note_pins([audit_entry(schema_note=note)], resolver({})) == []

    def test_an_entry_without_a_note_passes(self):
        assert check_schema_note_pins([audit_entry()], resolver({})) == []

    def test_every_pin_in_a_note_is_checked(self):
        note = ("rules/rules.yaml at a78d79d8 (4b4c159f) and .studio/x.yaml at a78d79d8")
        drift = check_schema_note_pins([audit_entry(schema_note=note)], resolver(RULES_AT))
        assert drift == ["entry 0 (r1): schema_note pins .studio/x.yaml at a78d79d8, "
                         "a commit not reachable in this repo"]


class TestCacheHits:
    """A leg that stopped reading its prefix back (STU-1057)."""

    def test_a_leg_reading_nothing_back_is_a_violation(self):
        violations, notes = check_cache_hits({"drafts": (0.0, 14, 8000, "anthropic")})
        assert violations == ["drafts made 14 call(s) and read none of its prefix back "
                              "(cache_hit_ratio 0.0) — the leg is paying cache-write on "
                              "a prefix nothing reads"]
        assert notes == []

    def test_a_leg_that_wrote_no_prefix_is_told_apart_from_one_that_paid_for_it(self):
        # Zero hits has two shapes (STU-1080). Run `d2592ee5` wrote no block at all —
        # the haiku default's 4096-token floor over a ~2.6k prefix — so the leg paid
        # no cache-write either, and an audit told it did hunts the wrong defect.
        violations, _ = check_cache_hits({"classify": (0.0, 3, 0, "anthropic")})
        assert violations == ["classify made 3 call(s) and cached nothing at all "
                              "(cache_hit_ratio 0.0, cache_creation 0) — no prefix was "
                              "ever written, which a model whose minimum cacheable "
                              "prefix is above this leg's does"]

    def test_a_single_call_cannot_have_read_anything_back(self):
        # A first call can only write the prefix, so its 0.0 is arithmetic. Failing
        # here would fire on every small push run.
        assert check_cache_hits({"classify": (0.0, 1, 8000, "anthropic")}) == ([], [])

    def test_a_leg_that_reported_no_input_makes_no_claim(self):
        assert check_cache_hits({"classify": (None, 5, 0, "anthropic")}) == ([], [])

    def test_a_leg_reading_any_of_it_back_passes(self):
        # Zero and not a tuned floor: the two ratios ever measured are 0.30 and 0.778.
        assert check_cache_hits({"classify": (0.03, 26, 8000, "anthropic")}) == ([], [])

    def test_a_provider_that_cannot_reuse_a_prefix_is_a_note_and_not_a_violation(self):
        # STU-1145. Under `claude-code` each fan-out leaf is a fresh process making one
        # call, so no pin, contract or model reachable from this repo makes the write
        # readable — measured 0.0 over 12 calls and again over 27 (STU-1133). Filing it
        # as a violation is filing the same line on every batch run forever.
        violations, notes = check_cache_hits({"classify": (0.0, 12, 8000, "claude-code")})
        assert violations == []
        assert notes == ["classify made 12 call(s) at cache_hit_ratio 0.0 under provider "
                         "`claude-code`, which spawns a process per call — the prefix "
                         "each one writes cannot be read back by another, so this is "
                         "structural and not a leg that lost its caching"]

    def test_that_provider_still_violates_when_it_wrote_no_prefix_at_all(self):
        # The exemption covers one of STU-1080's two shapes and not the other: a prefix
        # never written is the model's minimum cacheable prefix, which is actionable
        # under every provider. Collapsing the two would lose the repair with the noise.
        violations, notes = check_cache_hits({"classify": (0.0, 12, 0, "claude-code")})
        assert notes == []
        assert violations == ["classify made 12 call(s) and cached nothing at all "
                              "(cache_hit_ratio 0.0, cache_creation 0) — no prefix was "
                              "ever written, which a model whose minimum cacheable "
                              "prefix is above this leg's does"]

    def test_a_leg_with_no_recorded_provider_keeps_the_old_behaviour(self):
        # A leg written before STU-1145. Unknown must not be the way through, or the
        # escape hatch is one dropped field wide.
        violations, notes = check_cache_hits({"classify": (0.0, 12, 8000, None)})
        assert notes == []
        assert len(violations) == 1
