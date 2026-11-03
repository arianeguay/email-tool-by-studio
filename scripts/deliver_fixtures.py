#!/usr/bin/env python3
"""Deliver fixtures to the Outlook test mailbox as real inbound mail (STU-934).

    python scripts/deliver_fixtures.py deliver --count 5
    python scripts/deliver_fixtures.py compare

`deliver` sends the fixtures over SMTP from an outside account, waits for them to
land, categorizes them so `make unseed` sweeps them like a seeded round, and
freezes the read-back as a corpus dump. `compare` diffs that dump against the
seeded one, naming every field the two delivery paths disagree on.

The sending account comes from `DELIVER_SMTP_HOST`, `DELIVER_SMTP_PORT`,
`DELIVER_SMTP_USER`, `DELIVER_SMTP_PASSWORD` and `DELIVER_FROM`; the recipient
from `OUTLOOK_SEED_MAILBOX`, the same declaration the seeder refuses to write
without.

Delivery is real mail leaving a real account. The corpus is synthetic by
construction — no real address, subject or body is read or reproduced — but the
messages do cross the public internet, unlike a seeded round which never leaves
the tenant.
"""

import argparse
import json
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from email_classifier.core import schema
from email_classifier.core.types import EmailInput
from email_classifier.fetch.outlook import OutlookFetcher
from email_classifier.seed import corpus, deliver, outlook
from email_classifier.webhook import address, tunnel
from email_classifier import deployment

REPO_ROOT = deployment.ROOT
DELIVERED_DUMP = REPO_ROOT / "dumps" / "outlook-delivered.json"
SEEDED_DUMP = REPO_ROOT / "dumps" / "outlook-seeded.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    send = commands.add_parser("deliver", help="Send fixtures as real inbound mail.")
    send.add_argument("--count", type=int, default=None,
                      help="Messages to deliver (default: the whole corpus).")
    send.add_argument("--mix", default="",
                      help="Bucket weights, e.g. 'bucket_a=40,bucket_c=60'. "
                           "Default: the corpus's own proportions.")
    send.add_argument("--corpus", type=Path, default=REPO_ROOT / "fixtures",
                      help="Corpus to draw from (default: the deployment's fixtures/).")
    send.add_argument("--threads", action="store_true",
                      help="Read --corpus as conversations and deliver each one as a "
                           "reply chain, instead of drawing a bucket mix of independent "
                           "messages. --count limits the conversations, not the "
                           "messages; --mix does not apply.")
    send.add_argument("--dump", type=Path, default=DELIVERED_DUMP,
                      help="Where to freeze the read-back as a corpus.")
    send.add_argument("--random-seed", type=int, default=0,
                      help="Draw seed — same value draws the same mix (default: 0).")
    send.add_argument("--timeout", type=float, default=300.0,
                      help="Seconds to wait for the round to land (default: 300).")
    send.add_argument("--interval", type=float, default=10.0,
                      help="Seconds between Inbox polls (default: 10).")

    compare = commands.add_parser(
        "compare", help="Diff a delivered round against its seeded counterpart.")
    compare.add_argument("--delivered", type=Path, default=DELIVERED_DUMP)
    compare.add_argument("--seeded", type=Path, default=SEEDED_DUMP)
    return parser.parse_args(argv)


def write_dump(path: Path, inputs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")


def _health(url: str) -> dict | None:
    """What the webhook at `url` says about itself, or None when none answers.

    Through a forward when `WEBHOOK_SSH_HOST` names one (STU-1027): the staging
    webhook answers only on its own loopback, so from the machine a round is sent
    from `url` is not dialable and every round reported the unreachable case.
    """
    try:
        with tunnel.reachable(url) as dialable:
            with urllib.request.urlopen(dialable, timeout=10.0) as response:
                return json.load(response)
    except tunnel.ForwardFailed as exc:
        # Named rather than folded into the line below, which says the webhook did
        # not answer — here it was never asked.
        print(f"  [WARN] {exc}", file=sys.stderr)
        return None
    except (OSError, ValueError):
        return None


def _stamp(health: dict | None) -> datetime | None:
    at = (health or {}).get("last_notification_at")
    return datetime.fromisoformat(at) if at else None


def _report_notification(url: str, before: dict | None) -> None:
    """Whether the webhook logged a notification while the round was in flight.

    The liveness stamp is the only trace a processed notification leaves — the work
    queue drops its item once the run is done (STU-927) — so it is asked of the
    webhook over /healthz, never read off this checkout (STU-1019). The host that
    receives the notifications is the one that stamps them, and the staging webhook
    is on another: the local file never moves, so the warning below fired on all
    four rounds of STU-1000 while all 25 notifications had in fact arrived.
    """
    after = _health(url)
    if after is None:
        print(f"  notification: no webhook answering at {url} — none running, or it "
              "serves at an address this environment does not name")
        return
    previous, current = _stamp(before), _stamp(after)
    if current is None:
        print("  notification: the webhook is running and has received none, ever")
    elif previous is None or current > previous:
        print(f"  notification: webhook received one at {current.isoformat()}")
    else:
        print(f"  [WARN] notification: liveness unchanged since {current.isoformat()} — "
              "the delivery produced no notification the webhook saw", file=sys.stderr)


def _draw(args: argparse.Namespace) -> list[list[EmailInput]]:
    """The round to send, as the chains it is sent in.

    A flat round is one chain per message — nothing to thread. A thread round
    keeps each conversation whole, in arrival order, because that order is what
    the reply headers are built along.
    """
    if args.threads:
        if args.mix:
            raise SystemExit("--mix does not apply to --threads: a conversation is "
                             "delivered whole, so there is nothing to weight.")
        return corpus.load_threads(args.corpus)[: args.count]

    buckets = corpus.load_buckets(args.corpus)
    count = args.count if args.count is not None else sum(map(len, buckets.values()))
    drawn = corpus.select(buckets, count, corpus.parse_mix(args.mix), seed=args.random_seed)
    return [[email] for _, email in drawn]


def _report_threading(chains: list[list[EmailInput]], landed: dict[str, str]) -> int:
    """Name the fixture conversations delivery failed to land as one conversation.

    The grouping is read off `thread_id` in the ordinary read-back, not off the
    headers that were sent: what is under test is the receiving store's threading,
    and a chain the relay stripped or the store ignored looks identical on the
    way out.
    """
    split = 0
    for chain in chains:
        thread_ids = {landed[m.message_id] for m in chain if m.message_id in landed}
        if len(thread_ids) > 1:
            split += 1
            print(f"  [WARN] {chain[0].thread_id}: landed across {len(thread_ids)} "
                  "conversations — the store did not thread the delivered chain",
                  file=sys.stderr)
    if not split:
        print(f"  threading: each of the {len(chains)} conversation(s) landed under one "
              "conversation id")
    return split


def _deliver(args: argparse.Namespace) -> int:
    chains = _draw(args)
    drawn = [email for chain in chains for email in chain]

    mailbox = outlook.assert_test_mailbox()  # raises loudly on the wrong mailbox
    config = deliver.SmtpConfig.from_env()
    deliver.assert_outside_account(config, mailbox)
    session = outlook.build_session()

    run_id = str(uuid.uuid4())
    health_url = address.configured_health_url()
    before = _health(health_url)
    sent_at = datetime.now(timezone.utc)
    with deliver.relay(config) as submit:
        unwaited = [
            fixture
            for chain in chains
            for fixture in deliver.send_chain(
                submit, session, chain, run_id, config.sender, mailbox,
                timeout=args.timeout, interval=args.interval,
            )
        ]
    print(f"Sent {len(drawn)} message(s) from {config.sender} to {mailbox} "
          f"(delivery run {run_id})")
    if args.threads:
        print(f"  chained into {len(chains)} conversation(s)")
    for fixture in unwaited:
        print(f"  [WARN] {fixture}: never landed, so its reply was sent into an empty "
              "conversation", file=sys.stderr)

    found, waited = deliver.await_arrival(
        session, run_id, len(drawn), timeout=args.timeout, interval=args.interval
    )
    print(f"  arrived: {len(found)}/{len(drawn)} after {waited:.0f}s "
          f"(sent {sent_at.isoformat()})")
    _report_notification(health_url, before)

    message_ids = list(found.values())
    deliver.categorize(session, message_ids)
    fetched = OutlookFetcher(session).fetch_inputs_by_ids(message_ids)
    write_dump(args.dump, fetched)
    print(f"  corpus dump:   {args.dump}")

    print("  structural losses on every message: "
          + ", ".join(deliver.STRUCTURAL_LOSSES))
    by_id = {message_id: fixture for fixture, message_id in found.items()}
    by_fixture = {email.message_id: email for email in drawn}
    landed: dict[str, str] = {}
    for item in fetched:
        fixture = by_fixture[by_id[item["message_id"]]]
        landed[fixture.message_id] = item["thread_id"]
        lost = deliver.unexpected_divergence(fixture, schema.validate(item))
        if lost:
            print(f"  [WARN] {fixture.message_id}: delivery also dropped {', '.join(lost)}",
                  file=sys.stderr)

    split = _report_threading(chains, landed) if args.threads else 0

    missing = sorted(set(by_fixture) - set(found))
    for fixture in missing:
        print(f"  [WARN] {fixture}: never arrived — held, filtered or still in flight",
              file=sys.stderr)
    return 1 if missing or split else 0


def _compare(args: argparse.Namespace) -> int:
    delivered = json.loads(args.delivered.read_text(encoding="utf-8"))
    seeded = json.loads(args.seeded.read_text(encoding="utf-8"))
    differences, unmatched = deliver.compare(delivered, seeded)

    print(f"Compared {len(delivered)} delivered against {len(seeded)} seeded message(s)")
    for difference in differences:
        print(f"  {difference['subject']}")
        for field, (was, now) in difference["fields"].items():
            print(f"    {field}: seeded={was!r} delivered={now!r}")
    if not differences:
        print("  no field differs")
    for subject in unmatched:
        print(f"  [WARN] {subject!r} appears in only one round", file=sys.stderr)
    return 0


_COMMANDS = {"deliver": _deliver, "compare": _compare}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
