#!/usr/bin/env python3
"""Register (or renew) Gmail push notifications to a Pub/Sub topic.

Run once at setup, then from cron at least weekly — Gmail expires a watch
after at most 7 days. On first run the returned historyId is written as the
webhook's baseline state (never overwritten afterwards: the webhook advances
it itself as notifications are processed).
This is a cron job with nobody at a browser, so the grant is taken
non-interactively and a lost one alerts rather than exiting into a log
(STU-952, the Graph twin's STU-930): consent here would bind a random port and
block forever on a redirect nobody produces, which is indistinguishable from a
cron run still working — and unrenewed, the watch expires within seven days and
push classification stops.
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from email_classifier import alerts
from email_classifier.fetch import gmail
from email_classifier.webhook import state
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--topic", default=os.environ.get("GMAIL_PUBSUB_TOPIC"),
        help="Pub/Sub topic (projects/<id>/topics/<name>); defaults to $GMAIL_PUBSUB_TOPIC.",
    )
    parser.add_argument(
        "--state-file", type=Path, default=REPO_ROOT / "state" / "gmail-history.json"
    )
    args = parser.parse_args(argv)

    if not args.topic:
        print("[ERROR] no topic: pass --topic or set GMAIL_PUBSUB_TOPIC", file=sys.stderr)
        return 1

    try:
        service = gmail.build_service(gmail.get_credentials(interactive=False))
        response = gmail.watch(service, args.topic)
    except gmail.GrantLost as exc:
        alerts.notify(
            "email-classifier: the Gmail grant is gone, so the push watch was not "
            "renewed — it expires within seven days and new mail stops being "
            f"classified. {exc}"
        )
        return 1

    if state.load_history_id(args.state_file) is None:
        state.save_history_id(args.state_file, response["historyId"])
        print(f"baseline historyId {response['historyId']} written to {args.state_file}")

    expires = datetime.fromtimestamp(int(response["expiration"]) / 1000, tz=timezone.utc)
    print(f"watch active until {expires.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
