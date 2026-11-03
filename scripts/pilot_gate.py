#!/usr/bin/env python3
"""Read the V1 exit condition off the window's artifacts (STU-1242).

    scripts/pilot_gate.py --days 7
    scripts/pilot_gate.py --since 2026-08-26 --until 2026-09-02 --offline

Prints the three conditions the prod-pilot milestone states, each
naming the file it was read from and each answering met / not met / not measurable. The
third is a distinct answer from the second, per the rule the audit family already follows
(STU-912): a condition nobody could measure must not read as one that came back clean.

Local, and for the reasons the whole audit family is: the reports, the probe's journal and
the anonymization records are all gitignored and live on the serving host, and the drafting
ratio joins our drafts to what was sent, which comes from Graph.

Exit codes, each its own condition the way the family numbers them:

    0  every condition met
    1  a condition was not met
    2  a condition could not be measured, and none was failed
    5  nothing to read — no report under the directories given
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KERNEL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KERNEL_ROOT))

from email_classifier import audit, draft_delta, pilot_delta, pilot_gate  # noqa: E402
from email_classifier.anonymize import load_keymap, load_sent, sent_path  # noqa: E402
from email_classifier.orchestrate.provenance import LIVE_INPUT  # noqa: E402
from email_classifier import deployment  # noqa: E402

REPO_ROOT = deployment.ROOT


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, nargs="+", default=[Path("reports")])
    parser.add_argument("--runs-dir", type=Path, nargs="+", default=[Path(".studio/runs")])
    parser.add_argument("--health-journal", type=Path,
                        default=Path("state/health-checks.jsonl"))
    parser.add_argument("--keymap", type=Path,
                        default=Path("logs/anonymization/keymap.json"))
    parser.add_argument("--days", type=float, default=pilot_gate.WEEK_DAYS,
                        help="Window length back from --until. Ignored when --since is "
                             "given.")
    parser.add_argument("--since", help="Window start, ISO date or timestamp (UTC).")
    parser.add_argument("--until", help="Window end, ISO date or timestamp (UTC). "
                                        "Defaults to now.")
    parser.add_argument("--floor", type=int, default=10,
                        help="Conversations on a carrying commit before a ratio has a "
                             "denominator (STU-1182).")
    parser.add_argument("--offline", action="store_true",
                        help="Read conditions 1 and 2 and place the drafting traffic, "
                             "without calling Graph. Answers whether there is a "
                             "denominator yet; never what it scored.")
    parser.add_argument("-o", "--out", type=Path)
    return parser.parse_args(argv)


def _moment(text: str) -> float:
    """An ISO date or datetime, read as UTC when it says nothing about a zone.

    Naive-means-UTC and not naive-means-local: every timestamp this reads is written by a
    host in UTC — the probe's `at`, the report mtimes it is compared against — and a
    boundary that silently shifted by the reader's offset would move the window under a
    number someone else published.
    """
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def window_of(args) -> pilot_gate.Window:
    until = _moment(args.until) if args.until else datetime.now(timezone.utc).timestamp()
    since = _moment(args.since) if args.since else until - timedelta(days=args.days).total_seconds()
    return pilot_gate.Window(since=since, until=until)


def _recall_readings(rounds_in_window, args) -> list[dict]:
    """One recall verdict per run in the window, or the reason there is not one.

    Only the recorded sent log, never the Studio-journal fallback `audit_run.py recall`
    accepts: that fallback attributes payloads to a run by reading logs a neighbour may
    have written, and it carries an attribution caveat for exactly that reason. A caveat a
    sweep prints once per run is a caveat nobody reads, so a run without its own record is
    reported unsettled here and settled one at a time with `audit_run.py recall`, where the
    caveat is in front of a human.
    """
    keymap = load_keymap(args.keymap)
    readings = []
    for rnd in rounds_in_window:
        if not rnd.anonymized_fields:
            readings.append({"run_id": rnd.run_id, "state": "no scope", "leaks": 0,
                             "report": str(rnd.path)})
            continue
        recorded = sent_path(args.keymap, rnd.run_id)
        if not recorded.exists():
            readings.append({"run_id": rnd.run_id, "state": "unsettled", "leaks": 0,
                             "report": str(rnd.path), "missing": str(recorded)})
            continue
        result = audit.recall(load_sent(recorded), keymap)
        readings.append({"run_id": rnd.run_id, "state": "settled",
                         "leaks": result["leaks"], "sent_log": str(recorded),
                         "format_hits": result["format_hits"],
                         "keymap_values_in_clear": result["keymap_values_in_clear"]})
    return readings


def _ratio_condition(rounds_in_window, args, artifacts) -> pilot_gate.Condition:
    drafted = pilot_delta.drafted_by_round(rounds_in_window, args.runs_dir)
    latest = pilot_delta.latest_drafting_round(drafted)
    carrying = {t: b for t, (r, b) in latest.items() if r.cohort == pilot_delta.CARRYING}

    if len(carrying) < args.floor or args.offline:
        note = ("No Graph call was made, so nothing here says what was sent."
                if args.offline else None)
        return pilot_gate.drafts_accepted_ratio(len(carrying), args.floor, artifacts,
                                                None, note)

    from email_classifier.providers import get_drafter, get_thread_reader
    from email_classifier.stage1.policy import load_policy
    provider = load_policy(REPO_ROOT / "rules/policy.yaml").email_provider
    replies = get_thread_reader(provider).sent_replies(sorted(carrying))
    drafter = get_drafter(provider)
    still = {t for t in carrying if drafter.find_draft(t)}
    rows = draft_delta.compare(carrying, {t: r["body"] for t, r in replies.items()
                                          if t in carrying}, still)
    table = pilot_gate.accepted_table(rows, draft_delta.UNCHANGED_LABEL)
    return pilot_gate.drafts_accepted_ratio(len(carrying), args.floor, artifacts, table,
                                            None)


def main(argv=None) -> int:
    args = parse_args(argv)
    # No `tree_guard`, for the reason `scripts/pilot_delta.py` states: this publishes no
    # record another tree could disagree with, and the one thing it asks git — whether a
    # commit descends from the voice rules — resolves out of the shared object store.

    paths = [p for d in args.reports for p in sorted(Path(d).glob("*.json"))]
    if not paths:
        print(f"[ERROR] no report under {', '.join(str(d) for d in args.reports)} — this "
              "reads the serving host's reports, and an empty directory is a reader "
              "pointed at the wrong place, not an empty pilot", file=sys.stderr)
        return pilot_gate.EXIT_NOTHING_TO_READ

    window = window_of(args)
    placed = pilot_delta.rounds(paths, REPO_ROOT)
    in_window = [r for r in placed if window.holds(r.at)]
    # `LIVE_INPUT` and not the host the command runs on (STU-859): a `make run` is someone
    # at a keyboard watching, whatever machine it is typed on, and only the push path is
    # the unattended thing the milestone is about.
    live = [r for r in in_window if r.input_path == LIVE_INPUT]

    report_dirs = [str(d) for d in args.reports]
    print(f"[pilot-gate] window {window.describe()['since']} .. "
          f"{window.describe()['until']} | {len(in_window)} report(s) in it, "
          f"{len(live)} on the push path", file=sys.stderr)

    conditions = [
        pilot_gate.unattended_week(live, pilot_gate.health_entries(args.health_journal),
                                   window, args.health_journal, report_dirs),
        pilot_gate.no_pii_leak(_recall_readings(in_window, args),
                               [str(args.keymap.parent), str(args.keymap)]),
        _ratio_condition(in_window, args,
                         report_dirs + [str(d) for d in args.runs_dir]),
    ]

    for condition in conditions:
        print(f"\n{condition.verdict.upper()}  {condition.name}", file=sys.stderr)
        print(f"  {condition.summary}", file=sys.stderr)
        print(f"  read from: {', '.join(condition.artifacts)}", file=sys.stderr)

    code = pilot_gate.verdict_exit(conditions)
    result = {"window": window.describe(), "reports_in_window": len(in_window),
              "live_rounds": len(live), "exit": code,
              "conditions": [c.as_record() for c in conditions]}
    out = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        if (failure := pilot_gate.write_record(args.out, out)) is not None:
            print(f"\n[WARN] {failure} — the verdicts above stand, and the exit code is "
                  "still theirs", file=sys.stderr)
        else:
            print(f"\n[pilot-gate] wrote {args.out}", file=sys.stderr)
    else:
        print(out)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
