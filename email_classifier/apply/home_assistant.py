"""The calendar plan written to Home Assistant (STU-2090).

`calendar-post` appends `planned` rows to `logs/calendar/events.jsonl`; this applies every
planned row no `applied` or `failed` row has settled since, oldest first, and appends one
row per operation. So a plan made under a dry run is applied by the next run that may
write, the planner having marked its message handled.

Create goes through the `calendar.create_event` service, which returns no uid: the event
is found again over its own window by the marker its description carries. Move and
delete go through the websocket API, the instance listing no service for either. A uid
is never guessed: no match, several matches, or a move or delete with no applied uid on
its key is a `failed` row with a reason, and nothing is sent for it.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode

from email_classifier.calendar_plan import CREATE, DELETE, MOVE, PLANNED, append_rows

APPLIED, FAILED = "applied", "failed"


class HomeAssistantError(RuntimeError):
    pass


def marker(row: dict) -> str:
    return f"[mail:{row['message_id']} ref:{row['event'].get('reference') or '-'}]"


def _http(method: str, url: str, token: str, body: dict | None = None):
    request = urllib.request.Request(
        url, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raise HomeAssistantError(f"{method} {url.split('?')[0]}: HTTP {exc.code}") from None
    except OSError as exc:
        raise HomeAssistantError(f"{method} {url.split('?')[0]}: {exc}") from None


def _websocket(url: str):
    import websocket  # the `home-assistant` extra
    return websocket.create_connection(url, timeout=30)


class HomeAssistant:
    def __init__(self, url: str, token: str, http=_http, websocket=_websocket):
        self.url, self._token = url.rstrip("/"), token
        self._http, self._websocket = http, websocket

    def _call(self, method: str, path: str, body: dict | None = None):
        return self._http(method, self.url + path, self._token, body)

    def create(self, entity: str, event: dict, description: str) -> None:
        data = {"entity_id": entity, "summary": event["title"],
                "start_date_time": event["start"], "end_date_time": event["end"],
                "description": description}
        if event.get("location"):
            data["location"] = event["location"]
        self._call("POST", "/api/services/calendar/create_event", data)

    def events(self, entity: str, start: str, end: str) -> list[dict]:
        query = urlencode({"start": start, "end": end})
        return self._call("GET", f"/api/calendars/{quote(entity)}?{query}") or []

    def command(self, message: dict) -> None:
        url = "ws" + self.url.removeprefix("http") + "/api/websocket"
        try:
            connection = self._websocket(url)
        except OSError as exc:
            raise HomeAssistantError(f"websocket: {exc}") from None
        try:
            json.loads(connection.recv())
            connection.send(json.dumps({"type": "auth", "access_token": self._token}))
            auth = json.loads(connection.recv())
            if auth.get("type") != "auth_ok":
                raise HomeAssistantError(f"websocket auth: {auth.get('type')}")
            connection.send(json.dumps({"id": 1, **message}))
            result = json.loads(connection.recv())
            if not result.get("success"):
                raise HomeAssistantError(
                    f"{message['type']}: {(result.get('error') or {}).get('message')}")
        finally:
            connection.close()


def from_env() -> HomeAssistant:
    url, token = os.environ.get("HA_URL", ""), os.environ.get("HA_TOKEN", "")
    missing = [name for name, value in (("HA_URL", url), ("HA_TOKEN", token)) if not value]
    if missing:
        raise HomeAssistantError(
            f"{' and '.join(missing)} unset: the calendar plan cannot be written")
    return HomeAssistant(url, token)


def pending(rows: list[dict]) -> list[dict]:
    """Planned rows no later `applied` or `failed` row of their key has settled."""
    out: list[dict] = []
    for row in rows:
        if row.get("status") == PLANNED and row.get("op") in (CREATE, MOVE, DELETE):
            out.append(row)
        elif row.get("status") in (APPLIED, FAILED):
            out = [r for r in out if r["key"] != row["key"]]
    return out


def _applied_uids(rows: list[dict]) -> dict[str, str]:
    uids: dict[str, str] = {}
    for row in rows:
        if row.get("status") != APPLIED:
            continue
        if row["op"] == DELETE:
            uids.pop(row["key"], None)
        elif row.get("uid"):
            uids[row["key"]] = row["uid"]
    return uids


def _event_body(row: dict) -> dict:
    event = row["event"]
    body = {"summary": event["title"], "dtstart": event["start"], "dtend": event["end"],
            "description": marker(row)}
    if event.get("location"):
        body["location"] = event["location"]
    return body


def _apply_one(client: HomeAssistant, row: dict, uid: str | None) -> str:
    entity, event = row["entity"], row["event"]
    if row["op"] == CREATE:
        client.create(entity, event, marker(row))
        found = [e.get("uid") for e in client.events(entity, event["start"], event["end"])
                 if marker(row) in (e.get("description") or "")]
        if len(found) != 1 or not found[0]:
            raise HomeAssistantError(
                f"created, but {len(found)} events carry {marker(row)}: no uid recorded")
        return found[0]
    if not uid:
        raise HomeAssistantError(f"no applied uid for {row['key']}: nothing sent")
    if row["op"] == MOVE:
        client.command({"type": "calendar/event/update", "entity_id": entity, "uid": uid,
                        "event": _event_body(row)})
    else:
        client.command({"type": "calendar/event/delete", "entity_id": entity, "uid": uid})
    return uid


def describe(row: dict) -> str:
    event = row["event"]
    span = f"{event['start']} -> {event['end']}"
    if row["op"] == MOVE and row.get("previous"):
        span = f"{row['previous']['start']} => {span}"
    return f"{row['op']} {row['entity']} {row['key']} {event['title']!r} {span}"


def apply(rows: list[dict], client: HomeAssistant, run_id: str, events_path: Path
          ) -> list[dict]:
    """Apply `pending(rows)` in order, appending each outcome as it lands."""
    uids = _applied_uids(rows)
    written = []
    for row in pending(rows):
        item = {k: v for k, v in row.items() if k not in ("timestamp", "run_id", "status")}
        try:
            uid = _apply_one(client, row, uids.get(row["key"]))
        except HomeAssistantError as exc:
            outcome = {"run_id": run_id, **item, "status": FAILED,
                       "uid": uids.get(row["key"]), "reason": str(exc)}
        else:
            outcome = {"run_id": run_id, **item, "status": APPLIED, "uid": uid}
            if row["op"] == DELETE:
                uids.pop(row["key"], None)
            else:
                uids[row["key"]] = uid
        append_rows(events_path, [outcome])
        written.append(outcome)
    return written
