#!/usr/bin/env python3
"""Create (or renew) the Outlook Graph change-notification subscription.

Run once at setup (the webhook must already be up: creation triggers the
validationToken handshake), then from cron daily — Graph caps a mail
subscription's lifetime at ~3 days. The subscription id and its expiry are kept
in state/outlook-subscription.json, where /healthz reads them; a vanished
subscription (404 on renew) is recreated transparently.
This is the cron that meets a grant killed by 90 days of inactivity, so a lost
grant alerts here rather than exiting into a log (STU-930): unrenewed, the
subscription expires within three days and delivery stops silently.
"""

import argparse
import os
import sys
from pathlib import Path

from email_classifier import alerts
from email_classifier.fetch import outlook
from email_classifier.webhook import state
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def _is_gone(exc: Exception) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notification-url", default=os.environ.get("GRAPH_NOTIFICATION_URL"),
        help="Public URL of the /graph endpoint; defaults to $GRAPH_NOTIFICATION_URL.",
    )
    parser.add_argument(
        "--state-file", type=Path, default=REPO_ROOT / "state" / "outlook-subscription.json"
    )
    parser.add_argument(
        "--recreate", action="store_true",
        help="Delete the stored subscription and create a new one. Graph accepts only "
             "expirationDateTime and notificationUrl on a PATCH, so a rotated "
             "GRAPH_CLIENT_STATE reaches the subscription no other way.",
    )
    args = parser.parse_args(argv)

    client_state = os.environ.get("GRAPH_CLIENT_STATE")
    if not client_state:
        print("[ERROR] GRAPH_CLIENT_STATE is required (random secret shared with the webhook)",
              file=sys.stderr)
        return 1
    if not args.notification_url:
        print("[ERROR] no notification URL: pass --notification-url or set GRAPH_NOTIFICATION_URL",
              file=sys.stderr)
        return 1

    try:
        response = _renew(args, client_state)
    except outlook.GrantLost as exc:
        alerts.notify(
            "email-classifier: the Outlook Graph grant is gone, so the change "
            "subscription was not renewed — it expires within three days and new "
            f"mail stops being classified. {exc}"
        )
        return 1
    except Exception as exc:
        alerts.notify(
            "email-classifier: renewing the Outlook Graph subscription failed, so it "
            "is not renewed — it expires within three days and new mail stops being "
            f"classified. {type(exc).__name__}: {exc}"
        )
        return 1

    # Rewritten on renew too, not only on creation: /healthz reports this expiry, and the
    # creation-time one reads as long expired on a subscription cron keeps alive (STU-931).
    state.record_subscription(args.state_file, response)

    print(f"subscription active until {response['expirationDateTime']}")
    return 0


def _renew(args, client_state: str) -> dict:
    """Renew the stored subscription, creating one when there is none to renew."""
    session = outlook.build_session(interactive=False)

    subscription_id = state.load_subscription(args.state_file).get("subscription_id")

    if subscription_id and args.recreate:
        # Deleted rather than left to expire: until it does, it keeps notifying with
        # the client state being rotated away from, and every one of those is dropped.
        print(f"deleting subscription {subscription_id} to recreate it", file=sys.stderr)
        outlook.delete_subscription(session, subscription_id)
        subscription_id = None

    if subscription_id:
        try:
            response = outlook.renew_subscription(session, subscription_id)
        except Exception as exc:
            if not _is_gone(exc):
                raise
            print(f"subscription {subscription_id} gone; recreating", file=sys.stderr)
        else:
            if response.get("lifecycleNotificationUrl"):
                return response
            # A subscription predating STU-937 has no lifecycle URL, and Graph does
            # not let a PATCH add one — so this cron is where it gets replaced.
            # Deleted first: two live subscriptions notify twice for every message.
            print(f"subscription {subscription_id} has no lifecycle URL; replacing it",
                  file=sys.stderr)
            outlook.delete_subscription(session, subscription_id)

    response = outlook.subscribe(session, args.notification_url, client_state)
    print(f"subscription {response['id']} written to {args.state_file}")
    return response


if __name__ == "__main__":
    raise SystemExit(main())
