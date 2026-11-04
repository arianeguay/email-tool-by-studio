#!/usr/bin/env python3
"""Fetch Gmail (read-only) and dump EmailInput JSON conforming to STU-389.

Usage:
    python scripts/fetch_gmail.py --query "label:promotions after:2026/06/01" \
        --max-results 50 --output dumps/gmail-dump.json

Credentials default to secrets/credentials.json and secrets/token.json
(gitignored), overridable via GMAIL_CREDENTIALS_FILE / GMAIL_TOKEN_FILE.
The first run opens a browser for one-time consent. A host that sets
EMAIL_CLASSIFIER_HEADLESS gets a GrantLost naming `make gmail-consent` instead of
a consent window nobody will see.
"""

import argparse
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from email_classifier import deployment, providers
from email_classifier.core import schema
from email_classifier.fetch import window
from email_classifier.fetch.threads import expand_to_conversations
from email_classifier.stage1.policy import load_policy


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        default="",
        help="Gmail search query (e.g. 'label:promotions after:2026/06/01').",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=100,
        help="Maximum number of messages to fetch (default: 100).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dumps/gmail-dump.json"),
        help="Output path for the JSON array dump (default: dumps/gmail-dump.json).",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=deployment.ROOT / "rules" / "policy.yaml",
        help="Policy file naming the run's date floor (default: the deployment's "
             "rules/policy.yaml, under $EMAIL_CLASSIFIER_DEPLOYMENT_ROOT).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    fetcher = providers.get_fetcher("gmail")
    policy = load_policy(args.policy)
    tz = ZoneInfo(policy.timezone)
    start_date = window.resolve(policy.start_date, tz)
    inputs = expand_to_conversations(
        fetcher,
        fetcher.fetch_inputs(
            query=args.query, max_results=args.max_results,
            since=window.since_iso(start_date, tz),
        ),
        start_date=start_date,
        tz=tz,
    )

    # Fail loudly before writing if any projected object breaks the schema.
    for item in inputs:
        schema.validate(item)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Wrote {len(inputs)} emails since {start_date.isoformat()} to "
          f"{args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
