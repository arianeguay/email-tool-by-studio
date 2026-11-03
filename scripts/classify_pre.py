#!/usr/bin/env python3
"""Pre-stage of `classify-batch` (STU-843): Stage 1, then build the fan-out items.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

Runs the deterministic rule engine over the corpus and writes the frontier log — the
same order as the per-email orchestrator, which logs the Stage-1 boundary before any
escalation, so a dead LLM path never costs the eval corpus its entries. What escalates
is emitted as `escalated` — one item per *conversation* (STU-944), the items the
following `call` fans out over.

Anonymization happens HERE, not in the leaf: one child run per email means a tokenizer
living inside the item pipeline would re-derive its own tokens per email and lose
cross-email consistency. See email_classifier/anonymize.py.

Input:  { "corpus": "<dir|json>", "provider": "...", "run_id": "...", ...path overrides }
Output: { "run_id", "needs_verdict", "escalated", "stage1", "prompt_fingerprint",
          "provenance", "composition", "paths" }
"""

import json
import sys
import uuid
from datetime import date
from pathlib import Path

import yaml

from email_classifier.anonymize import (
    Tokenizer,
    load_keymap,
    record_minted,
    run_keymap_path,
    record_sent,
    sent_path,
    save_keymap,
)
from email_classifier.core.types import RuleMatch
from email_classifier.eval.harness import load_corpus
from email_classifier.orchestrate import batch, composition, conversation
from email_classifier.orchestrate.pipeline import dedupe_by_message
from email_classifier.orchestrate.provenance import (
    collect,
    resolved_provider,
    unusable_provider,
)
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.stage1.frontier_log import make_logger
from email_classifier.stage1.policy import filter_action, load_policy
from email_classifier.stage1.rule_engine import apply, load_rules
from email_classifier import deployment

REPO_ROOT = deployment.ROOT
STUDIO_DIR = REPO_ROOT / ".studio"

_ESCALATE = "escalate_to_llm"

_DEFAULT_PATHS = {
    "rules": "rules/rules.yaml",
    "policy": "rules/policy.yaml",
    "taxonomy": "rules/taxonomy.yaml",
    "learned": "rules/learned.yaml",
    "log": "logs/frontier.jsonl",
    "report_dir": "reports",
    "verdict_store": "logs/verdicts.jsonl",
    "keymap": "logs/anonymization/keymap.json",
}


def _fetched_since(value) -> date | None:
    """The fetch stage's `start_date`, or None when no fetch stage ran."""
    if value in (None, ""):
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _fetched_from(value) -> str | None:
    """The mailbox the fetch stage read, or None when no fetch stage ran (STU-1183).

    Carried, never re-resolved here — the same rule the floor above follows, and for
    a sharper reason: `run-batch` over a frozen dump is the rollback and catch-up
    path, so the box this host is pointed at today is routinely not the one those ids
    came out of. Empty is how the fetch stage spells "nothing names one" across a
    template, and it means here what null means in the report.
    """
    if value in (None, ""):
        return None
    return str(value)


def _paths(ctx: dict) -> dict[str, str]:
    return {key: str(REPO_ROOT / ctx.get(key, default))
            for key, default in _DEFAULT_PATHS.items()}


def main() -> None:
    payload = json.load(sys.stdin)
    ctx = yaml.safe_load(payload.get("additional_context", "") or "") or {}
    if "corpus" not in ctx:
        raise SystemExit("[ERROR] classify-batch input needs a `corpus` (directory or JSON dump)")

    paths = _paths(ctx)
    # Before the fan-out, and before anything is tokenized: a provider that cannot
    # initialize fails every child run identically, and the `on_failure: continue` that
    # keeps one dead conversation from failing the batch (STU-392) then swallows all of
    # them — a run whose every stage is green, whose escalated verdicts all carry
    # `action: None`, and whose `provenance.provider` names a provider it never reached
    # (STU-1128). One dead email is the bias; a dead provider is a dead run.
    provider = resolved_provider(STUDIO_DIR, ctx.get("provider"))
    unusable = unusable_provider(STUDIO_DIR, provider)
    if unusable:
        raise SystemExit(f"[ERROR] {unusable}")
    rules = load_rules(Path(paths["rules"]), Path(paths["learned"]))
    policy = load_policy(Path(paths["policy"]))
    corpus_path = REPO_ROOT / ctx["corpus"]
    emails = dedupe_by_message(load_corpus(corpus_path))

    # `mailbox-full` passes the id its fetch stage minted, so the report and the dump
    # it consumed share a name (STU-896). Standalone, the batch mints its own.
    run_id = ctx.get("run_id") or str(uuid.uuid4())
    logger = make_logger(policy, Path(paths["log"]), run_id=run_id)

    keymap_path = Path(paths["keymap"])
    # Seeded from the previous run so a value keeps its token: the item input is the
    # fan-out's resume key, and a token that drifted would miss every cache entry.
    tokenizer = Tokenizer(load_keymap(keymap_path) if policy.anonymization_fields else None)
    prompt_fp = batch.prompt_fingerprint(STUDIO_DIR, policy.anonymization_fields)

    stage1: list[dict] = []
    matches: dict[str, RuleMatch] = {}
    for email in emails:
        match = filter_action(apply(email, rules), policy)
        logger.log(email, match)
        matches[email.message_id] = match
        stage1.append({
            "message_id": email.message_id,
            "thread_id": email.thread_id,
            "from_address": email.from_address,
            "date": email.date,
            "rule_id": match.rule_id,
            "action": match.action,
            "action_original": match.action_original,
            "confidence": match.confidence,
            "bucket": match.bucket,
            "escalated": match.action == _ESCALATE,
        })

    # One item per conversation, not per message (STU-944). Every escalated message of
    # a thread carries the same item index, so `classify-post` hands them all the one
    # verdict the call produced.
    escalations = conversation.collapse(
        emails, {r["message_id"] for r in stage1 if r["escalated"]}
    )
    escalated = [
        batch.escalation_item(
            e, [matches[mid] for mid in e.message_ids],
            tokenizer, policy.anonymization_fields, prompt_fp,
        )
        for e in escalations
    ]
    by_id = {r["message_id"]: r for r in stage1}
    for index, e in enumerate(escalations):
        for message_id in e.message_ids:
            by_id[message_id]["escalation_index"] = index

    if policy.anonymization_fields:
        save_keymap(keymap_path, tokenizer.keymap)
        record_minted(run_keymap_path(keymap_path, run_id), tokenizer.minted())
        record_sent(sent_path(keymap_path, run_id), escalated)

    provisional = [
        EmailVerdict(
            message_id=r["message_id"], thread_id=r["thread_id"],
            path="llm" if r["escalated"] else "deterministic", status="ok",
            bucket=r["bucket"], suggested_action=r["action"], confidence=r["confidence"],
            rule_id=r["rule_id"], subtype=None, reasoning=None, error=None,
            date=r["date"],
        )
        for r in stage1
    ]

    escalated_messages = sum(1 for r in stage1 if r["escalated"])
    print(
        f"[classify-pre] {len(emails)} emails | {escalated_messages} escalated"
        f" | {len(escalated)} conversation(s) to classify",
        file=sys.stderr,
    )
    json.dump({
        "run_id": run_id,
        "needs_verdict": bool(escalated),
        "escalated": escalated,
        "stage1": stage1,
        "prompt_fingerprint": prompt_fp,
        "provenance": collect(
            provider,
            Path(paths["rules"]), REPO_ROOT,
            Path(paths["learned"]), policy.anonymization_fields, corpus_path,
            # Carried from the fetch, never resolved here (STU-1123). This stage
            # reads a corpus somebody else bounded; on `run-batch` over a frozen
            # dump nobody bounded it today at all, and null says that rather than
            # claiming a floor the corpus was not read under.
            _fetched_since(ctx.get("start_date")),
            # Which mailbox those ids came out of (STU-1183), from the fetch stage
            # that read them; null when this corpus is a dump nobody fetched today.
            _fetched_from(ctx.get("mailbox")),
        ),
        "composition": composition.measure(emails, provisional),
        "paths": paths,
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
