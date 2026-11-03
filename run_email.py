#!/usr/bin/env python3
"""End-to-end orchestrator (STU-392): dump|fetch -> stage1 -> stage2 -> report.

Read-only of the mailbox from end to end: no apply_label, no trash, no mutation path.
Config/setup errors fail loud BEFORE processing (missing `studio` CLI, malformed rules/
policy, bad dump). Per-email Stage 2 errors are recorded in the report and the run
continues. Exit 0 if every email classified; exit 2 if any per-email error occurred.
"""

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from email_classifier import deployment, providers
from email_classifier.anonymize import (
    Tokenizer,
    load_keymap,
    record_minted,
    run_keymap_path,
    save_keymap,
    sent_path,
)
from email_classifier.core import schema
from email_classifier.core.contract import load_taxonomy
from email_classifier.fetch import window
from email_classifier.fetch.threads import expand_to_conversations
from email_classifier.learn.store import VerdictStore
from email_classifier.orchestrate.pipeline import run
from email_classifier.orchestrate.provenance import (
    collect,
    resolved_provider,
    unusable_provider,
)
from email_classifier.orchestrate.report import render_human, report_to_dict
from email_classifier.orchestrate.stage2_client import (
    StudioCliClassifier,
    preflight_anonymize_scope,
)
from email_classifier.stage1.frontier_log import make_logger
from email_classifier.stage1.policy import Policy, load_policy
from email_classifier.stage1.rule_engine import load_rules

REPO_ROOT = deployment.ROOT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dump", type=Path, help="Frozen EmailInput JSON array dump.")
    source.add_argument("--fetch", action="store_true",
                        help="Live read-only fetch from the configured provider.")
    parser.add_argument("--query", default="", help="Provider search query (with --fetch).")
    parser.add_argument("--max-results", type=int, default=100,
                        help="Max emails to fetch (with --fetch).")
    parser.add_argument("--provider", default=None,
                        help="Override the Studio provider the agents name themselves "
                             "(claude-code|anthropic|mock).")
    parser.add_argument("--report-dir", type=Path, default=REPO_ROOT / "reports")
    parser.add_argument("--dump-dir", type=Path, default=REPO_ROOT / "dumps",
                        help="Where --fetch persists its fetched inputs (audit artifact).")
    parser.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    parser.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")
    parser.add_argument("--taxonomy", type=Path, default=REPO_ROOT / "rules" / "taxonomy.yaml")
    parser.add_argument("--log", type=Path, default=REPO_ROOT / "logs" / "frontier.jsonl")
    parser.add_argument("--learned", type=Path, default=REPO_ROOT / "rules" / "learned.yaml",
                        help="Promoted rules layer (STU-822); ignored when absent.")
    parser.add_argument("--verdict-store", type=Path,
                        default=REPO_ROOT / "logs" / "verdicts.jsonl",
                        help="Append-only LLM verdict corpus feeding `make promote`.")
    parser.add_argument("--keymap", type=Path,
                        default=REPO_ROOT / "logs" / "anonymization" / "keymap.json",
                        help="Shared PII keymap, so a value keeps its token across runs.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose stage-2 diagnostics (per-email studio cmd + claude-code "
                             "lifecycle logs from the spawned studio process).")
    return parser.parse_args(argv)


def _load_emails(args: argparse.Namespace, policy: Policy, run_id: str,
                 start_date) -> tuple[list, Path, date | None, str | None]:
    """Emails, the path they came from (STU-859), the floor applied, the mailbox read.

    A dump is not re-bounded by `start_date` (STU-1123). It is a fetch that was
    already bounded, frozen; re-applying today's floor to it would refuse to replay
    yesterday's corpus, which is the wrong direction for the rollback and catch-up
    `--dump` exists for. The floor governs where mail enters from the mailbox.

    Which is why the floor is *returned* rather than re-derived by the caller: the
    provenance a report publishes has to be the boundary its own corpus was read
    under, and a second place deciding that is a second place to get it wrong.

    The mailbox rides out the same way and for the same reason (STU-1183). Only the
    branch below reads one, so only it can name one: stamping the box this host is
    configured for today onto a replay of somebody else's frozen dump would put the
    wrong store on ids that came from another, which is the whole failure the field
    exists to end. `input_path` already names the dump, and the run that wrote it
    names its own mailbox.
    """
    if args.dump:
        if not args.dump.exists():
            print(f"[ERROR] dump not found: {args.dump}", file=sys.stderr)
            raise SystemExit(1)
        return schema.load_many(args.dump), args.dump, None, None
    fetcher = providers.get_fetcher(policy.email_provider)
    tz = ZoneInfo(policy.timezone)
    raw = expand_to_conversations(
        fetcher,
        fetcher.fetch_inputs(
            query=args.query, max_results=args.max_results,
            since=window.since_iso(start_date, tz),
        ),
        start_date=start_date,
        tz=tz,
    )
    emails = [schema.validate(item) for item in raw]

    # A fetch run has no frozen input to audit against, so persist what it actually
    # classified (STU-821): `audit_run.py check --dump dumps/<run-id>.json`.
    args.dump_dir.mkdir(parents=True, exist_ok=True)
    dump_path = args.dump_dir / f"{run_id}.json"
    dump_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Fetched inputs written to {dump_path}", file=sys.stderr)
    return emails, dump_path, start_date, providers.configured_mailbox(policy.email_provider)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Diagnostics go to stderr (stdout is reserved for the human report). INFO surfaces
    # per-email outcomes; -v adds the studio command and turns on the claude-code
    # provider's own lifecycle logging inside the spawned `studio` process.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.verbose:
        os.environ["STUDIO_LOG_CLAUDE_CODE"] = "1"

    # Config/setup: fail loud BEFORE any processing.
    if shutil.which("studio") is None:
        print("[ERROR] `studio` CLI not found on PATH. Install Studio first.", file=sys.stderr)
        return 1
    rules = load_rules(args.rules, args.learned)  # raises loudly on missing/malformed rules
    policy = load_policy(args.policy)  # raises loudly on missing/malformed policy
    taxonomy = load_taxonomy(args.taxonomy)  # raises loudly on missing/malformed taxonomy
    unsupported = preflight_anonymize_scope(policy.anonymization_fields, cwd=REPO_ROOT)
    if unsupported:
        print(f"[ERROR] {unsupported}", file=sys.stderr)
        return 1
    # `--provider` is an override, so the child gets the flag only when one was
    # asked for; provenance gets the provider that answers either way (STU-1013).
    # Resolved up here rather than below the fetch so the run refuses before it reads
    # any mail: a provider that cannot initialize fails every escalation identically,
    # and left to the first call it loses them one at a time inside a green run
    # (STU-1128).
    provider = resolved_provider(REPO_ROOT / ".studio", args.provider)
    unusable = unusable_provider(REPO_ROOT / ".studio", provider)
    if unusable:
        print(f"[ERROR] {unusable}", file=sys.stderr)
        return 1
    logger = make_logger(policy, args.log)
    # Resolved once per run, never at policy load (STU-1123).
    start_date = window.resolve(policy.start_date, ZoneInfo(policy.timezone))
    # raises loudly on bad dump / schema violation
    emails, input_path, applied_floor, mailbox = _load_emails(
        args, policy, logger.run_id, start_date)

    # No `anonymize_fields`: the scope is honoured below by this repo's tokenizer, not
    # by the engine's middleware, whose salutation-anchored detector finds no name in
    # French mail (STU-890 / STU-1009).
    stage2 = StudioCliClassifier(taxonomy=taxonomy, provider=args.provider, cwd=REPO_ROOT)
    tokenizer = Tokenizer(load_keymap(args.keymap)) if policy.anonymization_fields else None
    provenance = collect(
        provider, args.rules, REPO_ROOT, args.learned, policy.anonymization_fields,
        # The floor this corpus was actually read under, null over a frozen dump
        # nobody bounded today (STU-1123) — the same thing `classify_pre` records.
        input_path, applied_floor,
        # And the mailbox those ids resolve in, null over that same frozen dump
        # (STU-1183): this run read no mailbox to name.
        mailbox,
    )
    store = VerdictStore(args.verdict_store)
    try:
        report = run(emails, rules, policy, stage2, logger, provenance, store,
                     tokenizer, sent_path(args.keymap, logger.run_id))
    finally:
        # A value that reached the LLM keeps its token even if the run then died.
        if tokenizer is not None:
            save_keymap(args.keymap, tokenizer.keymap)
            record_minted(run_keymap_path(args.keymap, logger.run_id), tokenizer.minted())

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"{report.run_id}.json"
    report_path.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(render_human(report))
    print(f"\nReport written to {report_path}", file=sys.stderr)

    return 2 if report.totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
