#!/usr/bin/env python3
"""Apply a classification report to the mailbox — dry-run by default (STU-407).

    apply_email.py run reports/<run-id>.json [--apply]
    apply_email.py undo <run-id>

`run` without `--apply` issues no mutation at all: it journals the plan to
logs/applied/<run-id>.plan.jsonl and prints the summary. With `--apply`, every
operation is journaled to logs/applied/<run-id>.jsonl as it happens, which is
what `undo` replays backwards.

`undo` also deletes the reply drafts the run created (STU-854) — a run reverted
down to its labels but leaving its drafts behind is not a reverted run. Which
drafts were the run's is read from its report, so `undo` wants the report the
run produced alongside its journal.

A run that only planned is undoable for that reason alone: drafting is outside
`auto_apply` (STU-946), so a push run under the default policy leaves nothing but
a plan and a real draft, and refusing it over the missing journal would leave the
one thing it did create in the mailbox.
"""

import argparse
import json
import sys
from pathlib import Path

from email_classifier import deployment
from email_classifier.apply.executor import apply_verdicts, render_summary
from email_classifier.apply.journal import ApplyJournal, journal_path, read_entries
from email_classifier.apply.undo import (
    DraftUndoOutcome,
    drafts_of_run,
    render_drafts_summary,
    undo_drafts,
    undo_entries,
)
from email_classifier.apply.undo import render_summary as render_undo_summary
from email_classifier.core.contract import load_taxonomy
from email_classifier.orchestrate.drafts import pending_message_ids
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.providers import Writer, get_drafter, get_writer, preflight_write
from email_classifier.stage1.policy import Policy, load_policy

REPO_ROOT = deployment.ROOT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal-dir", type=Path, default=REPO_ROOT / "logs" / "applied")
    parser.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")
    parser.add_argument("--taxonomy", type=Path, default=REPO_ROOT / "rules" / "taxonomy.yaml")
    parser.add_argument("--draft-journal", type=Path,
                        default=REPO_ROOT / "logs" / "drafts" / "journal.jsonl")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Apply a report (dry-run unless --apply).")
    run.add_argument("report", type=Path, help="Report JSON written by run_email.py.")
    run.add_argument("--apply", action="store_true",
                     help="Issue real mutations. Without it, nothing is mutated.")

    undo = commands.add_parser(
        "undo",
        help="Revert an applied run: its journal backwards, then the drafts it created.",
    )
    undo.add_argument("run_id", help="Run id of the journal to invert.")
    undo.add_argument("--report", type=Path, default=None,
                      help="Report of the run, which names the drafts it created "
                           "(default reports/<run-id>.json). Missing: drafts are left.")
    return parser.parse_args(argv)


def _writer(policy: Policy) -> Writer:
    return get_writer(policy.email_provider)


def _refuse_without_write_scope(policy: Policy) -> bool:
    """True (after printing why) when the provider grant cannot mutate the mailbox.

    Checked before the first operation rather than discovered as a 403 partway
    through a batch (STU-844). A dry-run never reaches it: it issues no mutation.
    """
    refusal = preflight_write(policy.email_provider)
    if refusal:
        print(f"[ERROR] {refusal}", file=sys.stderr)
    return refusal is not None


def _run(args: argparse.Namespace) -> int:
    if not args.report.exists():
        print(f"[ERROR] report not found: {args.report}", file=sys.stderr)
        return 1
    report = json.loads(args.report.read_text(encoding="utf-8"))
    verdicts = [EmailVerdict(**v) for v in report["verdicts"]]
    policy = load_policy(args.policy)  # raises loudly on missing/malformed policy
    taxonomy = load_taxonomy(args.taxonomy)  # raises loudly on missing/malformed taxonomy

    if args.apply and _refuse_without_write_scope(policy):
        return 1

    run_id = report["run_id"]
    journal = ApplyJournal(
        journal_path(args.journal_dir, run_id, dry_run=not args.apply), run_id
    )
    outcomes = apply_verdicts(
        verdicts, _writer(policy), policy, journal, taxonomy, dry_run=not args.apply
    )

    print(render_summary(outcomes, dry_run=not args.apply))
    print(f"\nJournal written to {journal.path}", file=sys.stderr)
    if not args.apply:
        print("Re-run with --apply to execute this plan.", file=sys.stderr)
    return 2 if any(o.status == "error" for o in outcomes) else 0


def _undo_drafts(args: argparse.Namespace, policy: Policy) -> list[DraftUndoOutcome]:
    """Delete the drafts the run created (STU-854), or say loudly why it cannot.

    The drafts journal is shared by every run, so the run's report is what tells
    its drafts from another run's. Without either file there is no correlation to
    make — reverting nothing is the only safe answer, and it is not a silent one.
    """
    report = args.report or REPO_ROOT / "reports" / f"{args.run_id}.json"
    missing = [p for p in (report, args.draft_journal) if not p.exists()]
    if missing:
        print(f"[WARN] drafts not reverted: no {missing[0]}", file=sys.stderr)
        return []
    pending = set(pending_message_ids(json.loads(report.read_text(encoding="utf-8"))))
    candidates = drafts_of_run(read_entries(args.draft_journal), pending)
    if not candidates:
        return []
    return undo_drafts(candidates, get_drafter(policy.email_provider),
                       args.draft_journal, args.run_id)


def _undo(args: argparse.Namespace) -> int:
    path = journal_path(args.journal_dir, args.run_id, dry_run=False)
    try:
        entries = read_entries(path)
    except FileNotFoundError as exc:
        # A plan mutated nothing, so there is nothing to invert — but the run's drafts
        # are real (STU-946). The plan file is what tells that run from a mistyped id.
        if not journal_path(args.journal_dir, args.run_id, dry_run=True).exists():
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 1
        entries = []
    policy = load_policy(args.policy)
    if _refuse_without_write_scope(policy):
        return 1
    journal = ApplyJournal(args.journal_dir / f"{args.run_id}.undo.jsonl", args.run_id)
    outcomes = undo_entries(entries, _writer(policy), journal)
    draft_outcomes = _undo_drafts(args, policy)

    print(render_undo_summary(outcomes, args.run_id))
    if draft_outcomes:
        print(render_drafts_summary(draft_outcomes))
    print(f"\nUndo journal written to {journal.path}", file=sys.stderr)
    failed = [o for o in [*outcomes, *draft_outcomes] if o.status == "error"]
    return 2 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return _run(args) if args.command == "run" else _undo(args)


if __name__ == "__main__":
    raise SystemExit(main())
