#!/usr/bin/env python3
"""Fetch Outlook (Microsoft Graph, read-only) and dump EmailInput JSON (STU-389).

Usage:
    python scripts/fetch_outlook.py --query "from:noreply@example.org" \
        --max-results 50 --output dumps/outlook-dump.json

The query is a Graph `$search` string (KQL), not a Gmail query; with no query
the most recent messages are fetched. The app registration is read from
OUTLOOK_CLIENT_ID (see .env.example); the OAuth token cache defaults to
secrets/outlook-token.json (gitignored), overridable via OUTLOOK_TOKEN_FILE.
The first run opens a browser for one-time consent. A host that sets
EMAIL_CLASSIFIER_HEADLESS gets a GrantLost naming `make outlook-consent` instead
of waiting on a consent nobody is there to give.
"""

import argparse
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from email_classifier import providers
from email_classifier.core import schema
from email_classifier.fetch import window
from email_classifier.fetch.threads import expand_to_conversations
from email_classifier.stage1.policy import load_policy


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        default="",
        help="Graph $search query (e.g. 'from:noreply@example.org').",
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
        default=Path("dumps/outlook-dump.json"),
        help="Output path for the JSON array dump (default: dumps/outlook-dump.json).",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("rules/policy.yaml"),
        help="Policy file naming the run's date floor (default: rules/policy.yaml).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    fetcher = providers.get_fetcher("outlook")
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
