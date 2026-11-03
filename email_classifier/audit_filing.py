"""File a push run's audit entry so the gate stops being a transcription queue (STU-1084).

STU-900 gave `audits/audit_log.json` a gate and nothing that fills it, so the backlog
became blocking rather than smaller: six issues (STU-841, 860, 897, 921, 1038, 1071) are
the same human reading push reports and hand-appending entries, at a rate inbound mail
sets. On a real club mailbox that does not converge.

Only a push run files itself. A `make run` is a person at a keyboard who is already
looking, and `audit-gate` holding it costs nothing; the flood is the webhook's, one
report per notification batch (STU-939). The test is the report's own
`provenance.input_path`, which the push path records as `LIVE_INPUT` (STU-859) — not the
machine the walk runs on, so the same command is safe anywhere.

The walk is over every unfiled report, not just the one that just finished: filing that
failed leaves the report on disk, so the next run picks it up, and a filing this module
never got to run for degrades to exactly the blocking gate that exists today.
"""

import datetime
import json
import os
from pathlib import Path

from email_classifier import cost
from email_classifier.core.contract import Taxonomy
from email_classifier.audit import (
    check_cache_hits,
    stage2_calls,
    check_log_consistency,
    check_report,
    filed_entry,
    metrics,
    no_frontier_log_note,
    rule_stats,
    unrecorded_drafting_note,
)
from email_classifier.orchestrate.provenance import LIVE_INPUT

# What a filed entry is allowed to leave null, and why. `check_audit_entries` demands a
# `schema_note` beside each (STU-831), and the note is read by a person deciding whether
# the null is a gap or a choice.
_STALENESS_NOTE = (
    "filed entry (STU-1084): written by the run itself off its report and frontier log, "
    "with no human reading behind it — see `verdict`. never_fires and "
    "unexercised_by_this_run are null because rule staleness is a claim about the rule "
    "set rather than this run, identical on every run that pinned it; the reviewer "
    "computes it once over the window with `audit_run.py rules`."
)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _jsonl(path: Path) -> list[dict] | None:
    """The log's lines, or None when it cannot be read whole.

    None and not the lines it managed to parse: a half-read frontier log yields a
    `policy_downgrades` count nothing supports, and the caller already knows how to
    say "this family did not run". Raising is the third option and the wrong one —
    the write is after the walk, so one bad line would file nothing at all.
    """
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return None


def _covered(audit_log: Path, skipped: Path) -> set[str]:
    """Run ids already spoken for — audited, or excused with a reason (STU-900)."""
    covered = {e.get("run_id") for e in _load(audit_log)}
    covered |= {e.get("run_id") for e in _load(skipped) if (e.get("reason") or "").strip()}
    return covered


def _entry(report: dict, frontier: list[dict] | None, frontier_log: Path,
           drafts_log: Path, date: str, taxonomy: Taxonomy) -> dict:
    """One report's entry, from the artifacts the run itself left behind.

    Deliberately narrower than `audit_run.py check`: no dump cross-check, because a push
    run consumed none, and no rule replay, because the fixture corpora answer a question
    about the rule set. What is left reads off the report, the frontier log and the
    run's own cost — all of which the run wrote.
    """
    run_id = report.get("run_id")
    # A log that exists but names this run nowhere does not cover it, and 0 downgrades
    # would be a count nobody computed. A run that wrote lines and downgraded nothing
    # is a different thing and has them: every message writes a frontier line, only a
    # downgrade carries `action_original`.
    if frontier is not None and not any(e.get("run_id") == run_id for e in frontier):
        frontier = None
    violations = check_report(report, taxonomy)
    if frontier is not None:
        violations += check_log_consistency(report, frontier)
    legs = {}
    if (classify := cost.report_leg(report)) is not None:
        legs[cost.CLASSIFY_LEG] = classify
    if (drafting := cost.drafts_leg(drafts_log, run_id)) is not None:
        legs[cost.DRAFTS_LEG] = drafting
    # Read off the log rather than off the leg: an execution killed before `drafts-post`
    # priced nothing, so a run whose executions were all killed has no leg at all and
    # would otherwise file as a run that never drafted.
    _, unrecorded = cost.drafts_executions(drafts_log, run_id)
    # A leg that structurally cannot reuse a prefix is a note, not a violation
    # (STU-1145): this walk runs unattended, so a violation nothing can repair would
    # file itself on every push burst that escalates twice.
    cache_violations, cache_notes = check_cache_hits(cost.hit_ratios(legs))
    violations += cache_violations

    # A list rather than one string: three of these can be true at once, and the
    # frontier sentence used to overwrite whatever the cache sentence had said.
    notes = [_STALENESS_NOTE]
    if cost.CLASSIFY_LEG not in legs and stage2_calls(report.get("verdicts", [])):
        # The same silence `check` answers with exit 2 (STU-1057): a run that escalated
        # and priced nothing leaves the cache check nothing to read, and `violations`
        # is an integer either way — so the entry would report a family that never ran
        # as one that passed.
        notes.append("The cache hit ratio was not read: the run escalated and "
                     "recorded no classify cost of its own — `audit_run.py cost` falls "
                     "back to its Studio log.")
    notes += [f"{n}." for n in cache_notes]
    if unrecorded:
        # The entry outlives its report by the 30-day shelf, so a reviewer holding only
        # the entry has nothing left to learn from that the drafting figures are short
        # (STU-1157). Carried from `audit.py` like the `metrics` sentence below, so the
        # entry and `audit_run.py cost` cannot drift apart on what the floor means.
        notes.append(unrecorded_drafting_note(unrecorded, run_id, drafts_log))
    if frontier is None:
        # Two things go with the log, and `violations` is the one a shape check cannot
        # see: the count is still an integer, so a run whose log ⇔ report cross-check
        # never ran reads exactly like one that passed it. `check` answers that with
        # INCOMPLETE and exit 2 (STU-912); an entry can only say so in words. The
        # `metrics` sentence covers the null `policy_downgrades` — carried rather than
        # restated, so the two cannot drift.
        notes.append("violations counts the report-internal checks only: "
                     f"{no_frontier_log_note(str(frontier_log))}")
    return filed_entry(report, metrics(report, frontier, str(frontier_log)),
                       rule_stats(report, None, None), len(violations),
                       " ".join(notes), date)


def file_unaudited(reports_dir: Path, audit_log: Path, skipped: Path,
                   frontier_log: Path, drafts_log: Path, taxonomy: Taxonomy) -> list[str]:
    """Append an entry for every push report nothing has spoken for. Returns their ids."""
    if not reports_dir.is_dir():
        return []
    covered = _covered(audit_log, skipped)
    frontier = _jsonl(frontier_log)

    entries = _load(audit_log)
    filed = []
    for path in sorted(reports_dir.glob("*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # One truncated report must not cost every other run its entry: the write
            # is after this loop, so raising here files nothing at all and reverts to
            # the blocking gate with only a log line to say why. Skipped, the bad
            # report stays unfiled — which is the gate holding on it, where a human
            # looks. That is the direction this has to fail in.
            continue
        run_id = report.get("run_id")
        # A report naming no run cannot be filed against one: the entry would key on
        # null and `log-check` would read it as drift rather than as the odd report.
        if not run_id or run_id in covered:
            continue
        if (report.get("provenance") or {}).get("input_path") != LIVE_INPUT:
            continue
        # The report's own mtime, not today: a walk that catches up on a week of runs
        # would otherwise date them all to the day someone noticed.
        written = datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()
        entries.append(_entry(report, frontier, frontier_log, drafts_log, written, taxonomy))
        filed.append(run_id)

    if filed:
        audit_log.parent.mkdir(parents=True, exist_ok=True)
        # Written whole and moved into place: the webhook worker and `make audit-file`
        # both write this file on the serving host — `pull-webhook` runs the second
        # while the first is still serving — and a reader catching a partial rewrite
        # gets a truncated array, which is the audit trail destroyed rather than
        # delayed. Two writers racing can still cost one of them its entry; that one
        # is self-healing, since its report stays uncovered and the next walk re-files
        # it. A half-written file heals nothing.
        tmp = audit_log.with_suffix(audit_log.suffix + ".tmp")
        tmp.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, audit_log)
    return filed
