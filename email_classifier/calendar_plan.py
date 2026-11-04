"""Appointments read out of classified mail, planned as calendar events (STU-2089).

`calendar-pre` picks the report rows whose subtype the policy's `calendar:` block lists,
`appointment-extract` reads each one, and `calendar-post` hands the extraction to this
module. The model reports what the message says (a date, wall-clock times, the sentence
that states them); everything that can be checked or computed happens here: the evidence
is in the text, a stated weekday matches the date, a missing year is the next occurrence
after the message, and the times are placed on the policy's clock, DST included. What
fails a check lands in `logs/calendar/review.jsonl`, never in the plan.

`logs/calendar/events.jsonl` is append-only and the last row of a key is its state. A key
is `ref:<booking reference>` when the message gives one and `msg:<message id>` otherwise.
Every row carries `uid` and `status`, so a writer records what it did by appending a row
for the same key, and a later move or delete carries the uid forward.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from email_classifier.anonymize import deanonymize
from email_classifier.stage1.policy import CalendarPolicy

DEFAULT_PATHS = {
    "policy": "rules/policy.yaml",
    "taxonomy": "rules/taxonomy.yaml",
    "keymap": "logs/anonymization/keymap.json",
    "events": "logs/calendar/events.jsonl",
    "review": "logs/calendar/review.jsonl",
}

CONFIRMATION, RESCHEDULE, CANCELLATION, NOT_APPOINTMENT = (
    "confirmation", "reschedule", "cancellation", "not_appointment")
KINDS = (CONFIRMATION, RESCHEDULE, CANCELLATION, NOT_APPOINTMENT)
CONFIDENCES = ("high", "medium", "low")

CREATE, MOVE, DELETE, NONE = "create", "move", "delete", "none"
PLANNED = "planned"

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DATE = re.compile(r"(?:(\d{4})-)?(\d{2})-(\d{2})")
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class Refused(ValueError):
    """An extraction a deterministic check would not plan; its message is the reason."""


@dataclass(frozen=True)
class Appointment:
    kind: str
    message_id: str
    reference: str | None = None
    sender_domain: str = ""
    event: dict | None = None
    # The start of the event this message changes: the previous one for a reschedule,
    # the stated one for a cancellation.
    match_start: str | None = None


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def append_rows(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({"timestamp": now, **row}, ensure_ascii=False) + "\n")


def handled_message_ids(*journals: list[dict]) -> set[str]:
    """A message reviewed or planned once is not read again: its next appearance is a
    thread sibling the fetch widened to, not new information."""
    return {row["message_id"] for rows in journals for row in rows if row.get("message_id")}


def targets(report: dict, subtypes: tuple[str, ...], handled: set[str]) -> list[str]:
    """Message ids to extract, oldest first so a reschedule follows what it moves."""
    rows = [v for v in report.get("verdicts", [])
            if v.get("status") == "ok" and v.get("subtype") in subtypes
            and v["message_id"] not in handled]
    return [v["message_id"] for v in sorted(rows, key=lambda v: v.get("date") or "")]


def state(rows: list[dict]) -> dict[str, dict]:
    """The last row of each key."""
    return {row["key"]: row for row in rows}


def _normalized(text: str) -> str:
    return " ".join(text.split()).casefold()


def _resolve_date(written, message_day: date) -> date:
    match = _DATE.fullmatch(written) if isinstance(written, str) else None
    if not match:
        raise Refused(f"date {written!r} is not YYYY-MM-DD or MM-DD")
    year, month, day = match.groups()
    try:
        if year:
            return date(int(year), int(month), int(day))
        for offset in range(9):
            try:
                candidate = date(message_day.year + offset, int(month), int(day))
            except ValueError:
                if int(month) == 2 and int(day) == 29:
                    continue
                raise
            if candidate >= message_day:
                return candidate
    except ValueError:
        pass
    raise Refused(f"date {written!r} is not a calendar date")


def _localize(day: date, written, zone: ZoneInfo) -> datetime:
    match = _TIME.fullmatch(written) if isinstance(written, str) else None
    if not match:
        raise Refused(f"time {written!r} is not HH:MM")
    return datetime(day.year, day.month, day.day, int(match[1]), int(match[2]), tzinfo=zone)


def _check_weekday(day: date, weekday) -> None:
    if weekday in (None, ""):
        return
    if weekday not in _WEEKDAYS:
        raise Refused(f"weekday {weekday!r} is not an English day name")
    if _WEEKDAYS[day.weekday()] != weekday:
        raise Refused(f"the message says {weekday} but {day.isoformat()} is a "
                      f"{_WEEKDAYS[day.weekday()]}")


def _start(output: dict, date_key: str, time_key: str, message_day: date,
           zone: ZoneInfo, weekday=None) -> datetime:
    day = _resolve_date(output.get(date_key), message_day)
    _check_weekday(day, weekday)
    return _localize(day, output.get(time_key), zone)


def read(item: dict, output, calendar: CalendarPolicy, keymap: dict[str, str]) -> Appointment:
    """One extraction checked against its message, or `Refused`.

    `item` is the fan-out item, tokenized as the model read it, so the evidence is
    looked for in that text; what is planned is restored through `keymap`.
    """
    if not isinstance(output, dict):
        raise Refused("no extraction")
    kind = output.get("kind")
    if kind not in KINDS:
        raise Refused(f"kind {kind!r} is not one of {list(KINDS)}")
    confidence = output.get("confidence")
    if confidence not in CONFIDENCES:
        raise Refused(f"confidence {confidence!r} is not one of {list(CONFIDENCES)}")
    if confidence == "low":
        raise Refused("low confidence")
    if kind == NOT_APPOINTMENT:
        return Appointment(kind, item["message_id"])

    evidence = output.get("date_evidence")
    text = _normalized(f"{item.get('subject', '')}\n{item.get('body_text', '')}")
    if not isinstance(evidence, str) or not evidence.strip() or _normalized(evidence) not in text:
        raise Refused("date_evidence does not appear in the message")

    zone = ZoneInfo(calendar.timezone)
    message_day = datetime.fromisoformat(item["date"]).astimezone(zone).date()
    restore = lambda value: deanonymize(value, keymap) if isinstance(value, str) and value else None
    reference = restore(output.get("reference"))
    sender = restore(item.get("from_address")) or ""
    domain = sender.rpartition("@")[2].lower()

    if kind == CANCELLATION:
        match_start = None
        if output.get("date"):
            match_start = _start(output, "date", "start_time", message_day, zone,
                                 output.get("weekday")).isoformat()
        return Appointment(kind, item["message_id"], reference, domain, None, match_start)

    start = _start(output, "date", "start_time", message_day, zone, output.get("weekday"))
    if output.get("end_time"):
        end = _localize(start.date(), output["end_time"], zone)
        if end <= start:
            raise Refused("end_time is not after start_time")
    else:
        # Added on the absolute clock: an hour from 01:30 on a fall-back night is 01:30
        # again, an hour later.
        end = (start.astimezone(timezone.utc)
               + timedelta(minutes=calendar.default_minutes)).astimezone(zone)
    event = {
        "title": restore(output.get("title")) or restore(item.get("subject")) or "",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "location": restore(output.get("location")),
        "reference": reference,
        "sender_domain": domain,
    }
    match_start = None
    if kind == RESCHEDULE and output.get("previous_date"):
        match_start = _start(output, "previous_date", "previous_start_time",
                             message_day, zone).isoformat()
    return Appointment(kind, item["message_id"], reference, domain, event, match_start)


def _key(appointment: Appointment) -> str:
    if appointment.reference:
        return f"ref:{appointment.reference}"
    return f"msg:{appointment.message_id}"


def _live(current: dict[str, dict]) -> dict[str, dict]:
    return {key: row for key, row in current.items() if row.get("op") in (CREATE, MOVE)}


def _match(appointment: Appointment, live: dict[str, dict]) -> str | None:
    """The live event this message changes: by reference, then by sender domain and
    start. More than one candidate is refused, never picked."""
    if appointment.reference and f"ref:{appointment.reference}" in live:
        return f"ref:{appointment.reference}"
    if not appointment.match_start:
        return None
    wanted = datetime.fromisoformat(appointment.match_start)
    found = [key for key, row in live.items()
             if row["event"]["sender_domain"] == appointment.sender_domain
             and datetime.fromisoformat(row["event"]["start"]) == wanted]
    if len(found) > 1:
        raise Refused(f"{len(found)} events match the sender and previous start")
    return found[0] if found else None


def _item(op: str, key: str, appointment: Appointment, entity: str,
          event: dict | None, uid=None, previous: dict | None = None) -> dict:
    return {"op": op, "key": key, "message_id": appointment.message_id, "entity": entity,
            "event": event, "previous": previous, "uid": uid}


def _step(appointment: Appointment, live: dict[str, dict], entity: str) -> dict:
    if appointment.kind == NOT_APPOINTMENT:
        return _item(NONE, f"msg:{appointment.message_id}", appointment, entity, None)
    if appointment.kind == CONFIRMATION:
        key = _key(appointment)
        current = live.get(key)
        if current is None:
            return _item(CREATE, key, appointment, entity, appointment.event)
        if (current["event"]["start"], current["event"]["end"]) == (
                appointment.event["start"], appointment.event["end"]):
            return _item(NONE, f"msg:{appointment.message_id}", appointment, entity, None)
        return _item(MOVE, key, appointment, entity, appointment.event, current.get("uid"),
                     {"start": current["event"]["start"], "end": current["event"]["end"]})
    key = _match(appointment, live)
    if key is None:
        raise Refused(f"no planned event matches this {appointment.kind}")
    current = live[key]
    if appointment.kind == CANCELLATION:
        return _item(DELETE, key, appointment, entity, current["event"], current.get("uid"))
    event = {**appointment.event,
             "reference": appointment.reference or current["event"].get("reference")}
    return _item(MOVE, key, appointment, entity, event, current.get("uid"),
                 {"start": current["event"]["start"], "end": current["event"]["end"]})


def plan(pending: list[dict], results: dict[int, dict], journal: list[dict],
         calendar: CalendarPolicy, keymap: dict[str, str], run_id: str
         ) -> tuple[list[dict], list[dict], list[dict]]:
    """(plan, event rows, review rows) for one run's extractions, in `pending` order.

    An item whose child run failed yields nothing: it was not read, so a later run that
    meets it again reads it then.
    """
    live = _live(state(journal))
    planned, events, review = [], [], []
    for index, item in enumerate(pending):
        result = results.get(index)
        if not result or result.get("status") != "success":
            continue
        output = result.get("output")
        try:
            step = _step(read(item, output, calendar, keymap), live, calendar.entity)
        except Refused as refusal:
            review.append({"run_id": run_id, "message_id": item["message_id"],
                           "reason": str(refusal), "extraction": _restored(output, keymap)})
            continue
        events.append({"run_id": run_id, "status": PLANNED, **step})
        if step["op"] == NONE:
            continue
        planned.append(step)
        if step["op"] == DELETE:
            live.pop(step["key"], None)
        else:
            live[step["key"]] = step
    return planned, events, review


def _restored(output, keymap: dict[str, str]):
    if not isinstance(output, dict):
        return output
    return {k: deanonymize(v, keymap) if isinstance(v, str) else v for k, v in output.items()}
