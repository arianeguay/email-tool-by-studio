#!/usr/bin/env python3
"""The draft-versus-sent reading across the whole pilot, cohorted by serving commit.

    scripts/pilot_delta.py --reports reports --runs-dir .studio/runs

`audit_run.py reviewed` answers this for one run. The milestone's question spans the
pilot, and on 2026-09-01 answering it took a scratch aggregator that was never
committed — so the figure could not be reproduced or re-run. This is that reading, with
the half the scratch version lacked: which commit wrote each draft.

Local, and for three reasons at once (STU-1182): the reports are gitignored, the Studio
journals behind them are gitignored, and the sent replies come from Graph.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

KERNEL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KERNEL_ROOT))

from email_classifier import draft_delta, pilot_delta  # noqa: E402
from email_classifier.apply.journal import read_entries  # noqa: E402
from email_classifier.pilot_gate import write_record  # noqa: E402
from email_classifier import deployment  # noqa: E402

REPO_ROOT = deployment.ROOT


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, nargs="+", default=[Path("reports")],
                        help="Directories holding the production reports.")
    parser.add_argument("--runs-dir", type=Path, nargs="+", default=[Path(".studio/runs")],
                        help="Directories holding the Studio journals behind them.")
    parser.add_argument("--draft-journal", type=Path,
                        default=Path("logs/drafts/journal.jsonl"),
                        help="The shared per-draft journal: when each draft was written, "
                             "which is what a draft is paired with the first send after.")
    parser.add_argument("--floor", type=int, default=10,
                        help="Conversations the carrying table needs before it is worth "
                             "reading. Under it the table still prints, saying so.")
    parser.add_argument("--offline", action="store_true",
                        help="Place the rounds and stop, without calling Graph. Answers "
                             "'is there enough carrying traffic yet' for free.")
    parser.add_argument("--facts", action="store_true",
                        help="Also ask the draft-facts reader whether each sent reply kept, "
                             "extended or contradicted the draft (live LLM calls).")
    parser.add_argument("-o", "--out", type=Path)
    return parser.parse_args(argv)


def _report_paths(dirs):
    return [p for d in dirs for p in sorted(Path(d).glob("*.json"))]


def main(argv=None) -> int:
    args = parse_args(argv)
    # No `tree_guard` here, unlike the `--check` family: this publishes no record for
    # another tree to disagree with, and the one thing it asks git — whether a commit
    # descends from the rules — resolves out of the shared object store, so every
    # worktree answers it identically.

    paths = _report_paths(args.reports)
    if not paths:
        print(f"[ERROR] no report under {', '.join(str(d) for d in args.reports)} — this "
              "reads the serving host's reports, and an empty directory is not an empty "
              "pilot", file=sys.stderr)
        return 2

    rounds = pilot_delta.rounds(paths, REPO_ROOT)
    print(f"[pilot-delta] {len(rounds)} report(s) | rules commit {pilot_delta.RULES_COMMIT}",
          file=sys.stderr)

    unmatched: set[str] = set()
    drafted = pilot_delta.drafted_by_round(rounds, args.runs_dir, unmatched)

    latest = pilot_delta.latest_drafting_round(drafted)
    scored_rounds = [rnd for rnd, _ in latest.values()]
    cover = pilot_delta.coverage(scored_rounds)
    by_cohort = {name: {t: b for t, (r, b) in latest.items() if r.cohort == name}
                 for name in pilot_delta.COHORTS}

    print(f"[pilot-delta] {len(latest)} conversation(s) drafted | "
          + " | ".join(f"{n}: {len(by_cohort[n])}" for n in pilot_delta.COHORTS),
          file=sys.stderr)

    result = {"reports": len(rounds), "conversations": len(latest), "coverage": cover,
              "conversations_by_cohort": {n: len(by_cohort[n]) for n in pilot_delta.COHORTS},
              "unmatched_drafts": sorted(unmatched),
              "archive": draft_delta.ARCHIVE, "round_before": draft_delta.ROUND_BEFORE}

    if args.offline:
        result["tables"] = None
        result["offline"] = ("Rounds placed only. No Graph call was made, so nothing here "
                             "says what was sent — an offline run answers whether there is "
                             "carrying traffic yet, never how it scored.")
    else:
        from email_classifier.providers import get_drafter, get_thread_reader
        from email_classifier.stage1.policy import load_policy
        provider = load_policy(REPO_ROOT / "rules/policy.yaml").email_provider
        reader = get_thread_reader(provider)
        if not args.draft_journal.exists():
            print(f"[ERROR] no drafts journal at {args.draft_journal}: it is the only "
                  "record of when each draft was written", file=sys.stderr)
            return 2
        entries = read_entries(args.draft_journal)
        written = pilot_delta.written_at(latest, entries)
        replies = reader.sent_replies(dict(sorted(written.items())))
        # Same rule as `audit_run.py reviewed`: no write time, no pairing and no score.
        result["no_write_time"] = sorted(set(latest) - set(written))
        drafter = get_drafter(provider)
        still = {t for t in latest if drafter.find_draft(t)}

        tables = {}
        for name in pilot_delta.COHORTS:
            bodies = {t: b for t, b in by_cohort[name].items() if t in written}
            if not bodies:
                # Not an empty table: a cohort nothing fell into has no reading, and a
                # zeroed one would average into a comparison as though it did.
                tables[name] = None
                continue
            sent_bodies = {t: r["body"] for t, r in replies.items() if t in bodies}
            rows = draft_delta.compare(bodies, sent_bodies, still)
            table = draft_delta.summarise(rows, list(bodies.values()),
                                          list(sent_bodies.values()))
            if args.facts:
                from email_classifier import fact_read
                table["by_facts"] = fact_read.annotate(rows, bodies, sent_bodies, REPO_ROOT)
            tables[name] = {**table, "rows": rows}
        result["tables"] = tables

    carrying = len(by_cohort[pilot_delta.CARRYING])
    result["carrying_conversations"] = carrying
    result["enough"] = pilot_delta.enough(carrying, args.floor)
    if not result["enough"]:
        result["note"] = (
            f"{carrying} conversation(s) on a commit carrying the voice rules, under the "
            f"floor of {args.floor}. The carrying table is a reading of {carrying} "
            "conversation(s) and is not yet the pilot-wide answer; the pre-rules table is "
            "the baseline and is complete.")
        print(f"[pilot-delta] {result['note']}", file=sys.stderr)

    out = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        if (failure := write_record(args.out, out)) is not None:
            # Onto stdout instead: a Graph pass over the pilot is hours of reading, and
            # losing it to a directory this account cannot write is the worse outcome.
            print(f"[WARN] {failure}", file=sys.stderr)
            print(out)
        else:
            print(f"[pilot-delta] wrote {args.out}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
