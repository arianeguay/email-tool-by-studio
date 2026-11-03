#!/usr/bin/env python3
"""Prune the run artifacts nothing points at any more (STU-932).

    python scripts/prune_artifacts.py            # plan only — nothing is removed
    python scripts/prune_artifacts.py --apply    # the cron line
    python scripts/prune_artifacts.py -v         # every file, kept ones included

`reports/`, `dumps/`, `logs/applied/` and `dumps/web-cache/` grow once per run and
were never trimmed. Each has an age; `audits/audit_log.json` and `audits/skipped.json`
decide what no age reaches — `email_classifier/retention.py` states those rules.

Dry-run by default, like every other command here that mutates something: the cron
entry is the one that says `--apply`. It refuses outright when the audit log is
missing, because with no log every artifact reads as unprotected and the prune would
take the audited runs with the rest.
"""

import argparse
import json
import sys
import time
from pathlib import Path

KERNEL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL_ROOT))

from email_classifier.retention import SHELVES, apply_plan, plan
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                        help="remove the expired files; without it nothing is touched")
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--audit-log", type=Path, default=None,
                        help="defaults to <root>/audits/audit_log.json")
    parser.add_argument("--skipped", type=Path, default=None,
                        help="defaults to <root>/audits/skipped.json")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="list every file and the reason it is kept or removed")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print nothing when there is nothing to remove (cron)")
    args = parser.parse_args(argv)

    audit_log = args.audit_log or args.root / "audits" / "audit_log.json"
    if not audit_log.exists():
        print(f"[ERROR] no audit log at {audit_log} — refusing to prune, every run "
              "would read as unprotected", file=sys.stderr)
        return 2

    skipped = args.skipped or args.root / "audits" / "skipped.json"
    decisions = plan(args.root, now=time.time(), audit_entries=_load(audit_log),
                     skipped_entries=_load(skipped))
    doomed = [d for d in decisions if d.remove]
    if not doomed and args.quiet:
        return 0

    for shelf in SHELVES:
        here = [d for d in decisions if d.path.parent == args.root / shelf.path]
        going = [d for d in here if d.remove]
        print(f"{shelf.path:<20} {len(going):>4} to remove "
              f"({_size(sum(d.size for d in going))}), {len(here) - len(going):>4} kept")
        if args.verbose:
            for d in here:
                verb = "REMOVE" if d.remove else "keep  "
                print(f"    {verb} {d.path.name}  ({d.reason})")

    total = _size(sum(d.size for d in doomed))
    if not args.apply:
        print(f"\nplan only — {len(doomed)} file(s), {total}; pass --apply to remove them")
        return 0

    removed, freed = apply_plan(decisions)
    print(f"\nremoved {removed} file(s), freed {_size(freed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
