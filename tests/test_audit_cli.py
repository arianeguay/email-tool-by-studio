"""What `audit_run.py` resolves an audit against and what it refuses to take on trust:
the input a run consumed (STU-859), the rule set it pinned (STU-913), the exit for a
check family that could not run (STU-912), the shas a schema_note pins (STU-914),
what excuses a run from being audited at all (STU-900), and which artifacts a cost is
priced off — the run's own legs (STU-1012), the Studio log behind them (STU-1011), and
which rows of the shared drafts journal a run may claim (STU-1029) — and which
subcommands read `--run` at all, the rest refusing it rather than answering about
the newest report under the run id they were handed (STU-1067)."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from email_classifier.orchestrate.provenance import LIVE_INPUT
from scripts import audit_run
from tests.conftest import REPO_ROOT
from tests.test_audit import audit_entry, draft_row, llm_verdict
from email_classifier import deployment

SCRIPT = REPO_ROOT / "scripts" / "audit_run.py"


def _dump(tmp_path, name="dump.json"):
    path = tmp_path / name
    path.write_text(json.dumps([{"message_id": "m1"}]), encoding="utf-8")
    return path


def _report(path=None, sha=None):
    provenance = {"input_path": str(path) if path else None, "input_sha256": sha}
    return {"run_id": "r1", "provenance": provenance}


def test_resolve_defaults_to_the_recorded_input(tmp_path):
    dump = _dump(tmp_path)
    assert audit_run._resolve_dump(None, _report(dump)) == dump


def test_resolve_aborts_when_the_recorded_input_moved(tmp_path):
    with pytest.raises(SystemExit):
        audit_run._resolve_dump(None, _report(tmp_path / "gone.json"))


def test_resolve_skips_a_report_written_before_the_field(tmp_path, capsys):
    assert audit_run._resolve_dump(None, {"run_id": "r1"}) is None
    assert "pass --dump" in capsys.readouterr().err


def test_resolve_skips_a_live_input_with_no_artifact(capsys):
    assert audit_run._resolve_dump(None, _report(LIVE_INPUT)) is None
    assert LIVE_INPUT in capsys.readouterr().err


def test_an_explicit_dump_overrides_the_recorded_one(tmp_path, capsys):
    chosen = _dump(tmp_path, "other.json")
    assert audit_run._resolve_dump(chosen, _report(tmp_path / "dump.json")) == chosen
    assert "overrides recorded input" in capsys.readouterr().err


def test_a_rewritten_dump_is_a_violation(tmp_path):
    dump = _dump(tmp_path)
    stale = hashlib.sha256(b"[]").hexdigest()
    assert audit_run._dump_hash_violations(_report(dump, stale), dump)
    fresh = hashlib.sha256(dump.read_bytes()).hexdigest()
    assert audit_run._dump_hash_violations(_report(dump, fresh), dump) == []


def test_a_corpus_directory_loads_as_message_records():
    records = audit_run._load_input(deployment.ROOT / "fixtures")
    assert records and all("message_id" in r for r in records)


def _run(monkeypatch, tmp_path, command, report, log=None, drafts_log=None, extra=()):
    """`audit_run.main()` over a report on disk, with the frontier log optional."""
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "run-1.json").write_text(json.dumps(report), encoding="utf-8")
    argv = ["audit_run.py", command, "--reports", str(reports),
            "--log", str(log or tmp_path / "frontier.jsonl"),
            # Never the repo's own: a run of this machine's could name run-1.
            "--drafts-log", str(drafts_log or tmp_path / "stages.jsonl"), *extra]
    monkeypatch.setattr(audit_run.sys, "argv", argv)
    # The CLI reads the deployment's rules off its cwd.
    monkeypatch.chdir(deployment.ROOT)
    return audit_run.main()


def _one_verdict_report():
    return {
        "run_id": "run-1", "provenance": {"input_path": LIVE_INPUT},
        "totals": {"emails": 1, "deterministic": 1, "llm": 0, "errors": 0},
        "ratio_deterministic": 1.0, "by_bucket": {}, "by_action": {},
        "verdicts": [{
            "message_id": "m1", "thread_id": "t1", "path": "deterministic", "status": "ok",
            "bucket": "C", "suggested_action": "label_archive", "confidence": "high",
            "rule_id": "kobo_promo", "subtype": None, "reasoning": None, "error": None,
        }],
    }


def test_metrics_without_a_log_emits_an_explicit_null(monkeypatch, tmp_path, capsys):
    """Never an output where the key is simply gone (STU-912)."""
    assert _run(monkeypatch, tmp_path, "metrics", _one_verdict_report()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["policy_downgrades"] is None
    assert "frontier.jsonl" in out["schema_note"]


def test_check_without_a_log_does_not_exit_zero(monkeypatch, tmp_path, capsys):
    """A skipped check family is its own exit code, not a passed audit (STU-912)."""
    assert _run(monkeypatch, tmp_path, "check", _one_verdict_report()) == 2
    assert "INCOMPLETE: no frontier log" in capsys.readouterr().out


def test_check_with_a_log_passes_clean(monkeypatch, tmp_path, capsys):
    log = tmp_path / "frontier.jsonl"
    log.write_text(json.dumps({
        "run_id": "run-1", "message_id": "m1", "action": "label_archive",
    }) + "\n", encoding="utf-8")
    # And no cost family missed either: a deterministic-only run spent nothing on
    # stage 2, so there is no leg whose ratio went unread (STU-1057).
    assert _run(monkeypatch, tmp_path, "check", _one_verdict_report(), log) == 0
    assert "INCOMPLETE" not in capsys.readouterr().out


def _escalated_report(spend, tmp_path):
    """A report whose one verdict escalated, priced by `spend` (or not at all).

    Returns it with the frontier log that agrees with it, so the cost family is the
    only one with anything to say.
    """
    report = _one_verdict_report()
    report["totals"] = {"emails": 1, "deterministic": 0, "llm": 1, "errors": 0}
    report["ratio_deterministic"] = 0.0
    report["verdicts"] = [llm_verdict(mid="m1")]
    if spend is not None:
        report["cost"] = {"classify": spend}
    log = tmp_path / "frontier.jsonl"
    log.write_text(json.dumps({
        "run_id": "run-1", "message_id": "m1", "action": "escalate_to_llm",
    }) + "\n", encoding="utf-8")
    return report, log


def test_check_fails_a_leg_that_reads_none_of_its_prefix_back(monkeypatch, tmp_path,
                                                              capsys):
    """The silence STU-1030 left: a 0.0 ratio, four times the bill, nothing said."""
    report, log = _escalated_report(_spend(calls=14, cache_creation_tokens=200_000), tmp_path)
    assert _run(monkeypatch, tmp_path, "check", report, log) == 1
    assert "VIOLATION: classify made 14 call(s) and read none" in capsys.readouterr().out


def test_check_names_the_shape_of_a_leg_that_wrote_no_prefix(monkeypatch, tmp_path,
                                                             capsys):
    """Run `d2592ee5`: pin in place, haiku default, no cache block written (STU-1080)."""
    report, log = _escalated_report(
        _spend(model="claude-haiku-4-5", calls=3, prompt_tokens=7758), tmp_path)
    assert _run(monkeypatch, tmp_path, "check", report, log) == 1
    out = capsys.readouterr().out
    assert "VIOLATION: classify made 3 call(s) and cached nothing at all" in out
    assert "paying cache-write" not in out


def test_check_passes_a_leg_still_reading_its_prefix_back(monkeypatch, tmp_path, capsys):
    report, log = _escalated_report(
        _spend(calls=14, cache_creation_tokens=200_000, cached_input_tokens=100_000),
        tmp_path)
    assert _run(monkeypatch, tmp_path, "check", report, log) == 0
    assert "cache_hit_ratio" not in capsys.readouterr().out


def test_check_flags_an_escalating_run_that_priced_nothing(monkeypatch, tmp_path, capsys):
    """A ratio nobody could read is not a ratio that held (STU-912, STU-1057)."""
    report, log = _escalated_report(None, tmp_path)
    assert _run(monkeypatch, tmp_path, "check", report, log) == 2
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out
    assert "records no classify leg at all" in out


def test_check_names_a_leg_whose_calls_died_before_reporting(monkeypatch, tmp_path,
                                                             capsys):
    """Run `22e73bc3`: one item, dead on a provider it could not initialize (STU-1167)."""
    report, log = _escalated_report(
        {"llm_calls": 1, "unpriced_calls": 1, "provider": "anthropic", "by_model": {}},
        tmp_path)
    assert _run(monkeypatch, tmp_path, "check", report, log) == 2
    out = capsys.readouterr().out
    assert "recorded 1 classify call(s) and priced none of them" in out
    assert "resume cache" not in out


def test_check_names_a_leg_the_resume_cache_served_whole(monkeypatch, tmp_path, capsys):
    """The healthy half of the same silence: nothing to price, nothing wrong (STU-1167).

    `classify-post` drops a resumed item rather than counting it unpriced, so a leg
    every item resumed records no calls — which is what tells it from the dead one.
    """
    report, log = _escalated_report(
        {"llm_calls": 0, "unpriced_calls": 0, "provider": "anthropic", "by_model": {}},
        tmp_path)
    assert _run(monkeypatch, tmp_path, "check", report, log) == 2
    out = capsys.readouterr().out
    assert "made no calls of its own" in out
    assert "resume cache" in out
    assert "died before reporting usage" not in out


# --- the rule set a `rules` replay scores against (STU-913) ---------------------------

RULES_FILE = deployment.ROOT / "rules" / "rules.yaml"


def _rules_report(sha="deadbeef" * 8, commit=None, verdicts=None):
    return {
        "run_id": "r1",
        "verdicts": verdicts if verdicts is not None else [],
        "provenance": {"rules_sha256": sha, "git_commit": commit},
    }


def _pinned_sha():
    return hashlib.sha256(RULES_FILE.read_bytes()).hexdigest()


def test_a_matching_rules_sha_replays_the_working_tree_silently(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(deployment.ROOT)
    path, source = audit_run._resolve_rules(None, _rules_report(_pinned_sha()), tmp_path)
    assert path == Path("rules/rules.yaml")
    assert capsys.readouterr().err == ""
    assert "matches" in source


def test_a_stale_rules_sha_is_recovered_from_the_runs_commit(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(deployment.ROOT)
    commit = "cc70f4b"
    blob = audit_run._file_at_commit(commit, "rules/rules.yaml")
    if blob is None:
        pytest.skip("shallow checkout: the commit that pinned the older rule set is absent")
    report = _rules_report(hashlib.sha256(blob).hexdigest(), commit)
    path, source = audit_run._resolve_rules(None, report, tmp_path)
    assert path is not None and path.read_bytes() == blob
    assert commit in source and commit in capsys.readouterr().err


def test_an_unrecoverable_rule_set_is_not_replayed_at_all(tmp_path, capsys):
    path, source = audit_run._resolve_rules(None, _rules_report(commit=None), tmp_path)
    assert path is None
    assert "unrecoverable" in source
    assert "not computed" in capsys.readouterr().err


def test_a_commit_whose_rules_do_not_hash_to_the_pinned_set_is_unrecoverable(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(deployment.ROOT)
    path, _ = audit_run._resolve_rules(None, _rules_report(commit="cc70f4b"), tmp_path)
    assert path is None
    assert "not computed" in capsys.readouterr().err


def test_a_report_without_rules_sha256_keeps_todays_rules_with_a_caveat(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(deployment.ROOT)
    path, source = audit_run._resolve_rules(None, {"run_id": "r1"}, tmp_path)
    assert path == Path("rules/rules.yaml")
    assert "pre-STU-819" in source
    assert "before STU-819" in capsys.readouterr().err


def test_an_explicit_rules_file_overrides_the_pinned_one_with_a_warning(tmp_path, capsys):
    path, source = audit_run._resolve_rules(RULES_FILE, _rules_report(), tmp_path)
    assert path == RULES_FILE
    assert "named on the command line" in source
    assert "not the rule set the run pinned" in capsys.readouterr().err


def test_rules_surfaces_a_stale_rule_set_instead_of_scoring_against_it(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(deployment.ROOT)
    assert _run(monkeypatch, tmp_path, "rules", _rules_report()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["never_fires"] is None
    assert out["unexercised_by_this_run"] is None
    assert "unrecoverable" in out["rules_source"]


def test_rules_on_the_pinned_rule_set_reports_the_full_staleness_split(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.chdir(deployment.ROOT)
    # No --corpus: the default is the deployment's own fixtures/ (STU-2014).
    assert _run(monkeypatch, tmp_path, "rules", _rules_report(_pinned_sha())) == 0
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert captured.err == ""
    assert isinstance(out["never_fires"], list)
    assert isinstance(out["unexercised_by_this_run"], list)
    assert "matches" in out["rules_source"]


# --- the shas a `schema_note` pins (STU-914) ----------------------------------------

def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit_rules(repo, body):
    """Commit `body` as rules/rules.yaml; return (commit sha, its sha256)."""
    (repo / "rules").mkdir(exist_ok=True)
    (repo / "rules" / "rules.yaml").write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rules")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return sha, hashlib.sha256(body.encode()).hexdigest()


def _pinning_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.io")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "audits").mkdir()
    return tmp_path


def _write_log(repo, note):
    path = repo / "audits" / "audit_log.json"
    path.write_text(json.dumps([audit_entry(schema_note=note)]), encoding="utf-8")
    return path


def _log_check(audit_log):
    return subprocess.run([sys.executable, str(SCRIPT), "log-check", "--audit-log", str(audit_log)],
                          capture_output=True, text=True)


def test_log_check_passes_a_note_whose_pin_bears_out(tmp_path):
    repo = _pinning_repo(tmp_path)
    sha, digest = _commit_rules(repo, "rules: [a]\n")
    log = _write_log(repo, f"byte-identical to rules/rules.yaml at {sha[:8]} ({digest[:8]}).")
    result = _log_check(log)
    assert result.returncode == 0, result.stdout


def test_log_check_fails_a_note_pinning_a_sha_that_hashes_to_something_else(tmp_path):
    # STU-899's failure mode, mechanised: the note quotes the rule set the run used and
    # anchors it to a commit that carries a different one.
    repo = _pinning_repo(tmp_path)
    _, digest = _commit_rules(repo, "rules: [a]\n")
    moved, _ = _commit_rules(repo, "rules: [a, b]\n")
    log = _write_log(repo, f"the run pins rules_sha256 {digest[:8]}, byte-identical to "
                           f"rules/rules.yaml at {moved[:8]}.")
    result = _log_check(log)
    assert result.returncode == 1
    assert f"pins rules/rules.yaml at {moved[:8]}" in result.stdout
    assert f"but the note quotes {digest[:8]}" in result.stdout


def test_log_check_fails_a_pin_no_commit_bears(tmp_path):
    repo = _pinning_repo(tmp_path)
    _, digest = _commit_rules(repo, "rules: [a]\n")
    log = _write_log(repo, f"byte-identical to rules/rules.yaml at 0badc0de ({digest[:8]}).")
    result = _log_check(log)
    assert result.returncode == 1
    assert "not reachable in this repo" in result.stdout


def _coverage_tree(tmp_path, run_ids, audited=(), skipped=None):
    reports = tmp_path / "reports"
    reports.mkdir()
    for rid in run_ids:
        (reports / f"{rid}.json").write_text(json.dumps({"run_id": rid}), encoding="utf-8")
    log = tmp_path / "audit_log.json"
    log.write_text(json.dumps([{"run_id": r} for r in audited]), encoding="utf-8")
    marker = tmp_path / "skipped.json"
    if skipped is not None:
        marker.write_text(json.dumps(skipped), encoding="utf-8")
    return reports, log, marker


def _cli(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True, cwd=deployment.ROOT)


def _coverage(reports, log, marker):
    return _cli("coverage", "--reports", reports, "--audit-log", log, "--skipped", marker)


def test_coverage_fails_on_a_run_with_no_entry(tmp_path):
    result = _coverage(*_coverage_tree(tmp_path, ["r1"]))
    assert result.returncode == 1
    assert "UNAUDITED: r1" in result.stdout


def test_a_skip_marker_clears_the_gate(tmp_path):
    tree = _coverage_tree(tmp_path, ["r1"],
                          skipped=[{"run_id": "r1", "date": "2026-08-27", "reason": "MAX=1 smoke"}])
    result = _coverage(*tree)
    assert result.returncode == 0
    assert "SKIPPED: r1 — MAX=1 smoke" in result.stdout


def test_the_gate_is_not_cleared_by_a_reasonless_marker(tmp_path):
    tree = _coverage_tree(tmp_path, ["r1"], skipped=[{"run_id": "r1", "date": "2026-08-27"}])
    result = _coverage(*tree)
    assert result.returncode == 1
    assert "UNAUDITED: r1" in result.stdout
    assert "SKIPPED" not in result.stdout


def test_skip_writes_the_marker_the_gate_then_accepts(tmp_path):
    reports, log, marker = _coverage_tree(tmp_path, ["r1"])
    assert _cli("skip", "--run", "r1", "--reason", "MAX=1 smoke",
                "--reports", reports, "--audit-log", log, "--skipped", marker).returncode == 0
    assert json.loads(marker.read_text(encoding="utf-8"))[0]["reason"] == "MAX=1 smoke"
    assert _coverage(reports, log, marker).returncode == 0


def test_skip_refuses_a_run_no_report_bears(tmp_path):
    reports, log, marker = _coverage_tree(tmp_path, ["r1"])
    result = _cli("skip", "--run", "typo", "--reason", "smoke",
                  "--reports", reports, "--audit-log", log, "--skipped", marker)
    assert result.returncode == 1
    assert "nothing to skip" in result.stderr


def test_skip_refuses_a_run_that_was_audited(tmp_path):
    reports, log, marker = _coverage_tree(tmp_path, ["r1"], audited=["r1"])
    result = _cli("skip", "--run", "r1", "--reason", "smoke",
                  "--reports", reports, "--audit-log", log, "--skipped", marker)
    assert result.returncode == 1
    assert "already audited" in result.stderr


def test_skip_refuses_a_blank_reason(tmp_path):
    reports, log, marker = _coverage_tree(tmp_path, ["r1"])
    result = _cli("skip", "--run", "r1", "--reason", "   ",
                  "--reports", reports, "--audit-log", log, "--skipped", marker)
    assert result.returncode == 1
    assert "not a decision" in result.stderr
    assert not marker.exists()


def _cost_tree(tmp_path, logs, run_id="r1", emails=2, llm=1, cost=None):
    """A report plus the Studio run logs that name it, keyed by file name."""
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    (reports / f"{run_id}.json").write_text(json.dumps(
        {"run_id": run_id, "totals": {"emails": emails, "llm": llm}, "cost": cost or {}}),
        encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    for name, events in logs.items():
        (runs / name).write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return reports, runs


def _billed(run_id, write=1_000_000, out=1_000_000):
    counts = {"prompt_tokens": 0, "cache_creation_tokens": write, "cached_input_tokens": 0,
              "completion_tokens": out, "total_tokens": write + out}
    return [
        {"event": "stage_start", "stage": "classify", "run_id": run_id},
        {"event": "stage_complete", "stage": "classify", "depth": 0,
         "tokens": {**counts, "by_model": {"claude-opus-4-8": dict(counts)}}},
        {"event": "pipeline_complete", "total_tokens": write + out},
    ]


def _spend(model="claude-opus-4-8", calls=1, **counts):
    """One leg, in the shape a run's own artifact stores it (STU-1012)."""
    return {"llm_calls": calls, "by_model": {model: counts}}


def _cost(reports, runs, drafts=None):
    # `--drafts-log` always: left at its default the command would read the real
    # `logs/drafts/stages.jsonl` of whatever checkout the suite runs in.
    return _cli("cost", "--reports", reports, "--runs-dir", runs,
                "--drafts-log", drafts or (reports.parent / "absent.jsonl"))


def test_cost_prices_a_run_from_the_log_that_names_it(tmp_path):
    reports, runs = _cost_tree(tmp_path, {"a.jsonl": _billed("r1")})
    result = _cost(reports, runs)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    # 1M written at 1.25x $5, plus 1M out at $25
    assert payload["usd"] == 31.25
    assert payload["usd_per_email"] == 15.625
    assert payload["usd_per_escalation"] == 31.25
    assert payload["rollup_mismatch"] is None


def test_cost_exits_2_when_neither_the_run_nor_a_log_recorded_anything(tmp_path):
    # The counts then live nowhere, so the run is unknown, never $0.
    reports, runs = _cost_tree(tmp_path, {"other.jsonl": _billed("r2")})
    result = _cost(reports, runs)
    assert result.returncode == 2
    assert "never as zero" in result.stderr
    assert not result.stdout


def test_cost_refuses_to_pick_between_two_logs_naming_the_run(tmp_path):
    reports, runs = _cost_tree(tmp_path, {"a.jsonl": _billed("r1"), "b.jsonl": _billed("r1")})
    result = _cost(reports, runs)
    assert result.returncode == 1
    assert "--studio-log" in result.stderr


def test_cost_prices_a_run_whose_studio_log_is_gone_off_its_own_report(tmp_path):
    # The STU-1012 case: the worktree took the log, and the number survives anyway.
    reports, runs = _cost_tree(
        tmp_path, {}, cost={"classify": _spend(completion_tokens=1_000_000)})
    result = _cost(reports, runs)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["legs"] == ["classify"]
    assert payload["usd"] == 25.0
    assert "studio_log" not in payload


def test_cost_sums_the_drafting_leg_onto_the_classification_one(tmp_path):
    # Where a mailbox-full run's money actually goes: the report holds one leg, the
    # drafts stage log the other, and neither claims the other's spend.
    reports, runs = _cost_tree(
        tmp_path, {}, cost={"classify": _spend(cache_creation_tokens=1_000_000)})
    drafts = tmp_path / "stages.jsonl"
    drafts.write_text(json.dumps({
        "run_id": "r1", "status": "completed",
        "tokens": _spend(completion_tokens=1_000_000),
    }) + "\n", encoding="utf-8")
    payload = json.loads(_cost(reports, runs, drafts).stdout)
    assert payload["legs"] == ["classify", "drafts"]
    assert payload["by_stage"]["classify"]["usd"] == 6.25
    assert payload["by_stage"]["drafts"]["usd"] == 25.0
    assert payload["usd"] == 31.25


def test_cost_sums_a_drafting_leg_that_ran_twice_under_one_run_id(tmp_path):
    # A resume keeps the run id, so the run's drafting bill is both executions
    # (STU-1111). Taking the last published the resume's alone.
    reports, runs = _cost_tree(tmp_path, {})
    drafts = tmp_path / "stages.jsonl"
    drafts.write_text("".join(json.dumps(row) + "\n" for row in [
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 14,
         "tokens": _spend(calls=40, completion_tokens=1_000_000)},
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 3,
         "tokens": _spend(calls=20, completion_tokens=1_000_000)},
    ]), encoding="utf-8")
    payload = json.loads(_cost(reports, runs, drafts).stdout)
    assert payload["by_stage"]["drafts"]["usd"] == 50.0
    assert payload["by_stage"]["drafts"]["calls"] == 60
    assert payload["drafted_conversations"] == 17
    assert "unrecorded_drafting_executions" not in payload


def test_cost_publishes_no_per_conversation_figure_when_an_execution_was_killed(tmp_path):
    # `fb7889ca`: killed mid-fan-out, resumed under its own id. The killed pass's
    # spend and drafts are recorded nowhere, so the sum is a floor and the run has no
    # denominator — not the resume's four conversations wearing the run's name.
    reports, runs = _cost_tree(tmp_path, {})
    drafts = tmp_path / "stages.jsonl"
    drafts.write_text("".join(json.dumps(row) + "\n" for row in [
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 4,
         "tokens": _spend(calls=20, completion_tokens=1_000_000)},
    ]), encoding="utf-8")
    payload = json.loads(_cost(reports, runs, drafts).stdout)
    assert payload["unrecorded_drafting_executions"] == 1
    assert payload["drafted_conversations"] is None
    assert payload["usd_per_drafted_conversation"] is None
    assert "floors" in payload["schema_note"]


def test_a_run_that_recorded_its_own_cost_never_reads_the_studio_log(tmp_path):
    # Both exist and disagree; the run's own artifact is the one that counted each
    # call once, and the log's leaf rule is only the fallback.
    reports, runs = _cost_tree(
        tmp_path, {"a.jsonl": _billed("r1")},
        cost={"classify": _spend(completion_tokens=1_000_000)})
    payload = json.loads(_cost(reports, runs).stdout)
    assert payload["usd"] == 25.0
    assert "studio_log" not in payload


def test_a_named_studio_log_wins_over_the_run_s_own_legs(tmp_path):
    # Naming one is how an operator picks between the several that can name a run;
    # reading something else instead would answer a question nobody asked.
    reports, runs = _cost_tree(
        tmp_path, {"a.jsonl": _billed("r1")},
        cost={"classify": _spend(completion_tokens=1_000_000)})
    result = _cli("cost", "--reports", reports, "--runs-dir", runs,
                  "--drafts-log", tmp_path / "absent.jsonl",
                  "--studio-log", runs / "a.jsonl")
    payload = json.loads(result.stdout)
    assert payload["studio_log"].endswith("a.jsonl")
    assert payload["usd"] == 31.25


def test_an_unpriced_call_reaches_the_audit_as_a_floor(tmp_path):
    # The number is still worth having; what it must not do is look like the bill.
    reports, runs = _cost_tree(
        tmp_path, {},
        cost={"classify": {"llm_calls": 5, "unpriced_calls": 4,
                           "by_model": {"claude-opus-4-8": {"completion_tokens": 1_000_000}}}})
    payload = json.loads(_cost(reports, runs).stdout)
    assert payload["unpriced_calls"] == 4
    assert payload["usd"] == 25.0


# --- the drafting half of a run (STU-1029) ------------------------------------------

def _journal(tmp_path, rows):
    path = tmp_path / "journal.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _drafts(journal, *args, stages=None):
    # `--drafts-log` always: left at its default the note would name the real
    # `logs/drafts/stages.jsonl` of whatever checkout the suite runs in.
    return _cli("drafts", "--draft-journal", journal, *args,
                "--drafts-log", stages or (journal.parent / "absent.jsonl"))


def test_drafts_reports_only_the_named_run(tmp_path):
    journal = _journal(tmp_path, [
        draft_row(message_id="m1"),
        draft_row(message_id="m2", updated=True),
        draft_row(message_id="m3", status="superseded"),
        draft_row(run_id="r2", message_id="m4"),
        draft_row(run_id=None, message_id="m5", status="deleted"),
    ])
    result = _drafts(journal, "--run", "r1")
    assert result.returncode == 0
    out = json.loads(result.stdout)
    assert (out["run_id"], out["created"], out["updated"]) == ("r1", 1, 1)
    assert [(p["status"], p["draft_id"]) for p in out["problems"]] == [("superseded", "d-m3")]
    assert out["unattributable"] == {"rows": 1, "draft_ids": ["d-m5"]}


def test_drafts_defaults_to_the_run_the_report_names(tmp_path):
    reports, _ = _cost_tree(tmp_path, {})
    journal = _journal(tmp_path, [draft_row(run_id="r1")])
    result = _drafts(journal, "--reports", reports)
    assert json.loads(result.stdout)["run_id"] == "r1"


def test_drafts_says_a_silent_run_is_not_the_same_as_one_that_drafted_nothing(tmp_path):
    journal = _journal(tmp_path, [draft_row(run_id="r2")])
    result = _drafts(journal, "--run", "r1", stages=tmp_path / "stages.jsonl")
    assert result.returncode == 0
    assert "stages.jsonl" in result.stderr


def _stages(tmp_path, rows):
    path = tmp_path / "stages.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_drafts_says_why_a_conversation_due_a_reply_was_not_drafted(tmp_path):
    """STU-1186: the per-draft journal cannot answer this — a conversation `drafts-pre`
    held never reaches it, so nothing owed and a reply lost leave the same absence."""
    journal = _journal(tmp_path, [draft_row(run_id="r1")])
    stages = _stages(tmp_path, [
        {"run_id": "r1", "status": "started", "needs_reply": 3},
        {"run_id": "r1", "status": "completed", "needs_reply": 3, "drafted": 1,
         "selection": {"selected": 2, "held": {"blocked": 1},
                       "unreconciled": {"clamped_by_bucket": 1}}},
    ])

    result = _drafts(journal, "--run", "r1", stages=stages)

    assert result.returncode == 0
    executions = json.loads(result.stdout)["executions"]
    assert [(e["needs_reply"], e["drafted"]) for e in executions] == [(3, 1)]
    assert executions[0]["selection"]["held"] == {"blocked": 1}
    # One held on purpose, two handed over and one of those lost: a parent is waiting,
    # and the line alone says so.
    assert executions[0]["selection"]["selected"] - executions[0]["drafted"] == 1


def test_drafts_reads_a_line_without_the_breakdown_as_unknown_not_as_zero_held(tmp_path):
    """The direction STU-912 sets: a run predating the field decided nothing it could
    say, and reporting nothing held would say the stage held nothing back — which reads
    as a reply lost when it was not."""
    journal = _journal(tmp_path, [draft_row(run_id="r1")])
    stages = _stages(tmp_path, [
        {"run_id": "r1", "status": "completed", "needs_reply": 1, "drafted": 0},
    ])

    result = _drafts(journal, "--run", "r1", stages=stages)

    assert json.loads(result.stdout)["executions"][0]["selection"] == "unrecorded"
    assert "predates the selection breakdown" in result.stderr


def test_drafts_reports_each_execution_of_a_resumed_run_on_its_own(tmp_path):
    """Per execution and never folded (STU-1111): the resume selected again out of what
    was left, so summing `needs_reply` across the two counts conversations twice."""
    journal = _journal(tmp_path, [draft_row(run_id="r1")])
    stages = _stages(tmp_path, [
        {"run_id": "r1", "status": "completed", "needs_reply": 17, "drafted": 13,
         "selection": {"selected": 17, "held": {}, "unreconciled": {}}},
        {"run_id": "r1", "status": "completed", "needs_reply": 4, "drafted": 4,
         "selection": {"selected": 4, "held": {}, "unreconciled": {}}},
    ])

    executions = json.loads(_drafts(journal, "--run", "r1", stages=stages).stdout)

    assert [e["needs_reply"] for e in executions["executions"]] == [17, 4]
    assert executions["unrecorded_drafting_executions"] == 0


def test_drafts_names_an_execution_that_recorded_nothing_at_all(tmp_path):
    """A stage killed before `drafts-post` recorded neither what it drafted nor what it
    held, so the executions listed are not all of them."""
    journal = _journal(tmp_path, [draft_row(run_id="r1")])
    stages = _stages(tmp_path, [
        {"run_id": "r1", "status": "started", "needs_reply": 5},
        {"run_id": "r1", "status": "started", "needs_reply": 5},
        {"run_id": "r1", "status": "completed", "needs_reply": 5, "drafted": 2,
         "selection": {"selected": 5, "held": {}, "unreconciled": {}}},
    ])

    result = _drafts(journal, "--run", "r1", stages=stages)

    assert json.loads(result.stdout)["unrecorded_drafting_executions"] == 1
    assert "never closed" in result.stderr


def test_drafts_refuses_a_journal_that_is_not_there(tmp_path):
    result = _drafts(tmp_path / "gone.jsonl", "--run", "r1")
    assert result.returncode == 1
    assert "no drafts journal" in result.stderr


# `call-floor` — the drafts leg's own call count against the attempts the run's Studio
# journal recorded (STU-1058).

def _drafting(run_id="r1", pending=("m1",), retries=0):
    """A journal of one drafting item, with as many discarded attempts as asked for."""
    events = [{"event": "stage_complete", "stage": "drafts-pre", "depth": 1,
               "output": {"run_id": run_id,
                          "pending": [{"message_id": m} for m in pending]}}]
    for message_id in pending:
        events.append({"event": "stage_start", "stage": "generate-draft", "depth": 3})
        events += [{"event": "stage_retry", "stage": "generate-draft", "depth": 3}
                   ] * retries
        events.append({"event": "stage_complete", "stage": "generate-draft", "depth": 3,
                       "status": "success"})
        events.append({"event": "stage_start", "stage": "draft-qa", "depth": 3})
        events.append({"event": "stage_complete", "stage": "draft-qa", "depth": 3,
                       "status": "success"})
    events.append({"event": "stage_complete", "stage": "draft-fanout", "depth": 1,
                   "output": {"results": [{"index": i, "status": "success"}
                                          for i, _ in enumerate(pending)]}})
    return events


def _classifying(run_id="r1", items=1, retries=0, resumed=False):
    """A `classify-batch` journal: the `map:` parent, and the children that ran.

    The parent stage carries the child stage's own name, which is why its rows have to
    be told apart by something other than depth (STU-1246). A resumed fan-out is the
    shape where they cannot: no child ran, so only the parent's rows survive.
    """
    events = [{"event": "stage_start", "stage": "classify", "depth": 1},
              {"event": "map_start", "map": "classify", "depth": 1,
               "total_items": items, "run_id": run_id}]
    for _ in range(0 if resumed else items):
        events.append({"event": "stage_start", "stage": "classify", "depth": 2})
        events += [{"event": "stage_retry", "stage": "classify", "depth": 2}] * retries
        events.append({"event": "stage_complete", "stage": "classify", "depth": 2,
                       "status": "success"})
    events.append({"event": "map_complete", "map": "classify", "depth": 1,
                   "status": "success", "total": items})
    events.append({"event": "stage_complete", "stage": "classify", "depth": 1,
                   "status": "success",
                   "output": {"total": items, "resumed": items if resumed else 0}})
    return events


def _leg(payload, name):
    """The one row `call-floor` reports for that leg."""
    return next(row for row in payload["legs"] if row["leg"] == name)


def _floor(tmp_path, events, calls=None, journal_rows=(), run_id="r1", extra_logs=None,
           stage_rows=None, classify=None):
    """A run's Studio journals, next to the legs it recorded — or to no leg at all."""
    logs = {"a.jsonl": events, **(extra_logs or {})}
    reports, runs = _cost_tree(tmp_path, logs, run_id=run_id,
                               cost={"classify": classify} if classify else None)
    journal = tmp_path / "journal.jsonl"
    journal.write_text("".join(json.dumps(r) + "\n" for r in journal_rows),
                       encoding="utf-8")
    stages = tmp_path / "stages.jsonl"
    if stage_rows is not None:
        stages.write_text("".join(json.dumps(r) + "\n" for r in stage_rows),
                          encoding="utf-8")
    elif calls is None:
        stages = tmp_path / "absent.jsonl"
    else:
        stages.write_text(json.dumps({
            "run_id": run_id, "status": "completed",
            "tokens": _spend(calls=calls, completion_tokens=1),
        }) + "\n", encoding="utf-8")
    result = _cli("call-floor", "--reports", reports, "--runs-dir", runs,
                  "--draft-journal", journal, "--drafts-log", stages)
    return result, json.loads(result.stdout) if result.stdout else None


def test_call_floor_finds_no_gap_when_no_attempt_was_discarded(tmp_path):
    result, payload = _floor(tmp_path, _drafting(), calls=2)
    drafts = _leg(payload, "drafts")
    assert result.returncode == 0
    assert (drafts["reported_calls"], drafts["journal_calls"]) == (2, 2)
    assert drafts["undercount"] == 0
    assert drafts["reported_from"] == str(tmp_path / "stages.jsonl")


def test_call_floor_names_the_retries_the_journal_saw(tmp_path):
    # A leg written before STU-1210 counts stage executions, so the two discarded
    # attempts are the whole of its undercount. One written since counts them itself,
    # which is why the retries are reported beside the gap and no longer subtracted
    # from it — subtracting them from a count that contains them goes negative.
    _, payload = _floor(tmp_path, _drafting(retries=2), calls=2)
    drafts = _leg(payload, "drafts")
    assert (drafts["reported_calls"], drafts["journal_calls"]) == (2, 4)
    assert (drafts["undercount"], drafts["ralph_retries"]) == (2, 2)


def test_call_floor_names_the_executions_no_row_accounted_for(tmp_path):
    # A leg claiming one call against the two stages that ran: the other stage is the
    # half nothing the run recorded accounted for, and no retry explains it.
    _, payload = _floor(tmp_path, _drafting(), calls=1)
    drafts = _leg(payload, "drafts")
    assert (drafts["reported_calls"], drafts["journal_calls"]) == (1, 2)
    assert (drafts["undercount"], drafts["ralph_retries"]) == (1, 0)


def test_call_floor_says_which_unit_the_leg_it_read_counted_in(tmp_path):
    # STU-1210: the same undercount means "retries plus unrecorded executions" on a
    # leg that counted stage executions and "unrecorded executions" on one that
    # counted attempts, and only the leg can say which it is.
    _, old_leg = _floor(tmp_path, _drafting(retries=2), calls=2)
    assert _leg(old_leg, "drafts")["reported_unit"].startswith("stage executions")

    _, new_leg = _floor(tmp_path, _drafting(retries=2), stage_rows=[
        {"run_id": "r1", "status": "completed", "drafted": 1,
         "tokens": {**_spend(calls=4, completion_tokens=1), "unrecorded_items": 0}}])
    assert _leg(new_leg, "drafts")["reported_unit"].startswith("attempts —")

    # And a leg the host's Studio was too old to give a record to says how much of
    # its own count fell back to the older unit.
    _, mixed = _floor(tmp_path, _drafting(retries=2), stage_rows=[
        {"run_id": "r1", "status": "completed", "drafted": 1,
         "tokens": {**_spend(calls=2, completion_tokens=1), "unrecorded_items": 2}}])
    assert "2 item(s)" in _leg(mixed, "drafts")["reported_unit"]


def test_call_floor_finds_no_gap_when_the_leg_counted_its_own_retries(tmp_path):
    # What a leg written since STU-1210 reports: the attempts are inside its count,
    # so a run that discarded two of them is measured exactly rather than 2 low.
    result, payload = _floor(tmp_path, _drafting(retries=2), calls=4)
    drafts = _leg(payload, "drafts")
    assert result.returncode == 0
    assert (drafts["undercount"], drafts["ralph_retries"]) == (0, 2)


def test_call_floor_refuses_to_recompute_a_leg_out_of_mailbox_writes(tmp_path):
    # STU-1092: before STU-1030 the drafting agent re-wrote the draft on every
    # revision, so the shared journal carries several `ok` rows per conversation and
    # none of them is an LLM call. A run that recorded no leg is that old, so its
    # count is unmeasured rather than recomputed into a floor above its own ceiling.
    rows = [draft_row(message_id="m1", run_id="r1", draft_id=f"d{i}") for i in range(4)]
    result, payload = _floor(tmp_path, _drafting(), journal_rows=rows)
    drafts = _leg(payload, "drafts")
    assert result.returncode == 2
    assert (drafts["reported_calls"], drafts["undercount"]) == (None, None)
    assert drafts["journal_calls"] == 2
    assert "no leg is measured" in result.stderr


def test_call_floor_exits_3_when_the_leg_reports_more_calls_than_the_journal(tmp_path):
    # The bug STU-1092 names, asserted rather than published: a floor over its own
    # ceiling is a wrong count, not a low one.
    result, payload = _floor(tmp_path, _drafting(), calls=9)
    assert result.returncode == 3
    assert _leg(payload, "drafts")["undercount"] == -7
    assert "not a floor" in result.stderr


def test_call_floor_reads_every_journal_a_resumed_run_left(tmp_path):
    # The `fb7889ca` shape (STU-1111): killed mid-fan-out, resumed under its own id,
    # one leg recorded against two journals. Either alone is half the ceiling, and the
    # resume's half is smaller than the leg's own count — which exited 3.
    result, payload = _floor(
        tmp_path, _drafting(pending=("m1", "m2", "m3")),
        extra_logs={"b.jsonl": _drafting(pending=("m4",))},
        stage_rows=[
            {"run_id": "r1", "status": "started"},
            {"run_id": "r1", "status": "started"},
            {"run_id": "r1", "status": "completed", "drafted": 1,
             "tokens": _spend(calls=2, completion_tokens=1)},
        ])
    drafts = _leg(payload, "drafts")
    assert result.returncode == 0
    assert len(payload["studio_logs"]) == 2
    assert (drafts["reported_calls"], drafts["journal_calls"]) == (2, 8)
    assert drafts["unrecorded_leg_executions"] == 1
    assert (drafts["undercount"], drafts["ralph_retries"]) == (6, 0)


def test_call_floor_measures_the_classify_leg_beside_the_drafting_one(tmp_path):
    # STU-1246: both classify orchestrations report attempts read off a child's own
    # stage record, the same claim the drafting leg makes, and nothing checked it.
    result, payload = _floor(tmp_path, _drafting() + _classifying(items=3),
                             calls=2, classify=_spend(calls=3))
    classify = _leg(payload, "classify")
    assert result.returncode == 0
    assert (classify["reported_calls"], classify["journal_calls"]) == (3, 3)
    assert classify["undercount"] == 0
    assert classify["reported_from"] == str(tmp_path / "reports" / "r1.json")
    assert _leg(payload, "drafts")["undercount"] == 0


def test_call_floor_counts_the_classify_attempts_a_run_burned(tmp_path):
    # A child that exhausted its RALPH attempts made three calls, and a leg counting
    # attempts reports three: the run's own count and the journal's agree rather than
    # showing a gap. Run 90d1e7f3 is the shape this catches — 12 children failed after
    # 36 attempts and its leg recorded none of them.
    _, payload = _floor(tmp_path, _classifying(items=1, retries=2),
                        classify=_spend(calls=3))
    classify = _leg(payload, "classify")
    assert (classify["reported_calls"], classify["journal_calls"]) == (3, 3)
    assert (classify["undercount"], classify["ralph_retries"]) == (0, 2)


def test_call_floor_does_not_count_the_classify_fanout_stage_itself(tmp_path):
    # The `map:` stage carries the child stage's own name, so a run whose items all
    # came from the resume cache leaves only the parent's row at the deepest depth.
    # Counting it reports one call nobody made against a leg that rightly reported none.
    result, payload = _floor(tmp_path, _classifying(items=2, resumed=True),
                             classify=_spend(calls=0))
    classify = _leg(payload, "classify")
    assert result.returncode == 0
    assert (classify["reported_calls"], classify["journal_calls"]) == (0, 0)
    assert classify["by_stage"]["classify"] == {"executions": 0, "retries": 0, "calls": 0}


def test_call_floor_reports_a_leg_it_could_not_measure_beside_one_it_could(tmp_path):
    # The cost of exiting on what was measured: the unmeasured leg still says so, in
    # the payload and on stderr, so a 0 is never read as a gap of zero for it.
    result, payload = _floor(tmp_path, _classifying(items=2), classify=_spend(calls=2))
    drafts = _leg(payload, "drafts")
    assert result.returncode == 0
    assert (drafts["reported_calls"], drafts["reported_unit"]) == (None, None)
    assert "drafts: unmeasured" in result.stderr
    assert _leg(payload, "classify")["undercount"] == 0


def test_call_floor_exits_3_when_the_classify_leg_reports_more_than_the_journal(tmp_path):
    # Same meaning as for drafting, and it has to hold per leg: a floor over its own
    # ceiling is a wrong count wherever it is read.
    result, payload = _floor(tmp_path, _classifying(items=1), classify=_spend(calls=5))
    assert result.returncode == 3
    assert _leg(payload, "classify")["undercount"] == -4
    assert "classify: reported_calls 5" in result.stderr


def test_call_floor_does_not_measure_a_push_classify_leg_against_its_drafting_journal(
        tmp_path):
    # STU-2059, run 03ddd0e1 on apollon: each push escalation is its own `studio run
    # classify`, whose journal names no run of ours, so the only journal found is the
    # drafting one. It holds no classify stage, and reading that as 0 calls exited 3.
    result, payload = _floor(tmp_path, _drafting(), calls=2, classify=_spend(calls=1))
    classify = _leg(payload, "classify")
    assert result.returncode == 0
    assert (classify["reported_calls"], classify["journal_calls"]) == (1, None)
    assert classify["undercount"] is None
    assert "classify: unmeasured" in result.stderr
    assert _leg(payload, "drafts")["undercount"] == 0


def test_call_floor_exits_2_when_no_journal_survives_the_run(tmp_path):
    # A floor with nothing to measure it against is not a floor known to be exact.
    reports, runs = _cost_tree(tmp_path, {"other.jsonl": _billed("r2")})
    result = _cli("call-floor", "--reports", reports, "--runs-dir", runs,
                  "--drafts-log", tmp_path / "absent.jsonl")
    assert result.returncode == 2
    assert "never as exact" in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("command", sorted(audit_run._REPORT_AWARE))
def test_run_is_refused_by_a_subcommand_that_reads_a_report(command, tmp_path):
    reports, _, _ = _coverage_tree(tmp_path, ["r1"])
    result = _cli(command, "--run", "r1", "--reports", reports)
    assert result.returncode == 1
    assert f"--report {reports}/r1.json" in result.stderr


@pytest.mark.parametrize("command", ["coverage", "log-check"])
def test_run_is_refused_without_a_report_hint_where_there_is_no_report(command, tmp_path):
    result = _cli(command, "--run", "r1", "--reports", tmp_path)
    assert result.returncode == 1
    assert "does not read --run" in result.stderr
    assert "--report" not in result.stderr


def test_the_two_subcommands_that_read_run_still_take_it(tmp_path):
    reports, log, marker = _coverage_tree(tmp_path, ["r1"])
    journal = tmp_path / "journal.jsonl"
    journal.write_text("", encoding="utf-8")
    assert _cli("drafts", "--run", "r1", "--draft-journal", journal).returncode == 0
    assert _cli("skip", "--run", "r1", "--reason", "smoke", "--reports", reports,
                "--audit-log", log, "--skipped", marker).returncode == 0


def _push_report(rid="r1"):
    return {"run_id": rid, "totals": {"emails": 0, "deterministic": 0, "llm": 0,
                                      "errors": 0, "llm_calls": 0},
            "ratio_deterministic": 0.0, "by_bucket": {}, "verdicts": [],
            "provenance": {"input_path": LIVE_INPUT, "provider": "anthropic",
                           "git_commit": "abc"}}


def _filing_tree(tmp_path, reports):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    for report in reports:
        (reports_dir / f"{report['run_id']}.json").write_text(
            json.dumps(report), encoding="utf-8")
    log = tmp_path / "audit_log.json"
    log.write_text("[]", encoding="utf-8")
    marker = tmp_path / "skipped.json"
    marker.write_text("[]", encoding="utf-8")
    return reports_dir, log, marker


def _file(reports, log, marker):
    return _cli("file", "--reports", reports, "--audit-log", log, "--skipped", marker,
                "--log", reports.parent / "absent.jsonl")


def test_file_then_coverage_passes_with_nobody_having_written_an_entry(tmp_path):
    """STU-1084's verification, in miniature: the gate clears without a human."""
    reports, log, marker = _filing_tree(tmp_path, [_push_report()])
    assert _coverage(reports, log, marker).returncode == 1
    filed = _file(reports, log, marker)
    assert filed.returncode == 0
    assert "FILED: r1" in filed.stdout
    assert _coverage(reports, log, marker).returncode == 0


def test_coverage_names_a_filed_run_as_unreviewed(tmp_path):
    """The gate stops holding, so the count is all that says a reading is owed."""
    reports, log, marker = _filing_tree(tmp_path, [_push_report()])
    _file(reports, log, marker)
    result = _coverage(reports, log, marker)
    assert "UNREVIEWED: r1" in result.stdout
    assert "1 filed run(s) carry no verdict" in result.stdout


def test_a_reviewed_entry_stops_being_counted(tmp_path):
    reports, log, marker = _filing_tree(tmp_path, [_push_report()])
    _file(reports, log, marker)
    entries = json.loads(log.read_text(encoding="utf-8"))
    entries[0]["verdict"] = "clean — 0 messages, nothing to judge"
    log.write_text(json.dumps(entries), encoding="utf-8")
    result = _coverage(reports, log, marker)
    assert result.returncode == 0
    assert "UNREVIEWED" not in result.stdout


def test_a_filed_log_still_passes_log_check(tmp_path):
    reports, log, marker = _filing_tree(tmp_path, [_push_report()])
    _file(reports, log, marker)
    assert _log_check(log).returncode == 0


# --- sequence (STU-1112) -------------------------------------------------------------


def _studio_journal(tmp_path, *events):
    path = tmp_path / "run.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    return path


def _drafting_events():
    return [
        {"event": "stage_complete", "stage": "drafts-pre", "status": "success",
         "output": {"pending": [{"thread_id": "TA"}]}},
        {"event": "stage_context", "stage": "generate-draft", "run_id": "A"},
        {"event": "stage_complete", "stage": "generate-draft", "status": "success",
         "ts": "T1", "output": {"thread_id": "TA"}},
        {"event": "stage_context", "stage": "draft-qa", "run_id": "A"},
        {"event": "stage_complete", "stage": "draft-qa", "status": "success",
         "ts": "T2", "output": {"verdict": "approved"}},
        {"event": "pipeline_complete", "run_id": "A"},
        {"event": "map_item_complete", "map": "drafts", "index": 0, "run_id": "A"},
    ]


def test_sequence_prints_one_ordered_block_per_conversation(tmp_path):
    result = _cli("sequence", "--studio-log", _studio_journal(tmp_path, *_drafting_events()))
    assert result.returncode == 0
    assert "1 conversation(s), 2 attributed stage execution(s)" in result.stdout
    assert "TA" in result.stdout
    assert "pass 1  T1  generate-draft success | draft-qa approved" in result.stdout


def test_sequence_refuses_a_journal_it_cannot_read(tmp_path):
    result = _cli("sequence", "--studio-log", tmp_path / "gone.jsonl")
    assert result.returncode == 2
    assert "cannot read" in result.stderr


def test_sequence_reads_one_journal_not_several(tmp_path):
    journal = _studio_journal(tmp_path, *_drafting_events())
    result = _cli("sequence", "--studio-log", journal, journal)
    assert result.returncode != 0
    assert "pass a single --studio-log" in result.stderr


def _mailbox_report(named):
    return {"run_id": "r1", "provenance": {"mailbox": named}}


def test_a_report_from_another_mailbox_is_refused(monkeypatch):
    """STU-1183: a foreign id answers with nothing, which reads as deleted mail."""
    monkeypatch.chdir(deployment.ROOT)
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")
    with pytest.raises(SystemExit) as exc:
        audit_run._assert_named_mailbox(_mailbox_report("staging@club.example"))
    assert "staging@club.example" in str(exc.value)
    assert "inbox@club.example" in str(exc.value)


def test_the_named_mailbox_is_returned_when_the_two_agree(monkeypatch):
    monkeypatch.setenv("OUTLOOK_MAILBOX", "Inbox@Club.Example")
    named = audit_run._assert_named_mailbox(_mailbox_report("inbox@club.example"))
    assert named == "inbox@club.example"


def test_an_unnamed_side_founds_no_refusal(monkeypatch):
    """A report written before the field, and a host that addresses `/me`."""
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")
    assert audit_run._assert_named_mailbox({"run_id": "r1", "provenance": {}}) is None
    monkeypatch.delenv("OUTLOOK_MAILBOX")
    named = audit_run._assert_named_mailbox(_mailbox_report("staging@club.example"))
    assert named == "staging@club.example"


def test_an_unreadable_policy_says_so_rather_than_aborting(monkeypatch, tmp_path, capsys):
    """Most subcommands never touch a mailbox; a guard that cannot read must not kill them."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")
    assert audit_run._assert_named_mailbox(_mailbox_report("staging@club.example")) == \
        "staging@club.example"
    assert "not cross-checked" in capsys.readouterr().err


class _Mailbox:
    """Graph stand-in for `reviewed`: one sent reply, no draft left in the folder."""

    def __init__(self):
        self.asked = []

    def sent_replies(self, written_at):
        self.asked.append(dict(written_at))
        return {"T1": {"body": "Bonjour, oui c'est bien samedi."}}

    def find_draft(self, thread_id):
        return None


def test_reviewed_reports_an_unmatched_draft_as_a_row(monkeypatch, tmp_path, capsys):
    """A write-draft output whose thread no drafts-pre item carries (STU-2061): the
    drafter mis-copied the id on one attempt. The run is still read, and the draft is
    a row rather than an OrphanDraft traceback."""
    from email_classifier import providers

    def complete(stage, output):
        return json.dumps({"event": "stage_complete", "stage": stage, "output": output})

    journal = tmp_path / "drafts.jsonl"
    journal.write_text("\n".join([
        json.dumps({"event": "pipeline_start", "run_id": "run-1"}),
        complete("drafts-pre", {"pending": [{"thread_id": "T1"}]}),
        complete("write-draft", {"thread_id": "T1", "draft_body": "Bonjour, oui."}),
        complete("write-draft", {"thread_id": "TX", "draft_body": "Bonjour, oui samedi."}),
        complete("write-draft", {"thread_id": "T1", "draft_body": "Bonjour, oui c'est samedi."}),
    ]), encoding="utf-8")
    mailbox = _Mailbox()
    monkeypatch.setattr(providers, "get_thread_reader", lambda provider: mailbox)
    monkeypatch.setattr(providers, "get_drafter", lambda provider: mailbox)

    code = _run(monkeypatch, tmp_path, "reviewed", _one_verdict_report(),
                extra=["--studio-log", str(journal),
                       "--draft-journal", str(_draft_journal(tmp_path, "T1"))])

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["unmatched_items"] == 1
    assert out["conversations"] == 1
    assert [(r["thread_id"], r["outcome"]) for r in out["rows"]][-1] == \
        ("TX", audit_run.UNMATCHED_ITEM)
    assert mailbox.asked == [{"T1": "2026-10-16T12:00:00+00:00"}]


def _draft_journal(tmp_path, *threads, run_id="run-1"):
    path = tmp_path / "draft-journal.jsonl"
    path.write_text("\n".join(json.dumps({
        "timestamp": "2026-10-16T12:00:00+00:00", "run_id": run_id, "thread_id": t,
        "message_id": f"m-{t}", "status": "ok"}) for t in threads), encoding="utf-8")
    return path


def _two_draft_journal(tmp_path):
    def complete(stage, output):
        return json.dumps({"event": "stage_complete", "stage": stage, "output": output})

    journal = tmp_path / "drafts.jsonl"
    journal.write_text("\n".join([
        json.dumps({"event": "pipeline_start", "run_id": "run-1"}),
        complete("drafts-pre", {"pending": [{"thread_id": "T1"}, {"thread_id": "T2"}]}),
        complete("write-draft", {"thread_id": "T1", "draft_body": "Bonjour, oui."}),
        complete("write-draft", {"thread_id": "T2", "draft_body": "Bonjour, non."}),
    ]), encoding="utf-8")
    return journal


def test_reviewed_pairs_only_drafts_the_journal_saw_written(monkeypatch, tmp_path, capsys):
    """STU-1998: a draft is paired with the first send after its write time. One the
    drafts journal never recorded has no instant to pair from, so it is a row, never a
    score against whatever the conversation sent last."""
    from email_classifier import providers

    mailbox = _Mailbox()
    monkeypatch.setattr(providers, "get_thread_reader", lambda provider: mailbox)
    monkeypatch.setattr(providers, "get_drafter", lambda provider: mailbox)

    code = _run(monkeypatch, tmp_path, "reviewed", _one_verdict_report(),
                extra=["--studio-log", str(_two_draft_journal(tmp_path)),
                       "--draft-journal", str(_draft_journal(tmp_path, "T1"))])

    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert mailbox.asked == [{"T1": "2026-10-16T12:00:00+00:00"}]
    assert out["no_write_time"] == 1
    assert ("T2", audit_run.NO_WRITE_TIME) in [(r["thread_id"], r["outcome"])
                                               for r in out["rows"]]


def test_reviewed_reads_facts_only_when_asked(monkeypatch, tmp_path, capsys):
    """`reviewed` stays offline by default; `--facts` adds the reading beside the
    lexical bands, which stay as they were."""
    from email_classifier import fact_read, providers

    mailbox = _Mailbox()
    monkeypatch.setattr(providers, "get_thread_reader", lambda provider: mailbox)
    monkeypatch.setattr(providers, "get_drafter", lambda provider: mailbox)
    asked = []

    def read(thread_id, draft_body, sent_body, cwd, provider=None):
        asked.append(thread_id)
        return {"verdict": "contradicted", "contradicted": ["can still register"]}

    monkeypatch.setattr(fact_read, "read", read)
    extra = ["--studio-log", str(_two_draft_journal(tmp_path)),
             "--draft-journal", str(_draft_journal(tmp_path, "T1", "T2"))]

    _run(monkeypatch, tmp_path, "reviewed", _one_verdict_report(), extra=extra)
    offline = json.loads(capsys.readouterr().out)
    assert asked == [] and "by_facts" not in offline

    _run(monkeypatch, tmp_path, "reviewed", _one_verdict_report(),
         extra=[*extra, "--facts"])
    out = json.loads(capsys.readouterr().out)
    # T2 has no sent reply, so nothing to read it against.
    assert asked == ["T1"]
    assert out["by_facts"]["contradicted"] == 1
    assert out["by_outcome"] == offline["by_outcome"]
    assert [r.get("facts") for r in out["rows"] if r["thread_id"] == "T1"] == [
        {"verdict": "contradicted", "contradicted": ["can still register"]}]


def test_reviewed_reads_facts_on_the_hosts_provider(monkeypatch, tmp_path, capsys):
    """STU-2088: a host on `claude-code` with no API key failed every read under the
    reader's `anthropic` default. The host's provider wins, and `--provider` over it."""
    from email_classifier import fact_read, providers

    mailbox = _Mailbox()
    monkeypatch.setattr(providers, "get_thread_reader", lambda provider: mailbox)
    monkeypatch.setattr(providers, "get_drafter", lambda provider: mailbox)
    used = []

    def read(thread_id, draft_body, sent_body, cwd, provider=None):
        used.append(provider)
        return {"verdict": "kept", "contradicted": []}

    monkeypatch.setattr(fact_read, "read", read)
    monkeypatch.setenv("STUDIO_PROVIDER", "claude-code")
    extra = ["--studio-log", str(_two_draft_journal(tmp_path)),
             "--draft-journal", str(_draft_journal(tmp_path, "T1", "T2")), "--facts"]

    _run(monkeypatch, tmp_path, "reviewed", _one_verdict_report(), extra=extra)
    _run(monkeypatch, tmp_path, "reviewed", _one_verdict_report(),
         extra=[*extra, "--provider", "mock"])
    capsys.readouterr()
    assert used == ["claude-code", "mock"]


def test_reviewed_refuses_without_the_drafts_journal(monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, tmp_path, "reviewed", _one_verdict_report(),
             extra=["--studio-log", str(_two_draft_journal(tmp_path)),
                    "--draft-journal", str(tmp_path / "absent.jsonl")])
    assert "no drafts journal" in str(exc.value)
