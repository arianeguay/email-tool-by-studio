"""The calendar plan written to Home Assistant (STU-2090), against a fake instance."""

import io
import json
import sys
from urllib.parse import parse_qs, urlsplit

import pytest

from email_classifier import calendar_plan as cp
from email_classifier.apply import home_assistant as ha
from tests.test_mailbox_full import load_script

URL, TOKEN = "http://ha.example.test:8123", "fake-token"
EVENT = {"title": "Essai de violon", "start": "2026-11-04T14:00:00-05:00",
         "end": "2026-11-04T15:00:00-05:00", "location": "Studio B",
         "reference": "AB-1207", "sender_domain": "example-music.test"}
MOVED = {**EVENT, "start": "2026-11-10T16:00:00-05:00", "end": "2026-11-10T17:00:00-05:00"}


class FakeHA:
    """In-process Home Assistant: records every request, answers like a Local Calendar."""

    def __init__(self, listed=None, ws_success=True):
        self.http_calls, self.ws_sent = [], []
        self.calendar = []
        self.listed = listed
        self.ws_success = ws_success

    def http(self, method, url, token, body=None):
        assert token == TOKEN
        self.http_calls.append((method, url, body))
        parts = urlsplit(url)
        if parts.path == "/api/services/calendar/create_event":
            self.calendar.append({"uid": f"uid-{len(self.calendar) + 1}",
                                  "summary": body["summary"],
                                  "description": body["description"]})
            return []
        if parts.path.startswith("/api/calendars/"):
            return self.calendar if self.listed is None else self.listed
        raise AssertionError(url)

    def websocket(self, url):
        fake = self

        class Connection:
            def __init__(self):
                self.inbox = [{"type": "auth_required"}]

            def recv(self):
                return json.dumps(self.inbox.pop(0))

            def send(self, text):
                message = json.loads(text)
                fake.ws_sent.append((url, message))
                if message["type"] == "auth":
                    self.inbox.append({"type": "auth_ok"})
                else:
                    self.inbox.append({"id": message["id"], "type": "result",
                                       "success": fake.ws_success,
                                       "error": None if fake.ws_success
                                       else {"message": "Event not found"}})

            def close(self):
                pass

        return Connection()

    def client(self):
        return ha.HomeAssistant(URL + "/", TOKEN, http=self.http, websocket=self.websocket)


def planned(op, key="ref:AB-1207", message_id="m1", event=EVENT, uid=None, previous=None):
    return {"timestamp": "2026-10-20T13:00:00+00:00", "run_id": "run-0", "status": "planned",
            "op": op, "key": key, "message_id": message_id, "entity": "calendar.studio",
            "event": event, "previous": previous, "uid": uid}


def applied(op, uid="uid-9", **kw):
    return {**planned(op, **kw), "status": "applied", "uid": uid}


def run_apply(fake, rows, tmp_path):
    path = tmp_path / "events.jsonl"
    cp.append_rows(path, rows)
    written = ha.apply(cp.read_rows(path), fake.client(), "run-1", path)
    return written, cp.read_rows(path)


def test_a_create_posts_the_service_then_stores_the_uid_its_marker_finds(tmp_path):
    fake = FakeHA()
    written, journal = run_apply(fake, [planned("create")], tmp_path)
    (method, url, body), (read, window, _) = fake.http_calls
    assert (method, url) == ("POST", URL + "/api/services/calendar/create_event")
    assert body == {"entity_id": "calendar.studio", "summary": "Essai de violon",
                    "start_date_time": EVENT["start"], "end_date_time": EVENT["end"],
                    "description": "[mail:m1 ref:AB-1207]", "location": "Studio B"}
    parts = urlsplit(window)
    assert read == "GET" and parts.path == "/api/calendars/calendar.studio"
    assert parse_qs(parts.query) == {"start": [EVENT["start"]], "end": [EVENT["end"]]}
    assert written[0]["status"] == "applied" and written[0]["uid"] == "uid-1"
    assert journal[-1]["key"] == "ref:AB-1207" and journal[-1]["uid"] == "uid-1"
    assert cp.state(journal)["ref:AB-1207"]["uid"] == "uid-1"
    assert fake.ws_sent == []


def test_a_create_without_a_reference_marks_it_absent(tmp_path):
    fake = FakeHA()
    run_apply(fake, [planned("create", key="msg:m1", event={**EVENT, "reference": None,
                                                             "location": None})], tmp_path)
    body = fake.http_calls[0][2]
    assert body["description"] == "[mail:m1 ref:-]" and "location" not in body


@pytest.mark.parametrize("listed", [
    [],
    [{"uid": "a", "description": "[mail:m1 ref:AB-1207]"},
     {"uid": "b", "description": "note [mail:m1 ref:AB-1207]"}],
    [{"uid": "a", "description": "[mail:m2 ref:AB-1207]"}],
])
def test_a_create_whose_marker_matches_zero_or_several_events_records_no_uid(
    tmp_path, listed
):
    written, journal = run_apply(FakeHA(listed=listed), [planned("create")], tmp_path)
    assert written[0]["status"] == "failed" and written[0]["uid"] is None
    assert "no uid recorded" in written[0]["reason"]
    assert journal[-1]["status"] == "failed"


def test_a_move_updates_over_the_websocket_with_the_applied_uid(tmp_path):
    fake = FakeHA()
    previous = {"start": EVENT["start"], "end": EVENT["end"]}
    written, _ = run_apply(fake, [applied("create"), planned(
        "move", message_id="m2", event=MOVED, uid="uid-9", previous=previous)], tmp_path)
    url = "ws://ha.example.test:8123/api/websocket"
    assert fake.ws_sent == [
        (url, {"type": "auth", "access_token": TOKEN}),
        (url, {"id": 1, "type": "calendar/event/update", "entity_id": "calendar.studio",
               "uid": "uid-9", "event": {"summary": "Essai de violon",
                                         "dtstart": MOVED["start"], "dtend": MOVED["end"],
                                         "description": "[mail:m2 ref:AB-1207]",
                                         "location": "Studio B"}}),
    ]
    assert fake.http_calls == []
    assert written[0]["status"] == "applied" and written[0]["uid"] == "uid-9"


def test_a_delete_sends_the_uid_and_forgets_it(tmp_path):
    fake = FakeHA()
    written, journal = run_apply(fake, [applied("create"), planned("delete", message_id="m3")],
                                 tmp_path)
    assert fake.ws_sent[1][1] == {"id": 1, "type": "calendar/event/delete",
                                  "entity_id": "calendar.studio", "uid": "uid-9"}
    assert written[0]["status"] == "applied"
    assert "ref:AB-1207" not in cp._live(cp.state(journal))


def test_an_https_instance_is_reached_over_wss(tmp_path):
    fake = FakeHA()
    client = ha.HomeAssistant("https://ha.example.test", TOKEN, http=fake.http,
                              websocket=fake.websocket)
    client.command({"type": "calendar/event/delete", "entity_id": "calendar.studio",
                    "uid": "u"})
    assert fake.ws_sent[0][0] == "wss://ha.example.test/api/websocket"


@pytest.mark.parametrize("op", ["move", "delete"])
def test_a_move_or_delete_with_no_applied_uid_fails_and_sends_nothing(tmp_path, op):
    fake = FakeHA()
    written, _ = run_apply(fake, [planned(op, event=MOVED)], tmp_path)
    assert written[0]["status"] == "failed" and "nothing sent" in written[0]["reason"]
    assert fake.ws_sent == [] and fake.http_calls == []


def test_a_create_then_a_move_in_one_pass_moves_what_was_just_created(tmp_path):
    fake = FakeHA()
    written, _ = run_apply(fake, [planned("create"), planned("move", message_id="m2",
                                                             event=MOVED)], tmp_path)
    assert [w["status"] for w in written] == ["applied", "applied"]
    assert fake.ws_sent[1][1]["uid"] == "uid-1"


def test_a_refused_websocket_command_is_a_failed_row(tmp_path):
    written, _ = run_apply(FakeHA(ws_success=False),
                           [applied("create"), planned("delete", message_id="m3")], tmp_path)
    assert written[0]["status"] == "failed" and "Event not found" in written[0]["reason"]


def test_settled_rows_are_not_applied_again():
    rows = [planned("create"), {**planned("create"), "status": "failed"},
            planned("create", key="msg:m5", message_id="m5"),
            {**planned("none", key="msg:m6")}]
    assert [r["key"] for r in ha.pending(rows)] == ["msg:m5"]


def test_from_env_refuses_without_url_or_token(monkeypatch):
    monkeypatch.setenv("HA_URL", URL)
    with pytest.raises(ha.HomeAssistantError, match="HA_TOKEN"):
        ha.from_env()


def test_an_http_error_names_the_path_and_never_the_token(monkeypatch):
    import urllib.error

    def refuse(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(ha.urllib.request, "urlopen", refuse)
    with pytest.raises(ha.HomeAssistantError) as raised:
        ha.HomeAssistant(URL, TOKEN).events("calendar.studio", EVENT["start"], EVENT["end"])
    assert "HTTP 401" in str(raised.value) and TOKEN not in str(raised.value)
    assert "start=" not in str(raised.value)


# ── calendar-post ────────────────────────────────────────────────────────────

def run_post(monkeypatch, tmp_path, apply, rows):
    post = load_script("calendar_post")
    events = tmp_path / "events.jsonl"
    cp.append_rows(events, rows)
    paths = {"policy": "", "keymap": "", "events": str(events),
             "review": str(tmp_path / "review.jsonl")}
    pre = {"run_id": "run-1", "enabled": True, "apply": apply, "pending": [], "paths": paths}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"previous_outputs": {"calendar-pre": pre}})))
    post.main()
    return events


def test_a_dry_run_prints_the_plan_and_sends_and_appends_nothing(
    tmp_path, monkeypatch, capsys
):
    def no_client():
        raise AssertionError("a dry run built a client")

    monkeypatch.setattr(ha, "from_env", no_client)
    events = run_post(monkeypatch, tmp_path, False, [planned("create")])
    captured = capsys.readouterr()
    assert "would create calendar.studio ref:AB-1207" in captured.err
    out = json.loads(captured.out)
    assert out["dry_run"] is True and out["written"] == 0
    assert len(cp.read_rows(events)) == 1


def test_apply_writes_what_an_earlier_dry_run_planned(tmp_path, monkeypatch, capsys):
    fake = FakeHA()
    monkeypatch.setattr(ha, "from_env", fake.client)
    events = run_post(monkeypatch, tmp_path, True, [planned("create")])
    assert json.loads(capsys.readouterr().out)["written"] == 1
    assert cp.read_rows(events)[-1]["uid"] == "uid-1"


def test_apply_without_credentials_refuses_and_alerts(tmp_path, monkeypatch):
    alerted = []
    post = load_script("calendar_post")
    monkeypatch.setattr(post.alerts, "notify", alerted.append)
    events = tmp_path / "events.jsonl"
    cp.append_rows(events, [planned("create")])
    pre = {"run_id": "run-1", "enabled": True, "apply": True, "pending": [],
           "paths": {"policy": "", "keymap": "", "events": str(events),
                     "review": str(tmp_path / "review.jsonl")}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"previous_outputs": {"calendar-pre": pre}})))
    with pytest.raises(SystemExit, match="HA_URL and HA_TOKEN"):
        post.main()
    assert "1 planned event(s) wait" in alerted[0]
    assert len(cp.read_rows(events)) == 1


def test_a_failed_write_exits_non_zero_after_journaling_it(tmp_path, monkeypatch):
    monkeypatch.setattr(ha, "from_env", FakeHA(listed=[]).client)
    with pytest.raises(SystemExit, match="1 calendar write"):
        run_post(monkeypatch, tmp_path, True, [planned("create")])
    assert cp.read_rows(tmp_path / "events.jsonl")[-1]["status"] == "failed"


def test_the_calendar_child_gets_the_apply_flag_the_apply_child_gets():
    from tests.test_mailbox_full import load_pipeline
    stages = {s["call"]: s for s in load_pipeline("mailbox-full")["stages"]}
    assert stages["calendar"]["input"]["apply"] == stages["apply"]["input"]["apply"]
