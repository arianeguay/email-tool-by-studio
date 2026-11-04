#!/usr/bin/env python3
"""Pre-stage of `calendar` (STU-2089): pick the appointment mail, build the fan-out items.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

A policy with no `calendar:` block hands the fan-out nothing, so the child is a no-op.
Otherwise the report rows whose subtype the block lists, minus the message ids
`logs/calendar/events.jsonl` or `logs/calendar/review.jsonl` already hold, are tokenized
with the batch keymap like `drafts-pre` does, so a person keeps the token the
classification stage gave them.

Input:  { "corpus": "<dir|json>", "report": "<report path>", ...path overrides }
Output: { "run_id", "enabled", "needs_extract", "selected", "already_handled",
          "pending", "paths" }
"""

import json
import sys
from pathlib import Path

import yaml

from email_classifier import calendar_plan, deployment
from email_classifier.anonymize import (
    Tokenizer, anonymize_fields, load_keymap, record_minted, record_sent,
    run_keymap_path, save_keymap, sent_path,
)
from email_classifier.core.contract import load_taxonomy
from email_classifier.eval.harness import load_corpus
from email_classifier.orchestrate.pipeline import dedupe_by_message
from email_classifier.providers import can_draft, get_body_fetcher
from email_classifier.stage1.policy import load_policy

REPO_ROOT = deployment.ROOT
_ITEM_FIELDS = ("message_id", "thread_id", "from_address", "subject", "date")


def _paths(ctx: dict) -> dict[str, str]:
    return {key: str(REPO_ROOT / ctx.get(key, default))
            for key, default in calendar_plan.DEFAULT_PATHS.items()}


def _items(ids, emails, policy, paths, run_id) -> list[dict]:
    # A Gmail snippet stops at 255 characters, often before the date; a provider that
    # reads whole bodies is asked for them.
    bodies = (get_body_fetcher(policy.email_provider).fetch_bodies_by_ids(ids)
              if ids and can_draft(policy.email_provider) else {})
    keymap_path = Path(paths["keymap"])
    tokenizer = Tokenizer(load_keymap(keymap_path) if policy.anonymization_fields else None)
    items = []
    for message_id in ids:
        email = emails[message_id]
        item = {field: getattr(email, field) for field in _ITEM_FIELDS}
        item["body_text"] = bodies.get(message_id) or email.body_snippet
        anonymize_fields(item, tokenizer, policy.anonymization_fields,
                         from_address=email.from_address)
        items.append(item)
    if policy.anonymization_fields and items:
        save_keymap(keymap_path, tokenizer.keymap)
        record_minted(run_keymap_path(keymap_path, run_id), tokenizer.minted())
        record_sent(sent_path(keymap_path, run_id), items)
    return items


def main() -> None:
    payload = json.load(sys.stdin)
    ctx = yaml.safe_load(payload.get("additional_context", "") or "") or {}
    for required in ("corpus", "report"):
        if required not in ctx:
            raise SystemExit(f"[ERROR] calendar input needs a `{required}`")

    paths = _paths(ctx)
    policy = load_policy(Path(paths["policy"]))
    report = json.loads((REPO_ROOT / ctx["report"]).read_text(encoding="utf-8"))
    calendar = policy.calendar
    pending: list[dict] = []
    selected = handled_count = 0
    if calendar is not None:
        unknown = set(calendar.subtypes) - set(load_taxonomy(Path(paths["taxonomy"])).subtypes)
        if unknown:
            raise SystemExit(
                f"[ERROR] calendar.subtypes names {sorted(unknown)}, which the taxonomy "
                "does not have: those rows can never be selected")
        handled = calendar_plan.handled_message_ids(
            calendar_plan.read_rows(Path(paths["events"])),
            calendar_plan.read_rows(Path(paths["review"])))
        every = calendar_plan.targets(report, calendar.subtypes, set())
        ids = [message_id for message_id in every if message_id not in handled]
        selected, handled_count = len(every), len(every) - len(ids)
        emails = {e.message_id: e
                  for e in dedupe_by_message(load_corpus(REPO_ROOT / ctx["corpus"]))}
        missing = [message_id for message_id in ids if message_id not in emails]
        if missing:
            raise SystemExit(
                f"[ERROR] {len(missing)} message(s) to read are absent from the corpus: "
                f"{', '.join(missing[:5])}")
        pending = _items(ids, emails, policy, paths, report["run_id"])
        print(f"[calendar-pre] {len(pending)} to read, {handled_count} already handled",
              file=sys.stderr)
    else:
        print("[calendar-pre] no calendar block in the policy: nothing to read",
              file=sys.stderr)

    json.dump({
        "run_id": report["run_id"],
        "enabled": calendar is not None,
        "needs_extract": bool(pending),
        "selected": selected,
        "already_handled": handled_count,
        "pending": pending,
        "paths": paths,
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
