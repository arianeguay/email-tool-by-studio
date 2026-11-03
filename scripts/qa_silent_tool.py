#!/usr/bin/env python3
"""Stand in for `$PYTHON` so a reviewer tool answers with nothing (STU-1114).

    EMAIL_CLASSIFIER_QA_REAL_PYTHON=.venv/bin/python \
    EMAIL_CLASSIFIER_QA_SILENCED=email_classifier.websearch \
      PYTHON=scripts/qa_silent_tool.py studio run draft-qa-replay ...

Every tool in `.studio/tools/` runs `"${PYTHON:-.venv/bin/python}" -m <module>`, so
pointing `PYTHON` here silences a tool without touching one production file: the agent,
the contracts, the pipeline and the tool YAMLs the measurement is about are the ones
production runs, and the arm differs only in what came back.

Anything else — a module not silenced, a script stage Studio launches by path — is
handed to the real interpreter unchanged. The dispatch is on `-m <module>` alone; the
shim never inspects a command it is not answering.

Exits 0 with the tool's own empty payload, never non-zero: an empty answer and an
unreachable tool are the two questions this repo keeps apart (STU-1139), and a shim
that failed instead of answering would be measuring the other one.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from email_classifier import deployment, qa_live_sources  # noqa: E402


def main(argv: list[str]) -> int:
    silenced = {
        name.strip()
        for name in os.environ.get(qa_live_sources.SILENCED_ENV, "").split(",")
        if name.strip()
    }
    if len(argv) >= 2 and argv[0] == "-m" and argv[1] in silenced:
        query = argv[argv.index("--query") + 1] if "--query" in argv else ""
        sources = deployment.ROOT / "rules" / "web_sources.yaml"
        json.dump(
            qa_live_sources.empty_answer(argv[1], query, sources),
            sys.stdout,
            ensure_ascii=False,
        )
        return 0

    real = os.environ.get(qa_live_sources.REAL_PYTHON_ENV)
    if not real:
        print(f"[qa-silent-tool] {qa_live_sources.REAL_PYTHON_ENV} is unset; nothing to "
              "hand the un-silenced command to", file=sys.stderr)
        return 2
    os.execv(real, [real, *argv])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
