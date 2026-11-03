"""How far back a run reaches (STU-1123).

Until this module, the answer was `max_results` — 33 from the Makefile, 100 from
`fetch_mailbox.py` — a count of conversations rather than a boundary anyone
decided. A mailbox this tool did not start with was therefore bounded by an
accident of arithmetic, and mail older than whatever window a run happened to use
was neither classified nor recorded as skipped.

`policy.start_date` is the boundary instead, and the default is **today**, resolved
per run and never at policy load: the webhook loads its policy once and serves for
days, so a date frozen at startup would keep reaching back to the last restart.
Today is the direction a wrong answer should fail in — a deployment gets nothing
retroactive until somebody writes a date down, because the other default drafts
replies into threads that were settled before the tool existed. The cost is real
and is the point: a run tomorrow does not pick up what it skipped today.

Two enforcements of one boundary, and they are not redundant. Each provider bounds
its own listing in its own query language (`since_iso` feeds both), which is what
keeps `max_results` from being spent on mail the run will drop; but Graph refuses
`$filter` alongside `$search`, and Gmail's `after:` is a date, not an instant. So
the query bound is a hint that is never *narrower* than the truth, and `in_scope`
is the exact one — parsed dates, not string order, since a projection is only
promised to be ISO 8601 and not to carry a particular offset.
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

# The first deployment's clock, and every function below's default `tz` — kept as a module
# constant so a caller that has not threaded `policy.timezone` through yet (STU-1462)
# still gets the mailbox's real zone rather than the host's, which is nobody's
# decision on an unattended machine.
#
# It is the only clock "today" can mean here. Read in UTC instead, "today" turns over
# at 20:00 local through the summer: an evening run would floor itself at 20:00
# tonight and leave the whole local working day out of scope, silently and every
# evening. `orchestrate.drafts` reads the same zone — the hour a reply closes on and
# the day a fetch reaches back to are one fact about this mailbox.
CLUB_TZ = ZoneInfo("America/Toronto")


def resolve(configured: date | None, tz: ZoneInfo = CLUB_TZ) -> date:
    """The run's floor: what the policy said, else today in the mailbox's own day."""
    if configured is not None:
        return configured
    return datetime.now(timezone.utc).astimezone(tz).date()


def since_iso(start: date, tz: ZoneInfo = CLUB_TZ) -> str:
    """The floor as an instant a provider query can carry — the start of that day (or
    the configured instant) in the mailbox's timezone, inclusive, expressed in UTC."""
    return _floor(start, tz).astimezone(timezone.utc).isoformat()


def _floor(start: date, tz: ZoneInfo = CLUB_TZ) -> datetime:
    """A date floors at its midnight; an instant floors where it says (STU-1897), read in
    the mailbox's zone unless it names its own offset — what lets an outage sweep start
    when the outage did instead of re-running the morning before it."""
    if isinstance(start, datetime):
        return start if start.tzinfo is not None else start.replace(tzinfo=tz)
    return datetime.combine(start, time.min, tzinfo=tz)


def in_scope(inputs: list[dict], start: date, tz: ZoneInfo = CLUB_TZ) -> list[dict]:
    """`inputs` minus every message that arrived before `start`.

    Applied to a listing, never to a conversation the listing already brought in:
    `expand_to_conversations` filters first and widens second, so a thread whose
    live half is in scope keeps its older messages. Dropping those would rebuild
    the split STU-963 exists to close — half a conversation in the Inbox and half
    in the Archive — by policy instead of by disagreement.

    An unparseable date raises rather than defaulting either way: a message the
    boundary cannot place is one nothing here can honestly call in or out.
    """
    floor = _floor(start, tz)
    return [item for item in inputs if datetime.fromisoformat(item["date"]) >= floor]
