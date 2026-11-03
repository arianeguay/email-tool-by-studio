#!/usr/bin/env python3
"""Apply stage of `mailbox-full` (STU-846): a report -> mailbox operations.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

Dry-run unless the input says `apply: true` — anything else (absent, false, a
string) plans and mutates nothing, so a malformed input can never be the reason
a mailbox got written to. Policy gating (`deterministic_can_finalize`,
`destructive_actions_allowed`) stays where it is, in the executor.

A failed operation exits non-zero: the parent run fails rather than reporting a
success over a partial apply. The journal on disk holds what did happen.

Input:  { "report": "<report path>", "apply": true|false }
Output: { "run_id", "report_path", "journal_path", "dry_run", "acted" }
"""

import json
import sys
from pathlib import Path

import yaml

from email_classifier.apply.executor import apply_verdicts, render_summary
from email_classifier.apply.journal import ApplyJournal, journal_path
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

    journal_dir = REPO_ROOT / ctx.get("journal_dir", "logs/applied")
    journal = ApplyJournal(journal_path(journal_dir, report["run_id"], dry_run=dry_run),
                           report["run_id"])
    outcomes = apply_verdicts(
        verdicts, get_writer(policy.email_provider), policy, journal, taxonomy, dry_run=dry_run
    )

    print(render_summary(outcomes, dry_run=dry_run), file=sys.stderr)
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
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
