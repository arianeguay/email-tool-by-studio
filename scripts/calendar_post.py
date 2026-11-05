#!/usr/bin/env python3
"""Post-stage of `calendar` (STU-2089): check each extraction, plan the events.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

`calendar_plan.plan` does the work; this reads the fan-out, writes the rows it returns to
`logs/calendar/events.jsonl` and `logs/calendar/review.jsonl`, and prints the plan.

Then every planned row not yet settled, this run's and any an earlier dry run left, goes
to Home Assistant when `calendar-pre` carries `apply: true` (STU-2090), each outcome
appended to the same journal. Otherwise each is printed and nothing is sent or appended.
A write that fails exits non-zero once the journal holds every outcome, like `apply`.

What the fan-out spent goes on a `completed` line of `logs/calendar/stages.jsonl`
(STU-2105), the shape `drafts-post` writes, so `audit_run.py cost` can add the leg. A
run with nothing to read writes no line: no calendar leg, never a zero one.

Output: { "run_id", "planned", "review", "failed", "plan", "dry_run", "written",
          "events_path", "review_path" }
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from email_classifier import alerts, calendar_plan, cost
from email_classifier.apply.draft_journal import COMPLETED
from email_classifier.apply import home_assistant
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
        fanout = outputs.get(_FANOUT_STAGE)
        results = batch.results_by_index(fanout)
        failed = sum(1 for index in range(len(pre["pending"]))
                     if results.get(index, {}).get("status") != "success")
        plan, events, review = calendar_plan.plan(
            pre["pending"], results, calendar_plan.read_rows(events_path), calendar,
            load_keymap(Path(paths["keymap"])), pre["run_id"])
        calendar_plan.append_rows(events_path, events)
        calendar_plan.append_rows(review_path, review)
        # As in `drafts-post`: a resumed item was bought by an earlier attempt, and a
        # fan-out that handed nothing back still dispatched every item, priced as a floor.
        items = results.values() if fanout is not None else [{} for _ in pre["pending"]]
        spend = cost.leg((r for r in items if not r.get("cached")), pre.get("provider"))
        calendar_plan.append_rows(Path(paths["stage_log"]), [{
            "timestamp": datetime.now(timezone.utc).isoformat(), "run_id": pre["run_id"],
            "stage": cost.CALENDAR_LEG, "status": COMPLETED, "tokens": spend}])

    print(f"[calendar-post] {len(plan)} planned | {len(review)} to review | {failed} failed",
          file=sys.stderr)

    dry_run = pre.get("apply") is not True
    written = []
    unsettled = (home_assistant.pending(calendar_plan.read_rows(events_path))
                 if pre.get("enabled") else [])
    if dry_run:
        for row in unsettled:
            print(f"[calendar-post] would {home_assistant.describe(row)}", file=sys.stderr)
    elif unsettled:
        try:
            client = home_assistant.from_env()
        except home_assistant.HomeAssistantError as exc:
            alerts.notify(f"calendar: {exc}; {len(unsettled)} planned event(s) wait")
            raise SystemExit(f"[ERROR] {exc}")
        written = home_assistant.apply(calendar_plan.read_rows(events_path), client,
                                       pre["run_id"], events_path)
        for row in written:
            print(f"[calendar-post] {row['status']}: {home_assistant.describe(row)}"
                  + (f" ({row['reason']})" if row.get("reason") else ""), file=sys.stderr)
        failures = [row for row in written if row["status"] == home_assistant.FAILED]
        if failures:
            raise SystemExit(f"[ERROR] {len(failures)} calendar write(s) failed, "
                             f"see {events_path}")
    json.dump({
        "run_id": pre["run_id"],
        "planned": len(plan),
        "review": len(review),
        "failed": failed,
        "plan": plan,
        "dry_run": dry_run,
        "written": len(written),
        "events_path": str(events_path),
        "review_path": str(review_path),
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
