#!/usr/bin/env python3
"""Apply stage of `mailbox-full` (STU-846): a report -> mailbox operations.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

Conversations an earlier report decided are applied again once their rule expiry has
passed (STU-2070), so the archive lands in this run's journal and `undo` reverts it.

Dry-run unless the input says `apply: true` — anything else (absent, false, a
string) plans and mutates nothing, so a malformed input can never be the reason
a mailbox got written to. Policy gating (`deterministic_can_finalize`,
`destructive_actions_allowed`) stays where it is, in the executor.

A failed operation exits non-zero: the parent run fails rather than reporting a
success over a partial apply. The journal on disk holds what did happen.

Input:  { "report": "<report path>", "apply": true|false }
Output: { "run_id", "report_path", "journal_path", "dry_run", "acted", "unsubscribed" }
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from email_classifier.apply.executor import apply_verdicts, expired_threads, render_summary
from email_classifier.apply.journal import ApplyJournal, journal_path
from email_classifier.apply import unsubscribe
from email_classifier.core.contract import load_taxonomy
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.providers import get_writer, preflight_write
from email_classifier.stage1.policy import load_policy
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def main() -> None:
    payload = json.load(sys.stdin)
    ctx = yaml.safe_load(payload.get("additional_context", "") or "") or {}
    if "report" not in ctx:
        raise SystemExit("[ERROR] apply input needs a `report` (the report JSON to apply)")

    dry_run = ctx.get("apply") is not True
    policy = load_policy(REPO_ROOT / ctx.get("policy", "rules/policy.yaml"))
    taxonomy = load_taxonomy(REPO_ROOT / ctx.get("taxonomy", "rules/taxonomy.yaml"))
    if not dry_run:
        refusal = preflight_write(policy.email_provider)
        if refusal:
            raise SystemExit(f"[ERROR] {refusal}")

    report_path = REPO_ROOT / ctx["report"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    verdicts = [EmailVerdict(**v) for v in report["verdicts"]]
    now = datetime.now(timezone.utc)
    earlier = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(report_path.parent.glob("*.json"))
        if path != report_path
    ]
    verdicts += expired_threads(earlier, {v.thread_id for v in verdicts}, now)

    journal_dir = REPO_ROOT / ctx.get("journal_dir", "logs/applied")
    journal = ApplyJournal(journal_path(journal_dir, report["run_id"], dry_run=dry_run),
                           report["run_id"])
    writer = get_writer(policy.email_provider)
    outcomes = apply_verdicts(
        verdicts, writer, policy, journal, taxonomy, dry_run=dry_run, now=now,
    )
    unsubscribed = unsubscribe.unsubscribe(verdicts, writer, policy, report["run_id"], dry_run=dry_run)

    print(render_summary(outcomes, dry_run=dry_run), file=sys.stderr)
    if policy.auto_unsubscribe:
        print(unsubscribe.render_summary(unsubscribed, dry_run=dry_run), file=sys.stderr)
    print(f"\nJournal written to {journal.path}", file=sys.stderr)

    errors = [o for o in outcomes if o.status == "error"]
    if errors:
        raise SystemExit(f"[ERROR] {len(errors)} verdicts failed to apply — see {journal.path}")

    acted = "planned" if dry_run else "applied"
    json.dump({
        "run_id": report["run_id"],
        "report_path": str(report_path),
        "journal_path": str(journal.path),
        "dry_run": dry_run,
        "acted": sum(1 for o in outcomes if o.status == acted),
        "unsubscribed": unsubscribed,
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
