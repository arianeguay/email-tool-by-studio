#!/usr/bin/env python3
"""Post-stage of `calendar` (STU-2089): check each extraction, plan the events.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

`calendar_plan.plan` does the work; this reads the fan-out, writes the rows it returns to
`logs/calendar/events.jsonl` and `logs/calendar/review.jsonl`, and prints the plan. No
writer exists yet, so the plan changes no calendar.

Output: { "run_id", "planned", "review", "failed", "plan", "events_path", "review_path" }
"""

import json
import sys
from pathlib import Path

from email_classifier import calendar_plan
from email_classifier.anonymize import load_keymap
from email_classifier.orchestrate import batch
from email_classifier.stage1.policy import load_policy

_PRE_STAGE = "calendar-pre"
_FANOUT_STAGE = "calendar-fanout"


def main() -> None:
    payload = json.load(sys.stdin)
    outputs = payload.get("previous_outputs") or {}
    pre = outputs[_PRE_STAGE]
    paths = pre["paths"]
    events_path, review_path = Path(paths["events"]), Path(paths["review"])
    plan, review, failed = [], [], 0
    if pre["pending"]:
        calendar = load_policy(Path(paths["policy"])).calendar
        results = batch.results_by_index(outputs.get(_FANOUT_STAGE))
        failed = sum(1 for index in range(len(pre["pending"]))
                     if results.get(index, {}).get("status") != "success")
        plan, events, review = calendar_plan.plan(
            pre["pending"], results, calendar_plan.read_rows(events_path), calendar,
            load_keymap(Path(paths["keymap"])), pre["run_id"])
        calendar_plan.append_rows(events_path, events)
        calendar_plan.append_rows(review_path, review)

    print(f"[calendar-post] {len(plan)} planned | {len(review)} to review | {failed} failed",
          file=sys.stderr)
    json.dump({
        "run_id": pre["run_id"],
        "planned": len(plan),
        "review": len(review),
        "failed": failed,
        "plan": plan,
        "events_path": str(events_path),
        "review_path": str(review_path),
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
