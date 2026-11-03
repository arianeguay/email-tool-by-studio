"""What a prune may and may not take (STU-932).

The whole suite plans over a synthetic tree: the protections are claims about
`audits/audit_log.json`, `audits/skipped.json` and `logs/applied/`, and each test is
one artifact that has to survive a policy that would otherwise reach it.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from email_classifier.retention import (
    APPLIED,
    DAY_SECONDS,
    REPORTS,
    SHELVES,
    Shelf,
    apply_plan,
    plan,
    run_id_of,
)
from tests.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "prune_artifacts.py"

# Real time, because the CLI tests age files for a subprocess that reads its own
# clock; ages are in days, so the drift between the two calls is noise.
NOW = time.time()
AUDITED = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SKIPPED = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
UNCOVERED = "cccccccc-3333-4333-8333-cccccccccccc"
OTHER = "dddddddd-4444-4444-8444-dddddddddddd"
STRAY = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"


def write(path: Path, *, days_old: float, body: str = "{}") -> Path:
    """One artifact, aged by its mtime — the only thing a retention age reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    stamp = NOW - days_old * DAY_SECONDS
    import os

    os.utime(path, (stamp, stamp))
    return path


def tree(root: Path, *, days_old: float = 99) -> None:
    """A run's three artifacts, for each of the four run ids, all past every age.

    The skipped run is the oldest of the four on purpose: it is the one the shipped
    `keep_last` floor has to let go, and a floor of three over four runs would
    otherwise hold whichever one sorted youngest.
    """
    for offset, run_id in enumerate((AUDITED, UNCOVERED, OTHER, SKIPPED)):
        age = days_old + offset
        write(root / REPORTS / f"{run_id}.json", days_old=age)
        write(root / "dumps" / f"{run_id}.json", days_old=age)
        write(root / APPLIED / f"{run_id}.jsonl", days_old=age)


def audit_log(run_id: str = AUDITED, dump: str | None = None) -> list[dict]:
    return [{"run_id": run_id, "dump": dump or f"dumps/{run_id}.json"}]


def skipped(run_id: str = SKIPPED, reason: str = "smoke run") -> list[dict]:
    return [{"run_id": run_id, "date": "2026-08-28", "reason": reason}]


def decide(root: Path, *, entries=None, marks=None, shelves=SHELVES, now=NOW) -> dict:
    """The plan as `relative path -> (remove, reason)`, which is what tests assert on."""
    decisions = plan(root, now=now,
                     audit_entries=audit_log() if entries is None else entries,
                     skipped_entries=skipped() if marks is None else marks,
                     shelves=shelves)
    return {str(d.path.relative_to(root)): (d.remove, d.reason) for d in decisions}


# --- what no age reaches ---------------------------------------------------------


def test_an_audited_run_keeps_every_artifact_it_left(tmp_path):
    tree(tmp_path)
    verdicts = decide(tmp_path)
    for shelf, name in ((REPORTS, f"{AUDITED}.json"), ("dumps", f"{AUDITED}.json"),
                        (APPLIED, f"{AUDITED}.jsonl")):
        assert verdicts[f"{shelf}/{name}"] == (False, "audited")


def test_the_dump_an_entry_names_survives_without_a_run_of_its_own(tmp_path):
    """`dump` is a path, and the run that consumed it is rarely the one that wrote it."""
    tree(tmp_path)
    write(tmp_path / "dumps" / f"{STRAY}.json", days_old=400)
    verdicts = decide(tmp_path, entries=audit_log(dump=f"dumps/{STRAY}.json"))
    assert verdicts[f"dumps/{STRAY}.json"] == (False, "audit-input")


def test_a_dump_field_that_is_a_sentence_protects_nothing(tmp_path):
    """Pre-STU-819 entries record prose, and a live run records no artifact at all."""
    tree(tmp_path)
    verdicts = decide(tmp_path, entries=[
        {"run_id": AUDITED, "dump": "unknown (not recorded in report)"},
        {"run_id": OTHER, "dump": "webhook:push-notification"},
    ], marks=skipped())
    assert verdicts[f"dumps/{SKIPPED}.json"] == (True, "expired")


def test_an_unaudited_report_is_what_the_gate_is_holding_on(tmp_path):
    """Pruning it would release `audit-gate` on a run nobody looked at (STU-900)."""
    tree(tmp_path)
    verdicts = decide(tmp_path)
    assert verdicts[f"reports/{UNCOVERED}.json"] == (False, "unaudited")
    assert verdicts[f"dumps/{UNCOVERED}.json"] == (False, "unaudited")


def test_a_skip_marker_without_a_reason_excuses_nothing(tmp_path):
    """The STU-900 rule, unchanged: a reason-less marker is silence in a second file."""
    tree(tmp_path)
    verdicts = decide(tmp_path, marks=[{"run_id": SKIPPED, "reason": "   "}])
    assert verdicts[f"reports/{SKIPPED}.json"] == (False, "unaudited")


def test_an_apply_journal_holds_its_report_back(tmp_path):
    """Half an undo — the mailbox reverted, the drafts left behind (STU-854)."""
    tree(tmp_path)
    shelves = (Shelf(REPORTS, max_age_days=1), Shelf(APPLIED, max_age_days=365))
    verdicts = decide(tmp_path, shelves=shelves)
    assert verdicts[f"logs/applied/{SKIPPED}.jsonl"] == (False, "fresh")
    assert verdicts[f"reports/{SKIPPED}.json"] == (False, "undoable")


def test_the_report_goes_once_its_journal_goes_in_the_same_pass(tmp_path):
    tree(tmp_path)
    shelves = (Shelf(REPORTS, max_age_days=30), Shelf(APPLIED, max_age_days=30))
    verdicts = decide(tmp_path, shelves=shelves)
    assert verdicts[f"logs/applied/{SKIPPED}.jsonl"] == (True, "expired")
    assert verdicts[f"reports/{SKIPPED}.json"] == (True, "expired")


def test_a_journal_never_holds_a_dump_back(tmp_path):
    """`undo` does not read the dump, and the dumps are the bulk on disk."""
    tree(tmp_path)
    shelves = (Shelf("dumps", max_age_days=14), Shelf(APPLIED, max_age_days=90))
    verdicts = decide(tmp_path, shelves=shelves)
    assert verdicts[f"dumps/{SKIPPED}.json"] == (True, "expired")


# --- what the ages do reach ------------------------------------------------------


def test_a_skipped_run_is_the_one_a_prune_takes(tmp_path):
    tree(tmp_path)
    verdicts = decide(tmp_path)
    for path in (f"reports/{SKIPPED}.json", f"dumps/{SKIPPED}.json",
                 f"logs/applied/{SKIPPED}.jsonl"):
        assert verdicts[path] == (True, "expired")


def test_a_skipped_run_inside_its_age_stays(tmp_path):
    tree(tmp_path, days_old=1)
    verdicts = decide(tmp_path)
    assert verdicts[f"dumps/{SKIPPED}.json"] == (False, "fresh")
    # Its report reads `undoable` rather than `fresh`: the journal is there too, and
    # the coupling is what answers first. Both keep it.
    assert verdicts[f"reports/{SKIPPED}.json"] == (False, "undoable")


def test_the_newest_few_survive_a_quiet_month(tmp_path):
    """`RUN_ID` defaults to the newest report; an empty `reports/` breaks every target."""
    for age in range(6):
        write(tmp_path / REPORTS / f"{SKIPPED[:-1]}{age}.json", days_old=90 + age)
    verdicts = decide(tmp_path, marks=[{"run_id": f"{SKIPPED[:-1]}{a}", "reason": "smoke"}
                                       for a in range(6)])
    kept = [name for name, (remove, reason) in verdicts.items() if reason == "keep-last"]
    assert len(kept) == 3
    assert sorted(kept) == sorted(f"reports/{SKIPPED[:-1]}{a}.json" for a in range(3))


def test_a_hand_named_dump_is_never_a_candidate(tmp_path):
    """`make seed` and `make deliver` write dumps by name, and a golden expires with them."""
    tree(tmp_path)
    for name in ("gmail-dump.json", "outlook-seeded.json", "outlook-review.json"):
        write(tmp_path / "dumps" / name, days_old=400)
    verdicts = decide(tmp_path)
    for name in ("gmail-dump.json", "outlook-seeded.json", "outlook-review.json"):
        assert verdicts[f"dumps/{name}"] == (False, "unrecognized")


def test_the_shared_keymap_outlives_the_run_slices(tmp_path):
    """It is the machine's, not a run's (STU-924) — and it is what keeps a token stable."""
    write(tmp_path / "logs/anonymization/keymap.json", days_old=400)
    shelves = (Shelf("logs/anonymization", max_age_days=30),)
    verdicts = decide(tmp_path, shelves=shelves)
    assert verdicts["logs/anonymization/keymap.json"] == (False, "unrecognized")


def test_a_plan_and_an_undo_journal_name_the_same_run(tmp_path):
    tree(tmp_path)
    write(tmp_path / APPLIED / f"{AUDITED}.plan.jsonl", days_old=99)
    write(tmp_path / APPLIED / f"{SKIPPED}.undo.jsonl", days_old=400)
    verdicts = decide(tmp_path)
    assert verdicts[f"logs/applied/{AUDITED}.plan.jsonl"] == (False, "audited")
    assert verdicts[f"logs/applied/{SKIPPED}.undo.jsonl"] == (True, "expired")


def test_the_studio_run_logs_expire_on_age_alone(tmp_path):
    """Prunable since a run records its own cost (STU-1012). Their names carry
    Studio's run id, not ours, so no protection can reach them."""
    stale = write(tmp_path / ".studio/runs/2026-06-17T02h00m-mailbox-full-aaaa1111.jsonl",
                  days_old=99)
    fresh = write(tmp_path / ".studio/runs/2026-08-28T02h46m-mailbox-full-bbbb2222.jsonl",
                  days_old=1)
    verdicts = decide(tmp_path)
    assert verdicts[f".studio/runs/{stale.name}"] == (True, "expired")
    assert verdicts[f".studio/runs/{fresh.name}"] == (False, "fresh")


def test_the_studio_run_index_is_not_one_of_the_logs(tmp_path):
    """`runs.db` sits in there beside them; a shelf sweeping the whole directory
    would take the index with the entries."""
    write(tmp_path / ".studio/runs/runs.db", days_old=400)
    assert decide(tmp_path)[".studio/runs/runs.db"] == (False, "unrecognized")


def test_the_web_cache_expires_on_age_alone(tmp_path):
    """Its names are a sha256 of a URL; nothing outside the cache ever refers to one."""
    stale = write(tmp_path / "dumps/web-cache" / ("a" * 64), days_old=2)
    fresh = write(tmp_path / "dumps/web-cache" / ("b" * 64), days_old=0)
    verdicts = decide(tmp_path)
    assert verdicts[f"dumps/web-cache/{stale.name}"] == (True, "expired")
    assert verdicts[f"dumps/web-cache/{fresh.name}"] == (False, "fresh")


def test_run_id_is_read_off_the_filename_or_not_at_all():
    assert run_id_of(f"{AUDITED}.json") == AUDITED
    assert run_id_of(f"{AUDITED}.plan.jsonl") == AUDITED
    assert run_id_of("gmail-dump.json") is None
    assert run_id_of("keymap.json") is None
    assert run_id_of("a" * 64) is None


# --- applying the plan -----------------------------------------------------------


def test_apply_removes_exactly_what_the_plan_marked(tmp_path):
    tree(tmp_path)
    decisions = plan(tmp_path, now=NOW, audit_entries=audit_log(),
                     skipped_entries=skipped())
    doomed = {d.path for d in decisions if d.remove}
    kept = {d.path for d in decisions if not d.remove}
    removed, freed = apply_plan(decisions)

    assert removed == len(doomed) and freed > 0
    assert not any(p.exists() for p in doomed)
    assert all(p.exists() for p in kept)


def test_a_second_pass_removes_nothing(tmp_path):
    tree(tmp_path)
    kwargs = dict(now=NOW, audit_entries=audit_log(), skipped_entries=skipped())
    apply_plan(plan(tmp_path, **kwargs))
    assert apply_plan(plan(tmp_path, **kwargs)) == (0, 0)


def test_a_file_that_vanished_under_the_pass_is_not_an_error(tmp_path):
    """Two cron ticks overlapping reach the same end state this one was asking for."""
    tree(tmp_path)
    decisions = plan(tmp_path, now=NOW, audit_entries=audit_log(),
                     skipped_entries=skipped())
    next(d for d in decisions if d.remove).path.unlink()
    removed, _ = apply_plan(decisions)
    assert removed == len([d for d in decisions if d.remove]) - 1


def test_missing_directories_are_not_an_error(tmp_path):
    assert plan(tmp_path, now=NOW, audit_entries=[], skipped_entries=[]) == []


# --- the command -----------------------------------------------------------------


def run_cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True, cwd=REPO_ROOT)


def cli_tree(tmp_path: Path) -> Path:
    tree(tmp_path)
    audits = tmp_path / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    (audits / "audit_log.json").write_text(json.dumps(audit_log()), encoding="utf-8")
    (audits / "skipped.json").write_text(json.dumps(skipped()), encoding="utf-8")
    return tmp_path


def test_the_command_removes_nothing_without_apply(tmp_path):
    root = cli_tree(tmp_path)
    result = run_cli(root)
    assert result.returncode == 0, result.stderr
    assert "plan only" in result.stdout
    assert (root / REPORTS / f"{SKIPPED}.json").exists()


def test_the_command_removes_with_apply(tmp_path):
    root = cli_tree(tmp_path)
    result = run_cli(root, "--apply")
    assert result.returncode == 0, result.stderr
    assert not (root / REPORTS / f"{SKIPPED}.json").exists()
    assert (root / REPORTS / f"{AUDITED}.json").exists()
    assert (root / REPORTS / f"{UNCOVERED}.json").exists()


def test_the_command_refuses_without_an_audit_log(tmp_path):
    """With no log every run reads as unprotected, and the audited ones go first."""
    tree(tmp_path)
    result = run_cli(tmp_path, "--apply")
    assert result.returncode == 2
    assert "refusing to prune" in result.stderr
    assert (tmp_path / REPORTS / f"{SKIPPED}.json").exists()


def test_quiet_says_nothing_when_there_is_nothing_to_do(tmp_path):
    root = cli_tree(tmp_path)
    run_cli(root, "--apply")
    assert run_cli(root, "--apply", "--quiet").stdout == ""
