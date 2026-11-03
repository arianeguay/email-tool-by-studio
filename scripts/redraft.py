#!/usr/bin/env python3
"""Redraft one conversation: delete the draft it holds, prepare a fresh one.

Step one of the two `make redraft LINK=...` runs. This script clears the
mailbox and writes the fan-out item; the Makefile then runs `email-draft` on it
— the same leaf pipeline the drafts fan-out runs once per email, so the
draft/QA group and the stage that writes to the mailbox are the ones already in
use.

Deleting rather than letting `upsert_reply_draft` rewrite the body: what is
wrong with a draft worth redoing is often what `createReply` put around it — the
recipient, the quoted parent, an edit that no longer applies — and a rewrite
keeps all of that. The cost is real: a drafting run that then dies leaves the
conversation with no draft at all. That is the trade a human asking for a
redraft has already made.

The deletion is also what makes the mail eligible again: `drafts-pre` steps over
a conversation that already carries a draft (STU-884).

The target is named by an Outlook web link, the only handle on a conversation a
human has. Outlook-only, like every other drafting entry point.

Usage: scripts/redraft.py <link|id> [--out logs/redraft.input.yaml]
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from email_classifier import club_memory, mail_link
from email_classifier.anonymize import Tokenizer, load_keymap, save_keymap
from email_classifier.apply.draft_journal import record
from email_classifier.core.types import EmailInput
from email_classifier.orchestrate import drafts
from email_classifier.orchestrate.provenance import resolved_provider, unusable_provider
from email_classifier.providers import (
    get_body_fetcher,
    get_drafter,
    get_reference_resolver,
    get_thread_reader,
    preflight_write,
)
from email_classifier.stage1.policy import load_policy
from email_classifier import deployment

REPO_ROOT = deployment.ROOT
STUDIO_DIR = REPO_ROOT / ".studio"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("link", help="Outlook web link to the conversation, or a bare id")
    parser.add_argument("--out", default="logs/redraft.input.yaml")
    parser.add_argument("--policy", default=str(REPO_ROOT / "rules/policy.yaml"))
    # The same flag the Makefile hands the `email-draft` run below, so the provider this
    # checks is the provider that run will use (STU-1128).
    parser.add_argument("--provider", default=None)
    args = parser.parse_args(argv)

    policy = load_policy(Path(args.policy))
    refusal = preflight_write(policy.email_provider)
    if refusal:
        raise SystemExit(f"[ERROR] {refusal}")
    # Before the delete, not after: this script clears the mailbox and the drafting run
    # is a separate process, so a provider that cannot initialize would leave the
    # conversation with no draft at all — the cost this file already names as the trade
    # a human asking for a redraft has made, paid here for nothing.
    unusable = unusable_provider(
        STUDIO_DIR, resolved_provider(STUDIO_DIR, args.provider, "drafter"))
    if unusable:
        raise SystemExit(f"[ERROR] {unusable}")

    paths = {key: str(REPO_ROOT / default) for key, default in drafts.DEFAULT_PATHS.items()}
    resolver = get_reference_resolver(policy.email_provider)
    email = EmailInput(**resolver.fetch_input_by_reference(mail_link.message_ref(args.link)))

    drafter = get_drafter(policy.email_provider)
    draft_id = drafter.find_draft(email.thread_id)
    if draft_id:
        drafter.delete_draft(draft_id)
        record(Path(paths["draft_journal"]), None, email.message_id, email.thread_id,
               "deleted", draft_id)
        print(f"[redraft] deleted draft {draft_id}", file=sys.stderr)
    else:
        print("[redraft] no draft on that conversation — drafting from scratch",
              file=sys.stderr)

    bodies = get_body_fetcher(policy.email_provider).fetch_bodies_by_ids([email.message_id])
    history = get_thread_reader(policy.email_provider).fetch_thread_history(
        email.thread_id, email.message_id)
    keymap_path = Path(paths["keymap"])
    tokenizer = Tokenizer(load_keymap(keymap_path) if policy.anonymization_fields else None)
    item = drafts.draft_item(
        email,
        bodies.get(email.message_id, ""),
        history,
        tokenizer,
        policy.anonymization_fields,
        drafts.prompt_fingerprint(STUDIO_DIR, policy.anonymization_fields),
        paths,
        club_memory.load_index(club_memory.MEMORY_DIR),
        "",  # no run: a redraft answers a human naming one conversation
        drafts.period_at(datetime.now(timezone.utc), ZoneInfo(policy.timezone)),
    )
    if policy.anonymization_fields:
        save_keymap(keymap_path, tokenizer.keymap)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(item, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    print(f"[redraft] {email.subject or '(no subject)'} -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
