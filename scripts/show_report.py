#!/usr/bin/env python3
"""Print a report's human summary (STU-843).

`studio run` captures a script stage's stderr, so classify-post's summary never reaches
the terminal. Defaults to the newest report, the same convention as the Makefile's
RUN_ID.
"""

import json
import sys
from pathlib import Path

from email_classifier.orchestrate.report import EmailVerdict, Report, render_human
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        reports = sorted((REPO_ROOT / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not reports:
            raise SystemExit("[ERROR] no report in reports/")
        path = reports[-1]

    data = json.loads(path.read_text(encoding="utf-8"))
    print(render_human(Report(
        run_id=data["run_id"],
        totals=data["totals"],
        ratio_deterministic=data["ratio_deterministic"],
        by_bucket=data["by_bucket"],
        by_action=data["by_action"],
        verdicts=[EmailVerdict(**v) for v in data["verdicts"]],
        provenance=data["provenance"],
        composition=data["composition"],
    )))


if __name__ == "__main__":
    main()
