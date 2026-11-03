#!/usr/bin/env python3
"""The commitments drafted replies made that nobody has settled yet (STU-1065).

    python scripts/commitments.py                         # list the open ones
    python scripts/commitments.py settle <id> --note '...' # a human did it, or decided not to

Reads `logs/drafts/commitments.jsonl`, which `write-draft` appends to. An entry closes
only through `settle`, and the note is mandatory: a settled row that says nothing is the
same silence as no row.
"""

import argparse
import sys
from pathlib import Path

from email_classifier import commitments
from email_classifier.orchestrate.drafts import DEFAULT_PATHS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path,
                        default=commitments.path_beside(Path(DEFAULT_PATHS["draft_journal"])))
    sub = parser.add_subparsers(dest="command")
    settle = sub.add_parser("settle")
    settle.add_argument("id")
    settle.add_argument("--note", required=True)
    args = parser.parse_args(argv)

    if args.command == "settle":
        if not args.note.strip():
            print("[ERROR] --note must say what was done", file=sys.stderr)
            return 1
        try:
            commitments.settle(args.file, args.id, args.note.strip())
        except KeyError as exc:
            print(f"[ERROR] {exc.args[0]}", file=sys.stderr)
            return 1
        return 0

    entries = commitments.unsettled(args.file)
    for e in entries:
        print(f"{e['id']}  {e['timestamp'][:16]}  thread {e['thread_id']}  run {e['run_id']}\n"
              f"    {e['obliges']}: {e['sentence']}")
    print(f"{len(entries)} unsettled commitment(s) in {args.file}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
