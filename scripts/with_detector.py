#!/usr/bin/env python3
"""Run a batch command under one shared name detector (STU-1020).

The webhook holds a detector for the processes a notification spawns (STU-938);
the make targets spawn the same processes and reached none of it, because they are
not children of the webhook. Three of the four entry points are `studio run` — a
Node process — so the context manager cannot live "in each script": it lives here,
and the targets prefix it.

What it buys differs per target. `mailbox-full` and `redraft` fan out to a
`org_memory-read` and a `mail_precedents-search_mail` per reply target, each a
fresh ~25 s load on the homelab; `run` and `run-batch` tokenize in a single process
already, so wrapping them only moves that one load in here. They are wrapped anyway:
"a batch entry point holds one detector" is a rule, and naming which targets fan out
today is that rule plus an exception list that goes stale the first time an agent
gains a tool.

Usage: scripts/with_detector.py <command> [args...]
"""

import subprocess
import sys

from email_classifier.detector import Detector


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: with_detector.py <command> [args...]", file=sys.stderr)
        return 2
    with Detector():
        return subprocess.call(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
