#!/usr/bin/env python3
"""Candidate drafting rules from the reviewer's corrections over a window (STU-1108).

    scripts/correction_rules.py --reports reports --runs-dir .studio/runs

Pairs each conversation's latest draft with the reply sent in its place, the way
`pilot_delta.py` does, keeps the pairs the reviewer changed, redacts them, and asks the
correction-reader for rules. Writes a candidates file for a human to accept into a
skill or reject; nothing edits a prompt.

Local: the reports, the journals and the drafts journal are gitignored, the sends come
from the mailbox, and the reader is a live LLM call.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

KERNEL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KERNEL_ROOT))

from email_classifier import correction_rules, deployment, pilot_delta  # noqa: E402
from email_classifier.apply.journal import read_entries  # noqa: E402
from email_classifier.pilot_gate import write_record  # noqa: E402

REPO_ROOT = deployment.ROOT
OUT_DIR = Path("logs/corrections")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reports", type=Path, nargs="+", default=[Path("reports")])
    parser.add_argument("--runs-dir", type=Path, nargs="+", default=[Path(".studio/runs")])
    parser.add_argument("--draft-journal", type=Path,
                        default=Path("logs/drafts/journal.jsonl"))
    # The reader reads real mail, so it runs where the host runs (STU-1128, STU-2088).
    parser.add_argument("--provider",
                        default=os.environ.get("STUDIO_PROVIDER") or correction_rules.DEFAULT_PROVIDER)
    parser.add_argument("-o", "--out", type=Path,
                        help=f"Default: {OUT_DIR}/<UTC time>.json, on the retention shelf.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    paths = [p for d in args.reports for p in sorted(Path(d).glob("*.json"))]
    if not paths:
        print(f"[ERROR] no report under {', '.join(map(str, args.reports))}", file=sys.stderr)
        return 2
    if not args.draft_journal.exists():
        print(f"[ERROR] no drafts journal at {args.draft_journal}: it is the only record "
              "of when each draft was written", file=sys.stderr)
        return 2

    latest = pilot_delta.latest_drafting_round(
        pilot_delta.drafted_by_round(pilot_delta.rounds(paths, REPO_ROOT), args.runs_dir))
    written = pilot_delta.written_at(latest, read_entries(args.draft_journal))

    from email_classifier.providers import get_thread_reader
    from email_classifier.stage1.policy import load_policy
    reader = get_thread_reader(load_policy(REPO_ROOT / "rules/policy.yaml").email_provider)
    sent = {t: r["body"] for t, r in reader.sent_replies(dict(sorted(written.items()))).items()}
    drafted = {t: body for t, (_, body) in latest.items() if t in written}

    pairs = correction_rules.redacted(correction_rules.corrected(drafted, sent))
    print(f"[corrections] {len(latest)} drafted | {len(sent)} paired with a send | "
          f"{len(pairs)} corrected", file=sys.stderr)
    result = correction_rules.propose(pairs, REPO_ROOT, args.provider or None)
    record = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "min_support": correction_rules.MIN_SUPPORT, "provider": args.provider,
              "drafted": len(latest), "paired": len(sent), "corrected": len(pairs),
              "note": "Candidates for a human to accept into a skill or reject. "
                      "Nothing here was applied to any prompt.",
              **result}

    out = args.out or OUT_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    payload = json.dumps(record, indent=2, ensure_ascii=False)
    if (failure := write_record(out, payload)) is not None:
        print(f"[WARN] {failure}", file=sys.stderr)
        print(payload)
    else:
        print(f"[corrections] {len(record.get('candidates') or [])} candidate(s) | wrote {out}",
              file=sys.stderr)
    return 0 if result["reading"] == correction_rules.OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
