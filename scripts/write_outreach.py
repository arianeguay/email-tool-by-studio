#!/usr/bin/env python3
"""Write stage of `outreach-draft` (STU-2121): put a new outgoing message in Drafts.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

The agent composes, the run writes, as in `write_draft.py` (STU-1030). The draft is
keyed on the item's `key`, never on anything the model wrote, so a rerun on the same
key finds the draft it already left. Only IMAP can draft a new message today.
"""

import json
import sys
from pathlib import Path

import yaml

from email_classifier.providers import get_drafter
from email_classifier.stage1.policy import load_policy

_COMPOSE_STAGE = "compose-outreach"


def main() -> None:
    payload = json.load(sys.stdin)
    item = yaml.safe_load(payload.get("additional_context", "") or "") or {}
    draft = (payload.get("previous_outputs") or {})[_COMPOSE_STAGE]

    body = (draft.get("draft_body") or "").strip()
    if not body:
        sys.exit("[write-outreach] refusing to create an empty draft")
    provider = load_policy(Path(item["policy"])).email_provider
    if provider != "imap":
        sys.exit(f"[write-outreach] no new-message drafts on {provider!r}, only on imap")

    result = get_drafter(provider).create_draft(
        str(item["key"]), (draft.get("to") or "").strip() or None,
        draft["subject"].strip(), body)
    print(f"[write-outreach] {item['key']} -> {result['draft_id']}"
          f"{'' if result['created'] else ' (already there)'}", file=sys.stderr)
    json.dump({**draft, "key": item["key"], **result}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
