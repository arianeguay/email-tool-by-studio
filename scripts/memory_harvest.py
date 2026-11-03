"""Propose the blocks pasted across sent replies as club-memory facts, then approve them (STU-2001).

    scripts/memory_harvest.py harvest --since 2026-09-01
    scripts/memory_harvest.py approve

`harvest` reads Sent Items since a day and writes the blocks that are new, or whose text
moved since the last decision, to `club-memory/harvest-candidates.json`. Set `approve` on
every entry to true or false (edit `fact` and `description` freely; `name` is how the
next harvest recognizes the block), then `approve` writes the approved ones into the store.
"""

import argparse
from datetime import date
from pathlib import Path

from email_classifier import club_memory, memory_harvest
from email_classifier.providers import get_sent_reader
from email_classifier.stage1.policy import load_policy

POLICY = club_memory.REPO_ROOT / "rules/policy.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("harvest", "approve"))
    parser.add_argument("--since", type=date.fromisoformat,
                        help="first day of Sent Items to read (harvest; required)")
    parser.add_argument("--directory", type=Path, default=club_memory.MEMORY_DIR)
    parser.add_argument("--policy", type=Path, default=POLICY)
    args = parser.parse_args(argv)

    if args.command == "approve":
        try:
            written = memory_harvest.approve(args.directory)
        except ValueError as exc:
            parser.exit(1, f"[ERROR] {exc}\n")
        print(f"{len(written)} fact(s) written to {args.directory}: {', '.join(written) or '-'}")
        return 0

    if args.since is None:
        parser.error("harvest needs --since: no default reaches back a sensible distance")
    replies = get_sent_reader(load_policy(args.policy).email_provider).sent_since(
        f"{args.since.isoformat()}T00:00:00+00:00")
    proposed = memory_harvest.harvest(replies, args.directory)
    print(f"{len(replies)} sent replies since {args.since}, "
          f"{len(proposed)} candidate(s) in {args.directory / memory_harvest.CANDIDATES_NAME}")
    for candidate in proposed:
        print(f"  {candidate['status']:8} {candidate['name']}  x{candidate['replies']} "
              f"{candidate['first_sent']} to {candidate['last_sent']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
