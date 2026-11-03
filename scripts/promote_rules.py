#!/usr/bin/env python
"""Promote uniform LLM verdicts into deterministic rules (STU-822).

    python scripts/promote_rules.py [--dry-run]
    python scripts/promote_rules.py --revoke <address>

Offline job — never run from the webhook request path: it rewrites the rules file the
serving process reads, and the webhook only picks up the result on its next mtime check.

`--revoke` blacklists a sender and rewrites the layer without it; the blacklist is
permanent, so a revoked sender cannot be re-promoted by a later run.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from email_classifier.learn.promote import (
    evaluate,
    load_blacklist,
    load_learned,
    render,
    rule_id,
    save_blacklist,
)
from email_classifier.learn.store import load_observations
from email_classifier.stage1.rule_engine import load_rules


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("logs/verdicts.jsonl"))
    parser.add_argument("--rules", type=Path, default=Path("rules/rules.yaml"))
    parser.add_argument("--learned", type=Path, default=Path("rules/learned.yaml"))
    parser.add_argument("--blacklist", type=Path, default=Path("rules/learned_blacklist.yaml"))
    parser.add_argument("--revoke", metavar="ADDRESS",
                        help="Blacklist a sender and drop its learned rule.")
    parser.add_argument("--dry-run", action="store_true", help="Report only, write nothing.")
    args = parser.parse_args()

    blacklist = load_blacklist(args.blacklist)
    existing = load_learned(args.learned)
    if args.revoke:
        address = args.revoke.lower()
        blacklist.add(address)
        kept = [r for r in existing if r.from_address != address]
        if not args.dry_run:
            save_blacklist(args.blacklist, blacklist)
            if len(kept) != len(existing):
                args.learned.write_text(render(kept), encoding="utf-8")
        existing = kept
        print(f"revoked: {address}")

    observations = load_observations(args.store)
    if not observations:
        print(f"no observations in {args.store} — nothing to promote")
        return 0

    hand_written = load_rules(args.rules)
    decisions = evaluate(
        observations,
        hand_written,
        blacklist,
        promoted_at=datetime.now(timezone.utc).isoformat(),
        previously_promoted={rule_id(r.from_address): r.promoted_at for r in existing},
    )

    promoted = [d.rule for d in decisions if d.rule is not None]
    for d in sorted(decisions, key=lambda d: d.from_address):
        if d.rule is None:
            print(f"  skip    {d.from_address}: {d.reason}")
        else:
            print(f"  PROMOTE {d.from_address}: {d.rule.bucket}/{d.rule.action} "
                  f"({d.rule.sample_size} messages, {len(d.rule.run_ids)} runs)")

    print(f"\n{len(promoted)} promoted / {len(decisions)} sender(s) observed")
    if args.dry_run:
        print("(dry run — nothing written)")
        return 0

    args.learned.parent.mkdir(parents=True, exist_ok=True)
    args.learned.write_text(render(promoted), encoding="utf-8")
    print(f"wrote {args.learned}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
