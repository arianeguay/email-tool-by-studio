#!/usr/bin/env python3
"""Stand-in for `write-draft` in the drafting bake-off (STU-1073).

Script executor interface: reads JSON from stdin, writes JSON to stdout.

Production's reviewer reads the mailbox write's output, so what it judges carries a
`draft_id`. Here nothing is written — a bake-off authors each conversation once per arm
per repeat, and every one of those would land in the mailbox and in the drafts journal —
so the id names the bake-off's own attempt instead. The reviewer never reads it; the
shape of its `previous_stage_output` is what has to match, so that the prompt being
scored is the prompt production gives.
"""

import json
import sys
import uuid

_COMPOSE_STAGE = "generate-draft"


def main() -> None:
    payload = json.load(sys.stdin)
    draft = (payload.get("previous_outputs") or {})[_COMPOSE_STAGE]
    print(f"[seal-draft] {draft['thread_id']}", file=sys.stderr)
    json.dump({**draft, "draft_id": f"bakeoff-{uuid.uuid4()}"}, sys.stdout,
              ensure_ascii=False)


if __name__ == "__main__":
    main()
