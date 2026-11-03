#!/usr/bin/env python3
"""What the push path decided lately, one line per conversation.

    scripts/recent_mail.py --hours 24
    ssh <host> 'cd <checkout> && python3 -' < scripts/recent_mail.py

Reads `reports/`, so it runs where the runs happened. A report lists the whole fetched
conversation, not only the message that triggered the run, so its verdict count is not a
count of new mail: this groups by conversation and shows the newest message of each.
Stdlib only, so it also runs on a host that has not pulled it yet.
"""

import argparse
import json
import time
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--reports", type=Path, default=Path("reports"))
    parser.add_argument("--width", type=int, default=160)
    args = parser.parse_args()

    since = time.time() - args.hours * 3600
    latest = {}
    for path in args.reports.glob("*.json"):
        if path.stat().st_mtime < since:
            continue
        for verdict in json.loads(path.read_text()).get("verdicts", []):
            thread = verdict.get("thread_id")
            if thread not in latest or verdict["date"] > latest[thread]["date"]:
                latest[thread] = {**verdict, "run": path.stem[:8]}

    rows = sorted(latest.values(), key=lambda v: v["date"])
    for v in rows:
        print(f'{v["date"][:16]}Z  {v["run"]}  {v["bucket"]}/{v.get("subtype") or "-"}  '
              f'{v["suggested_action"]:<13} {v["path"]:<4} '
              f'{(v.get("reasoning") or "").replace(chr(10), " ")[:args.width]}')
    needs_reply = sum(v["suggested_action"] == "needs_reply" for v in rows)
    print(f"\n{len(rows)} conversation(s) in the last {args.hours:g}h, {needs_reply} needing a reply")


if __name__ == "__main__":
    main()
