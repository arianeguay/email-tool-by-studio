#!/usr/bin/env python3
"""Echo stage of `draft-qa-replay` (STU-1014): hand the reviewer a recorded draft.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

The draft arrives by env var rather than inside the input file so that the reviewer's
`input` block is exactly the fan-out item `email-draft` gives it. A judge scored on a
prompt the production judge never sees measures the harness.

Env: EMAIL_CLASSIFIER_QA_REPLAY_DRAFT — path to a recorded `generate-draft` output.
"""

import json
import os
import sys
from pathlib import Path

_DRAFT_ENV = "EMAIL_CLASSIFIER_QA_REPLAY_DRAFT"


def main() -> None:
    sys.stdin.read()
    path = os.environ.get(_DRAFT_ENV)
    if not path:
        sys.exit(f"[qa-replay-draft] {_DRAFT_ENV} is unset — no draft to judge")
    draft = json.loads(Path(path).read_text(encoding="utf-8"))
    print(f"[qa-replay-draft] {draft['thread_id']}", file=sys.stderr)
    json.dump(draft, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
