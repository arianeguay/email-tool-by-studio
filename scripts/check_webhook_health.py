#!/usr/bin/env python3
"""Alert when the webhook stops receiving notifications, or runs stale code (STU-931).

Polls /healthz and exits non-zero when the endpoint is unreachable or reports a
notification age past its configured threshold. That age is the only symptom a
deleted subscription produces: Graph stops POSTing and nothing else changes —
no error, no failed run, no log line. Run from cron; the non-zero exit is the
alert.

Since STU-1017 it also measures the commit the payload names against the remote's
`main`, under its own exit code: a host fifteen commits behind — two of them privacy
fixes — answered every one of these checks for days. Its own code because a behind
host is not an outage, and 2 has to keep meaning only "the webhook is dead".

Since STU-1037 it dials through the same ssh forward a delivery round opens, so a
laptop reads the staging webhook rather than its own empty loopback. A forward that
does not come up gets its own code too, for the reason 3 has one: the machine
running this learned nothing about the webhook, and saying it is dead is the false
alarm the forward was added to end.

Since STU-1087 the check refuses to poll an address nobody named, under a code of its
own: composing one out of `DEFAULT_PORT` is how 2 came to mean "webhook unreachable"
about a webhook answering one port over. `argparse` exits 2 on its own usage errors,
which collides with that same code, so those exit here too — both say the invocation
never posed the question, not that the webhook is dead. This is the same hand-run the
journal below caught recording an outage that never happened: without the env sourced it
now refuses rather than polls, so the false line cannot be written in the first place.

That refusal kept the code and gained a second message since STU-1122: Make's
`-include .env` cannot tell a missing file from one this account may not open, so on the
serving host — where STU-1116 put the config at `700 email-classifier` — an operator's own
`make check-webhook` reported the port as unset while it sat in the file `.env` links to.
It now names that file, its owner and running under that account, rather than sending
someone to export the 8080 it was refusing to dial. Still exit 5, deliberately: no cron
path reaches this condition (cron here runs as that account already), and both halves are
the same verdict — the invocation never got as far as asking the webhook anything.

Since STU-1095 it also carries the one fault that is announced before it happens: the
app-only client secret expires on a date fixed a year ahead, and it cannot be renewed
unattended. Two more codes, for the reason 3 and 5 have their own — the repair differs
each time, and the dedup key is the code alone, so a condition folded into another is a
condition that goes unreported behind it.

Since STU-1126 the drift leg reports its own blindness. A fetch that fails leaves the
host in exactly the state STU-1017 was built to end, and exiting 0 for it — still the
right call on its own — makes that indistinguishable from a host that is current. So the
consecutive unverified runs are counted off the journal below, and past a threshold the
condition gets its own code too.

Since STU-1168 it also counts down the other deadline nothing was watching. An audit
entry the push path filed for itself protects none of its artifacts (STU-1084), so 30
days after its report was written a reviewer loses the report, the anonymization record
and the apply journal, and the entry becomes counters nobody can check. `coverage` prints
how many are owed and no schedule runs it; this is the job that runs hourly and already
announces what expires. Its own code, for the reason the two above have theirs.

Since STU-1181 the drift itself is counted the same way, for the failure the dedup below
leaves uncovered: a drift that does not end. The serving host exited 3 on 27 consecutive cron
runs — 2026-08-30T19:45Z to 2026-08-31T23:23Z, the summary walking from 2 commits behind
to 22 — for one notification, exactly as designed, and the drift ended because a human
happened to read the probe's output in a session about something else. Every draft written
for a real parent in those 27 hours came from a drafter `main` had already replaced. A
measured drift that survives the threshold below therefore earns a second code, so a
standing drift is distinguishable on the phone from a fresh one; a blind leg and an
ignored one look identical from the operator's end today, and both are silence.

    0  fresh, and running what `main` holds
    1  no notification since the stale threshold
    2  the endpoint could not be reached
    3  fresh, but serving code `main` has moved past
    4  the ssh forward to the endpoint could not be opened
    5  nothing said which address to ask (a usage error, or an unreadable `.env`)
    6  the client secret expires inside the warning window
    7  a client secret is configured but nothing says when it expires
    8  the drift leg has gone unmeasured this many runs in a row
    9  the drift the leg measured has stood this many runs in a row
   10  an unreviewed audit entry loses its artifacts inside the warning window

`--wait` is for the one caller that knows better than the socket: `make pull-webhook`
has just restarted the unit, and `Type=simple` means systemd returned as soon as the
process forked, seconds before it binds. Only the unreachable case is retried, and only
for a caller that asked -- from cron, a webhook that does not answer is the alert.

Since STU-1066 the non-zero exit is no longer the whole alert. It was a cron mail on a
host nobody reads mail on, which is how a check exiting 3 for a host serving code
fifteen commits behind `main` said so to nobody for days. Every run is appended to
`state/health-checks.jsonl` and a fault goes out through `alerts.notify`.

That journal exists to settle criterion 5 of the pilot gate, which asks what the *hourly*
runs exited over five days -- and it could not, because every caller wrote the same shape
(STU-1082). Three lines landed within four minutes of the file existing and not one of
them was an hourly run; one was a hand-run of this script without the env sourced, which
polled a port another service holds and recorded an outage that never happened. So a line
now names what produced it, and `--journal` points a drill at a throwaway path: proving
the alert channel works was only possible by hand-patching the module constant, which is
what made that mess possible. `--source` defaults to `manual` because the failure
direction has to be a cron run missing from the count rather than a hand-run inflating it.

A hand-run still notifies. The check is the one thing on this host whose purpose is that
silence gets broken, and someone running it wants to see that end-to-end; `--journal` is
what keeps a drill off the pilot's evidence, and it is the whole of what keeps it off.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from email_classifier import alerts, audit_expiry
from email_classifier.webhook import address, credential, deploy, tunnel

JOURNAL = deploy.REPO_ROOT / "state" / "health-checks.jsonl"

NOT_CONFIGURED = 5
EXPIRING_SOON = 6
EXPIRY_UNKNOWN = 7
DEPLOY_BLIND = 8
DRIFT_SUSTAINED = 9
AUDIT_EXPIRING = 10

# Hourly runs. Three names a genuinely broken remote inside the day, and rides out the
# isolated resolver miss the serving host is documented to take — both the misses measured
# there were single runs that succeeded on the retry. The three consecutive blind runs
# of 30 August 2026 are what this is sized against.
BLIND_AFTER_RUNS = 3

# Hourly runs, and not inherited from the number above: that one sizes a first
# notification, this one a second. Six is a working half-day — long enough that the
# exit-3 alert was demonstrably read by nobody rather than merely not acted on yet, which
# is what a threshold of three would mostly report while the first notification is still
# on the screen. Long enough matters twice, because the dedup keys on the code alone and
# a repeat of a fresh alert is what teaches an operator to swipe the next one away.
# Against a larger number: the club's mail is daytime, so twelve runs is an entire
# business day of parents answered by a drafter `main` had replaced, and the 27 runs
# measured on the serving host are what that costs at the far end. Six also lands the escalation
# where someone can act on it — merges land in waking hours, and this alert fires once
# and is then spent for as long as the drift stands.
SUSTAINED_AFTER_RUNS = 6

_PREFIX = {0: "", 3: "[DRIFT]", 6: "[EXPIRING]", 8: "[BLIND]", 9: "[DRIFT UNFIXED]",
           10: "[AUDIT EXPIRING]"}


class _Parser(argparse.ArgumentParser):
    """An `ArgumentParser` whose usage errors do not read as a dead webhook.

    `argparse` exits 2, which is this check's "the endpoint could not be reached" —
    so a mistyped flag in a cron line reported an outage. A usage error and an
    unnamed address are the same condition anyway, and repaired the same way: the
    invocation never got as far as asking the webhook anything.
    """

    def error(self, message: str) -> None:  # pragma: no cover - argparse contract
        self.print_usage(sys.stderr)
        self.exit(NOT_CONFIGURED, f"{self.prog}: error: {message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "--url",
        help="/healthz URL; defaults to $WEBHOOK_HEALTH_URL, else the address "
             "$WEBHOOK_HOST/$WEBHOOK_PORT say the webhook listens on. Passed "
             "explicitly it makes both irrelevant, so an unconfigured environment "
             "is only refused when nothing else named an address.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="Keep retrying an unreachable endpoint for this long before alerting. "
             "For a caller that just restarted the service; leave it off in cron.",
    )
    parser.add_argument(
        "--source", choices=("cron", "deploy", "manual"), default="manual",
        help="What is running this, recorded on the journal line so the pilot gate can "
             "read the hourly runs apart from the rest (STU-1082). `manual` is the "
             "default so a forgotten flag undercounts cron rather than inventing it.",
    )
    parser.add_argument(
        "--journal", type=Path, default=JOURNAL, metavar="PATH",
        help=f"Where to append this run's verdict (default {JOURNAL}). Point it at a "
             "throwaway path to drill the alert channel without writing into the "
             "pilot's evidence.",
    )
    args = parser.parse_args(argv)

    # Built from the same $WEBHOOK_HOST/$WEBHOOK_PORT the server binds, so moving
    # the port moves the check with it (STU-978) — and refused outright when
    # neither that port nor an explicit URL was named (STU-1087), rather than
    # polling a guess and reporting the answer as the webhook's silence. Resolved
    # after parsing, so `--url` is a complete answer on its own.
    try:
        args.url = args.url or address.configured_health_url()
    except RuntimeError as exc:
        code, message, drift = NOT_CONFIGURED, str(exc), None
    else:
        code, message, drift = _check(args)
    prefix = _PREFIX.get(code, "[ALERT]")
    line = f"{prefix} {message}".strip()
    print(line, file=sys.stdout if code == 0 else sys.stderr)

    # The codes are already the distinct conditions — each was given its own number
    # precisely so it would not collapse into another. So the code is the whole
    # dedup key: a drift left unfixed for a day is one fault, not 24, and the summary
    # cannot be part of the key, a stale age and a moving `main` tip both changing every
    # hour without the fault changing at all. Nor is the source: a fault belongs to the
    # host, not to whoever asked, and keying on it would notify twice for the drift a
    # hand-run just reported.
    previous = _last_exit(args.journal)
    _record(args.journal, code, message, args.source, drift)
    if code != 0 and code != previous:
        alerts.notify(line)
    return code


def _check(args: argparse.Namespace) -> tuple[int, str, deploy.Drift | None]:
    """The verdict, the line describing it, and what the deploy leg answered.

    That third value is None when the run never reached the deploy leg at all, which
    is neither of the things the leg itself can say: the streaks below have to tell a
    run that asked and got silence, a run that asked and was told it is behind, and a
    run that never asked, apart from one another.
    """
    deadline = time.monotonic() + args.wait
    while True:
        try:
            with tunnel.reachable(args.url) as dialable:
                with urllib.request.urlopen(dialable, timeout=args.timeout) as response:
                    health = json.load(response)
            break
        except tunnel.ForwardFailed as exc:
            return 4, str(exc), None
        except (OSError, ValueError) as exc:
            if time.monotonic() >= deadline:
                return 2, f"webhook unreachable at {args.url}: {exc}", None
            time.sleep(1.0)

    age = _duration(health["last_notification_age_seconds"])
    # `.get`: a host still serving the payload from before STU-1017 has no commit to
    # compare, which is a deploy this check cannot vouch for and not a crash.
    drift = deploy.measure(health.get("commit"), kernel=health.get("kernel_commit"))
    if health["status"] != "ok":
        # Silence beats staleness: a webhook receiving nothing is not classifying
        # mail at all, whichever commit it would have classified it with.
        return 1, (
            f"no notification in {age} "
            f"(threshold {_duration(health['stale_after_seconds'])}); "
            f"{_describe(health['subscription'])}; {drift.summary()}"
        ), drift

    # Ahead of both deploy verdicts, behind silence. A host serving stale code repairs
    # itself with `make pull-webhook` and is caught again on the next hourly run, and a
    # blind leg keeps reporting itself for as long as it is blind; the warning window is
    # the only thing here that runs out, and either one left standing would otherwise
    # mask it for the whole month it exists to give.
    expiry = credential.measure()
    if expiry is not None:
        if expiry.unknown:
            return EXPIRY_UNKNOWN, expiry.summary(), drift
        if expiry.soon:
            return EXPIRING_SOON, expiry.summary(), drift

    # Behind the credential and ahead of the drift, by the rule the paragraph above
    # states: a window is the only thing here that runs out, and a drift left standing
    # in front of one would mask it for the whole month it exists to give. The repair
    # differs too — this one is a person reading an audit, not a `make pull-webhook`.
    audit = audit_expiry.measure()
    if audit is not None and audit.soon:
        return AUDIT_EXPIRING, audit.summary(), drift

    # A drift that stands is a second fault, not a louder first one (STU-1181). The
    # exit-3 alert fires once and the dedup is right to leave it at that — an age and a
    # moving `main` tip both change every hour without the fault changing — but nothing
    # then separated a drift somebody is repairing from one nobody read, and the second
    # is 27 hours of live parent mail written by a drafter `main` had replaced.
    if drift.behind:
        standing = _streak(args.journal, "deploy_behind") + 1
        if standing >= SUSTAINED_AFTER_RUNS:
            return (
                DRIFT_SUSTAINED,
                f"{drift.summary()} — {standing} consecutive runs",
                drift,
            )
        return 3, drift.summary(), drift

    # A single unmeasured run still exits 0: answering "behind" off a `main` this clone
    # last saw weeks ago is worse than answering nothing, and STU-1017 chose that
    # deliberately. What it cannot do is separate "nobody has looked" from "looked and
    # found nothing wrong", so the runs that looked at nothing are counted instead.
    if drift.unverified:
        streak = _streak(args.journal, "deploy_unverified") + 1
        if streak >= BLIND_AFTER_RUNS:
            return DEPLOY_BLIND, f"{drift.summary()} — {streak} consecutive runs", drift

    return 0, (
        f"ok — last notification {age} ago, "
        f"{_describe(health['subscription'])}, {drift.summary()}"
        f"{_describe_expiry(expiry)}{_describe_audit(audit)}"
    ), drift


def _entries(journal: Path) -> list[dict]:
    """The runs this journal holds, oldest first, unreadable lines dropped."""
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def _last_exit(journal: Path) -> int | None:
    """The code the previous run recorded, or None when there is no previous run.

    None is what makes an unwritable or unreadable journal alert every hour rather
    than never: a dedup that cannot remember must repeat, not stay quiet.

    Every source reads the same journal, so a drill pointed elsewhere dedups against
    its own file and leaves the host's standing fault reported exactly once.
    """
    try:
        return _entries(journal)[-1]["exit"]
    except (KeyError, IndexError):
        return None


def _streak(journal: Path, field: str) -> int:
    """How many runs in a row this field has stood true for, this one aside.

    Read back from the journal rather than kept in memory, because there is no memory
    to keep it in: the check is an hourly cron process that exits.

    A line that does not carry the field is transparent — it predates the field, or its
    run never got far enough to answer for it (an unreachable endpoint, an address nobody
    named, or, for `deploy_behind`, a leg that measured nothing) — and none of those is
    evidence either way. Only a line carrying the field false breaks the streak: for
    `deploy_unverified` that is a run that measured, for `deploy_behind` a run that
    measured *current* with the branch. A journal that cannot be read counts nothing
    rather than assuming a streak: the count is a measurement, and one that could not be
    made is not one to invent.

    One function for both counts because they are one rule with two subjects, and the
    exit code is the subject of neither — 8 and 0 are the same code for a blind leg
    (STU-1126), and 3 is masked outright by a stale age or an expiring secret while the
    drift underneath goes right on standing.
    """
    streak = 0
    for entry in reversed(_entries(journal)):
        verdict = entry.get(field)
        if verdict is None:
            continue
        if not verdict:
            break
        streak += 1
    return streak


def _record(
    journal: Path, code: int, message: str, source: str, drift: "deploy.Drift | None"
) -> None:
    """Append this run's verdict, so a window of hourly runs can be read back.

    `docs/homelab-pilot.md` criterion 5 asks that the check exited 0 on every hourly
    run over five days and never exited 3. Nothing recorded those codes, so the gate
    could not be settled from an artifact — which its own rule forbids.

    `source` is what makes them readable apart (STU-1082): cron, the post-restart check
    `make deploy-webhook` runs, and a hand-run all wrote the same shape, so the file
    answered "what did the hourly runs exit" with every run it had. A line without the
    field predates this and is attributable to nobody — counted as such rather than
    guessed at, the way a drafts-journal line without a run id is (STU-1003).

    `deploy_unverified` is what makes the file answer STU-1126's question too, and it
    is written only when the run actually consulted the deploy leg: the exit code
    cannot stand in for it, an unmeasured deploy exiting 0 alongside a healthy one.

    `deploy_behind` does the same for STU-1181, and is written a step further in: only
    when the leg answered at all, so a blind run says nothing about whether the host is
    behind rather than saying no. The exit code cannot stand in for this one either, and
    in the other direction — a stale age or an expiring secret outranks drift, so a host
    that is behind spends those runs exiting 1 or 6 while the drift stands underneath.
    """
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "exit": code,
        "source": source,
        "summary": message,
    }
    if drift is not None:
        entry["deploy_unverified"] = drift.unverified
        if not drift.unverified:
            entry["deploy_behind"] = drift.behind
    try:
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
    except OSError as exc:
        print(f"[WARN] could not record this run in {journal}: {exc}", file=sys.stderr)


def _duration(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h" if seconds >= 3600 else f"{seconds / 60:.0f}min"


def _describe_expiry(expiry: credential.Expiry | None) -> str:
    """The passing leg, named on the ok line, so wired-and-quiet is not silence.

    A leg that never fires and a leg nobody wired look identical from the outside,
    which is the reading STU-912 exists to refuse. None is an environment with no
    client secret, where there is genuinely nothing to report.
    """
    return f", {expiry.summary()}" if expiry else ""


def _describe_audit(audit: "audit_expiry.Expiry | None") -> str:
    """The audit-expiry leg on the ok line, so quiet is not indistinguishable from
    unwired (STU-912). None is a tree with no `reports/`, where nothing can be read.
    """
    return f", {audit.summary()}" if audit is not None else ""


def _describe(subscription: dict | None) -> str:
    if not subscription:
        return "no subscription recorded"
    return (
        f"subscription {subscription['subscription_id']} "
        f"expires {subscription.get('expires_at', 'unknown')}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
