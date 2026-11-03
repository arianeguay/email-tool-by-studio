#!/usr/bin/env python3
"""Name Inbox mail that no report holds a verdict for (STU-1502).

On 2026-09-03 the webhook on the serving host refused connections from 03:23 into the next
day, and the three messages that arrived meanwhile were never classified. Nothing
recovered them: the `missed` catch-up (STU-937) starts only on a Graph lifecycle event,
which an unreachable endpoint does not produce, and `start_date: today` (STU-1123) drops
yesterday's mail from every later run. Nothing asked the mailbox what it holds that
`reports/` does not, and this does: it lists the Inbox since a stated instant and diffs
the ids against every `ok` verdict on disk. An errored verdict does not count (STU-1992):
from 2026-09-30 to 2026-10-01 every stage-2 call on the serving host failed to spawn `claude`,
21 messages were never classified nor drafted, and this check read them as held because
a run had looked at them. Looked at is not decided; a later `ok` verdict clears one.

    0  every Inbox message it could judge holds a verdict
    2  no report on disk, so there is nothing to match the mailbox against
    3  Inbox mail holds no verdict; the ids go to stdout, the alert carries the count
       and the oldest date
    4  the mailbox could not be read, which is not a mailbox with nothing missing
    5  nothing said since when (`--since` or `$UNVERDICTED_SINCE`), or the policy's
       provider is not one this can list

The instant has no default. `today` is what lost the 2026-09-03 mail in the first place,
and a check that picks its own start is a check that can quietly look at nothing, so it
refuses under 5 the way `check_webhook_health.py` refuses an address nobody named.

Two windows are not judged, and each is counted on the line rather than dropped:

- mail older than the reports shelf (`retention.SHELVES`, 30 days). A report is written
  after the mail it covers arrives, so mail inside the shelf has its report on disk if
  it ever had one; older mail may have lost its verdict to `make prune-apply`, and would
  otherwise read as missed forever.
- mail from the last `GRACE`. The webhook batches, runs stage 2 and only then writes the
  report, so mail that is merely in flight would page someone every hour.

Ids and dates only, never a subject or a sender: the line lands in cron mail and the
alert on a phone.

Every run is appended to `state/unverdicted-checks.jsonl`, and the alert dedups on it:
a run notifies when its code differs from the previous run's, or when it exits 3 naming
an id the previous run did not. The unchanged set a standing miss produces every hour is
one alert, a new miss is another, and a set that only shrinks (someone handled one) is
the repair in progress rather than news. `--source` is recorded on the line, as in
`check_webhook_health.py`, and a hand-run notifies too; `--journal` keeps a drill off the
host's file.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from email_classifier import alerts, retention
from email_classifier.providers import get_fetcher
from email_classifier.stage1.policy import load_policy
from email_classifier import deployment

REPO_ROOT = deployment.ROOT
REPORTS = REPO_ROOT / retention.REPORTS
POLICY = REPO_ROOT / "rules" / "policy.yaml"
JOURNAL = REPO_ROOT / "state" / "unverdicted-checks.jsonl"
SINCE_ENV = "UNVERDICTED_SINCE"

NO_REPORTS = 2
UNVERDICTED = 3
UNREADABLE = 4
NOT_CONFIGURED = 5

# Long enough for a batch window, a stage-2 escalation and the report write, and short
# of the hourly schedule, so a miss is named on the run after the one it was in flight.
GRACE = timedelta(hours=1)

_SHELF = next(s for s in retention.SHELVES if s.path == retention.REPORTS)


class _Parser(argparse.ArgumentParser):
    """Usage errors exit 5, not argparse's 2, which here means no report on disk."""

    def error(self, message: str) -> None:  # pragma: no cover - argparse contract
        self.print_usage(sys.stderr)
        self.exit(NOT_CONFIGURED, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "--since", default=os.environ.get(SINCE_ENV),
        help=f"ISO 8601 instant the mailbox is judged from; defaults to ${SINCE_ENV}. "
             "Required one way or the other.",
    )
    parser.add_argument("--reports", type=Path, default=REPORTS)
    parser.add_argument("--policy", type=Path, default=POLICY)
    parser.add_argument("--source", choices=("cron", "manual"), default="manual")
    parser.add_argument(
        "--journal", type=Path, default=JOURNAL, metavar="PATH",
        help=f"Where to append this run's verdict (default {JOURNAL}); a throwaway "
             "path keeps a drill off the host's dedup.",
    )
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)

    code, message, unmatched = _check(args, now)
    prefix = {0: "", UNVERDICTED: "[UNVERDICTED]"}.get(code, "[ALERT]")
    line = f"{prefix} {message}".strip()
    print(line, file=sys.stdout if code == 0 else sys.stderr)
    for message_id, received in unmatched:
        print(f"  {received} {message_id}")

    ids = sorted(message_id for message_id, _ in unmatched)
    previous = _last_entry(args.journal)
    _record(args.journal, now, code, message, args.source, ids)
    if code != 0 and (
        previous is None
        or previous.get("exit") != code
        or set(ids) - set(previous.get("unmatched", []))
    ):
        alerts.notify(line)
    return code


def _check(
    args: argparse.Namespace, now: datetime
) -> tuple[int, str, list[tuple[str, str]]]:
    if not args.since:
        return NOT_CONFIGURED, (
            f"no instant to judge the mailbox from: pass --since or set ${SINCE_ENV}"
        ), []
    try:
        since = _parse(args.since)
    except ValueError:
        return NOT_CONFIGURED, f"--since {args.since!r} is not an ISO 8601 instant", []
    provider = load_policy(args.policy).email_provider
    if provider != "outlook":
        return NOT_CONFIGURED, (
            f"listing the Inbox is implemented for outlook, not {provider!r}"
        ), []

    verdicts = _verdict_ids(args.reports)
    if verdicts is None:
        return NO_REPORTS, (
            f"no report in {args.reports}, so no verdict to match the Inbox against"
        ), []

    try:
        listed = get_fetcher(provider).inbox_received_since(_graph_instant(since))
    except Exception as exc:  # any failure to list is an unmeasured mailbox, never a clean one
        return UNREADABLE, f"the Inbox could not be listed: {type(exc).__name__}: {exc}", []

    shelf_floor = now - timedelta(days=_SHELF.max_age_days)
    grace_floor = now - GRACE
    beyond_shelf = in_flight = 0
    unmatched = []
    for message_id, received in listed:
        at = _parse(received)
        if at < shelf_floor:
            beyond_shelf += 1
        elif at > grace_floor:
            in_flight += 1
        elif message_id not in verdicts:
            unmatched.append((message_id, received))
    unmatched.sort(key=lambda pair: _parse(pair[1]))

    judged = len(listed) - beyond_shelf - in_flight
    skipped = (
        f"; not judged: {beyond_shelf} older than the {_SHELF.max_age_days}-day report "
        f"shelf, {in_flight} from the last {GRACE.total_seconds() / 3600:.0f}h"
    )
    if unmatched:
        return UNVERDICTED, (
            f"{len(unmatched)} of {judged} Inbox message(s) received since "
            f"{since.isoformat()} hold no verdict, oldest {unmatched[0][1]}{skipped}"
        ), unmatched
    return 0, (
        f"ok: all {judged} Inbox message(s) received since {since.isoformat()} "
        f"hold a verdict{skipped}"
    ), []


def _parse(value: str) -> datetime:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError(f"{value!r} names no timezone")
    return at.astimezone(timezone.utc)


def _graph_instant(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _verdict_ids(reports: Path) -> set[str] | None:
    """Every message id a report on disk holds an `ok` verdict for, None when there is none.

    An unreadable report is skipped: whatever it held then reads as missed, which is
    the loud direction.
    """
    files = sorted(reports.glob("*.json"))
    if not files:
        return None
    ids: set[str] = set()
    for path in files:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        ids.update(
            v["message_id"]
            for v in report.get("verdicts", [])
            if v.get("message_id") and v.get("status") == "ok"
        )
    return ids


def _last_entry(journal: Path) -> dict | None:
    """The previous run's line; None when unreadable, so a dedup that cannot remember repeats."""
    try:
        return json.loads(journal.read_text(encoding="utf-8").splitlines()[-1])
    except (OSError, ValueError, IndexError):
        return None


def _record(
    journal: Path, now: datetime, code: int, message: str, source: str, ids: list[str]
) -> None:
    entry = {
        "at": now.isoformat(),
        "exit": code,
        "source": source,
        "summary": message,
        "unmatched": ids,
    }
    try:
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"[WARN] could not record this run in {journal}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
