"""Tests for the drafts arm of `mailbox-full` (STU-850, Gmail drafter STU-853).

The mailbox is a fake throughout — no msal/requests, no Google client, no
mailbox. Neither fake serves a send path, so a code path reaching for one would
crash.
"""

import base64
import importlib.util
import io
import json
import sys
from datetime import datetime
from email import message_from_bytes
from email.policy import default as email_policy
from pathlib import Path

import pytest
import yaml

from email_classifier import club_memory, cost, prompt_sources
from email_classifier.apply.gmail import GmailDrafter
from email_classifier.apply.journal import read_entries
from email_classifier.apply.outlook import OutlookDrafter
from email_classifier.orchestrate import drafts
from email_classifier.stage1.policy import load_policy
from tests.conftest import requires_gliner, stub_detector
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
STUDIO = deployment.ROOT / ".studio"


EMAIL = {
    "message_id": "m1", "thread_id": "t1", "from_address": "parent@example.ca",
    "subject": "Horaire des pratiques", "headers": {"From": "parent@example.ca"},
    "body_snippet": "Bonjour Marie, quel est l'horaire?",
    "date": "2026-06-12T08:15:00+00:00", "provider_category": None,
}


def load_script(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_stage(module, payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    module.main()
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def run_script_stage(module, ctx, monkeypatch, capsys):
    return run_script_stage_reported(module, ctx, monkeypatch, capsys)[0]


def run_script_stage_reported(module, ctx, monkeypatch, capsys):
    """The stage's output, plus what it said on stderr — where the skips are named."""
    return run_stage(module, {"additional_context": yaml.safe_dump(ctx)}, monkeypatch, capsys)


# The bodies the mailbox would serve for the pending ids (STU-852). "coût" sits
# past a preview cutoff the fixture's `body_snippet` stops at.
BODIES = {
    "m1": "Bonjour Marie,\n\nQuel est l'horaire?\n\nEt le coût de l'inscription?",
    "m2": "Bonjour,\n\nMême question pour le second courriel.",
}


class _Bodies:
    """Fake body fetcher: serves canned bodies, records what was asked for."""

    def __init__(self, bodies):
        self._bodies = bodies
        self.asked = []

    def fetch_bodies_by_ids(self, message_ids):
        self.asked.append(list(message_ids))
        return {message_id: self._bodies.get(message_id, "") for message_id in message_ids}


@pytest.fixture
def bodies():
    return _Bodies(BODIES)


class _Threads:
    """Fake thread reader (STU-941): serves canned turns, records what was asked for."""

    def __init__(self, history=None, last_sent=None):
        self.history = dict(history or {})
        self.last_sent = dict(last_sent or {})
        self.asked = []

    def fetch_thread_history(self, thread_id, exclude_message_id):
        self.asked.append((thread_id, exclude_message_id))
        return [dict(turn) for turn in self.history.get(thread_id, [])]

    def last_sent_at(self, thread_id):
        return self.last_sent.get(thread_id)


@pytest.fixture
def threads():
    return _Threads()


class _ExistingDrafts:
    """Fake drafter for `drafts-pre`: knows which threads already carry a draft."""

    def __init__(self, thread_ids=(), saved_at="2026-06-12T09:00:00Z"):
        self.thread_ids = set(thread_ids)
        self.saved_at = saved_at
        # Drafts that left the Drafts folder, by id: the message Graph still holds, or
        # None for a 404.
        self.left = {}

    def find_draft(self, thread_id):
        return f"draft_{thread_id}" if thread_id in self.thread_ids else None

    def get_draft(self, draft_id):
        if draft_id in self.left:
            return self.left[draft_id]
        return {"id": draft_id, "isDraft": True,
                "lastModifiedDateTime": self.saved_at}


@pytest.fixture
def existing_drafts():
    return _ExistingDrafts()


@pytest.fixture
def drafts_pre(request, monkeypatch, bodies, existing_drafts, threads):
    # What `drafts-pre` puts in the item is the subject of nearly every test here, not
    # who it finds in it. Stubbing the detector keeps them hermetic — they run on a
    # fresh clone without the `ner` extra. A test that does assert on a name asks for
    # the real one with `real_ner`, and skips when the extra is absent.
    if "real_ner" not in request.keywords:
        stub_detector(monkeypatch)
    module = load_script("drafts_pre")
    monkeypatch.setattr(module, "get_body_fetcher", lambda provider: bodies)
    monkeypatch.setattr(module, "get_drafter", lambda provider: existing_drafts)
    monkeypatch.setattr(module, "get_thread_reader", lambda provider: threads)
    return module


@pytest.fixture
def drafts_post(tmp_path):
    module = load_script("drafts_post")
    # Never the checkout's own `.studio/runs`: a real journal naming the run would
    # answer for the test.
    module.RUNS_DIR = tmp_path / "runs"
    return module


def write_policy(tmp_path, scope="full", provider="outlook"):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({
        "deterministic_can_finalize": True,
        "destructive_actions_allowed": ["mark_for_delete"],
        "frontier_log": {"file": True, "stdout": False},
        "email": {"provider": provider},
        "anonymization": {"scope": scope},
    }), encoding="utf-8")
    return path


def write_corpus(tmp_path, emails=(EMAIL,)):
    path = tmp_path / "dump.json"
    path.write_text(json.dumps(list(emails)), encoding="utf-8")
    return path


def write_report(tmp_path, actions=("needs_reply",), run_id="run-1", thread_id=None):
    verdicts = [{
        "message_id": f"m{i + 1}", "thread_id": thread_id or f"t{i + 1}",
        "path": "deterministic",
        "status": "ok", "bucket": "A", "suggested_action": action, "confidence": "high",
        "rule_id": "r1", "subtype": None, "reasoning": None, "error": None,
        "date": f"2026-06-{15 + i}T09:00:00+00:00",
    } for i, action in enumerate(actions)]
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"run_id": run_id, "verdicts": verdicts}), encoding="utf-8")
    return path


def pre_ctx(tmp_path, **overrides):
    """The default corpus/report/policy are only written when nothing overrides them —
    they share their file names with the overrides."""
    ctx = {
        "keymap": str(tmp_path / "keymap.json"),
        "draft_journal": str(tmp_path / "drafts.jsonl"),
        "stage_log": str(tmp_path / "stages.jsonl"),
        **overrides,
    }
    if "corpus" not in ctx:
        ctx["corpus"] = str(write_corpus(tmp_path))
    if "report" not in ctx:
        ctx["report"] = str(write_report(tmp_path))
    if "policy" not in ctx:
        ctx["policy"] = str(write_policy(tmp_path))
    return ctx


# ── the Graph drafter ────────────────────────────────────────────────────────

class _Session:
    """Fake GraphSession: a Drafts folder keyed by conversation."""

    def __init__(self, existing=None):
        self.drafts = dict(existing or {})  # draft id -> {conversationId, body}
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        if not url.endswith("/messages"):
            draft_id = url.rsplit("/", 1)[-1]
            if draft_id not in self.drafts:
                raise _NotFound()
            return {"id": draft_id, "isDraft": True,
                    "lastModifiedDateTime": "2026-08-20T10:00:00Z"}
        conversation = (params or {}).get("$filter", "").split("'")[1::2]
        return {"value": [
            {"id": draft_id} for draft_id, draft in self.drafts.items()
            if draft["conversationId"] in conversation
        ]}

    def request(self, method, url, params=None, payload=None):
        self.calls.append((method, url, payload))
        if method == "DELETE":
            del self.drafts[url.rsplit("/", 1)[-1]]
            return {}
        if url.endswith("/createReply"):
            draft_id = f"d{len(self.drafts) + 1}"
            self.drafts[draft_id] = {"conversationId": "t1", "body": ""}
            return {"id": draft_id}
        draft = self.drafts[url.rsplit("/", 1)[-1]]
        draft["body"] = payload["body"]["content"]
        if "toRecipients" in payload:
            draft["toRecipients"] = payload["toRecipients"]
        return {}


class _NotFound(Exception):
    """What Graph raises for an id that no longer resolves — a 404 carried on the
    HTTP response, which is all `get_draft` reads off it."""

    response = type("Response", (), {"status_code": 404})()


def test_a_draft_is_created_as_a_reply_and_never_sent():
    session = _Session()

    result = OutlookDrafter(session).upsert_reply_draft("m1", "t1", "Bonjour.")

    assert result == {"draft_id": "d1", "updated": False}
    assert session.drafts["d1"]["body"] == "Bonjour."
    assert not any("send" in url for _, url, _ in session.calls)


def test_a_second_draft_on_the_same_conversation_rewrites_the_first():
    session = _Session({"d1": {"conversationId": "t1", "body": "Première version."}})

    result = OutlookDrafter(session).upsert_reply_draft("m1", "t1", "Version corrigée.")

    assert result == {"draft_id": "d1", "updated": True}
    assert session.drafts == {"d1": {"conversationId": "t1", "body": "Version corrigée."}}
    assert not any("createReply" in url for _, url, _ in session.calls)


def test_a_draft_on_another_conversation_is_a_new_draft():
    session = _Session({"d1": {"conversationId": "t9", "body": "Autre fil."}})

    result = OutlookDrafter(session).upsert_reply_draft("m1", "t1", "Bonjour.")

    assert result["updated"] is False
    assert len(session.drafts) == 2


def test_a_reply_recipient_replaces_the_one_createreply_inferred():
    """STU-888: the contact form's envelope is a robot the parent never reads."""
    session = _Session()

    OutlookDrafter(session).upsert_reply_draft(
        "m1", "t1", "Bonjour.", "sylvie.lachance@videotron.ca", "Sylvie Lachance")

    assert session.drafts["d1"]["toRecipients"] == [
        {"emailAddress": {"address": "sylvie.lachance@videotron.ca",
                          "name": "Sylvie Lachance"}}
    ]
    # Still a reply: the override moves the address, not the conversation.
    assert any("createReply" in url for _, url, _ in session.calls)


def test_ordinary_mail_keeps_the_recipient_the_reply_came_with():
    session = _Session()

    OutlookDrafter(session).upsert_reply_draft("m1", "t1", "Bonjour.")

    assert "toRecipients" not in session.drafts["d1"]


def test_a_reply_recipient_with_no_name_is_still_addressed():
    session = _Session()

    OutlookDrafter(session).upsert_reply_draft("m1", "t1", "Bonjour.", "parent@videotron.ca")

    assert session.drafts["d1"]["toRecipients"] == [
        {"emailAddress": {"address": "parent@videotron.ca"}}
    ]


def test_a_draft_is_deleted_by_id():
    """STU-854: deletion is safe here — the run created the object it destroys."""
    session = _Session({"d1": {"conversationId": "t1", "body": "Bonjour."}})

    OutlookDrafter(session).delete_draft("d1")

    assert session.drafts == {}
    assert ("DELETE", "/me/messages/d1", None) in session.calls


def test_a_draft_a_human_already_removed_reads_as_gone_not_as_an_error():
    assert OutlookDrafter(_Session()).get_draft("d1") is None


def test_the_mailbox_reports_what_decides_whether_a_draft_may_be_deleted():
    session = _Session({"d1": {"conversationId": "t1", "body": "Bonjour."}})

    draft = OutlookDrafter(session).get_draft("d1")

    assert draft["isDraft"] is True and draft["lastModifiedDateTime"]


# ── the write stage ──────────────────────────────────────────────────────────

class FakeDrafter:
    def __init__(self, standing=None, modified="2026-09-19T20:00:00Z"):
        self.calls = []
        self.deleted = []
        self.standing = standing
        self.modified = modified

    def upsert_reply_draft(self, message_id, thread_id, body, reply_to=None, reply_name=None):
        self.calls.append((message_id, thread_id, body, reply_to, reply_name))
        return {"draft_id": "d1", "updated": False}

    def find_draft(self, thread_id):
        return self.standing

    def get_draft(self, draft_id):
        if draft_id in self.deleted:
            return None
        return {"id": draft_id, "isDraft": True, "lastModifiedDateTime": self.modified}

    def delete_draft(self, draft_id):
        self.deleted.append(draft_id)


@pytest.fixture
def write_draft(monkeypatch):
    module = load_script("write_draft")
    monkeypatch.setattr(module, "preflight_write", lambda provider: None)
    return module


def run_write(module, tmp_path, body, monkeypatch, capsys, **overrides):
    item = {"message_id": "m1", "thread_id": "t1",
            "keymap": str(tmp_path / "keymap.json"),
            "journal": str(tmp_path / "drafts.jsonl"),
            "run_id": overrides.get("run_id", "run-1"),
            "prompt_fingerprint": overrides.get("prompt_fingerprint", "fp-1"),
            "policy": str(overrides.get("policy") or write_policy(tmp_path)),
            "reply_to": overrides.get("reply_to", ""),
            "reply_name": overrides.get("reply_name", "")}
    draft = {"thread_id": "t1", "draft_body": body, "confidence": "high",
             "sources_used": ["club:inscriptions"]}
    payload = {"additional_context": yaml.safe_dump(item),
               "previous_outputs": {"generate-draft": draft}}
    return run_stage(module, payload, monkeypatch, capsys)[0]


def test_the_stage_restores_the_real_values_before_the_mailbox(
    write_draft, tmp_path, monkeypatch, capsys
):
    (tmp_path / "keymap.json").write_text(
        json.dumps({"PERSON_1": "Marie Tremblay"}), encoding="utf-8")
    drafter = FakeDrafter()
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    out = run_write(write_draft, tmp_path, "Bonjour PERSON_1,\nMerci.", monkeypatch, capsys)

    assert drafter.calls == [("m1", "t1", "Bonjour Marie Tremblay,\nMerci.", None, None)]
    assert out["draft_id"] == "d1"


def test_the_reviewer_reads_the_preimage_of_what_reached_the_mailbox(
    write_draft, tmp_path, monkeypatch, capsys
):
    """STU-1030: `draft-qa` judges this stage's output, so a claimed draft and a
    written one can no longer be two different texts — the reviewer's copy is the
    same string, before the keymap restores the names into it (STU-843)."""
    (tmp_path / "keymap.json").write_text(
        json.dumps({"PERSON_1": "Marie Tremblay"}), encoding="utf-8")
    drafter = FakeDrafter()
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    out = run_write(write_draft, tmp_path, "Bonjour PERSON_1.", monkeypatch, capsys)

    assert out["draft_body"] == "Bonjour PERSON_1."
    assert drafter.calls[0][2] == "Bonjour Marie Tremblay."
    assert out["sources_used"] == ["club:inscriptions"]


def test_the_stage_restores_the_parsed_recipient_before_the_mailbox(
    write_draft, tmp_path, monkeypatch, capsys
):
    """The address reaches the stage tokenized, like everything else (STU-888)."""
    (tmp_path / "keymap.json").write_text(
        json.dumps({"EMAIL_1": "sylvie.lachance@videotron.ca", "PERSON_1": "Sylvie Lachance"}),
        encoding="utf-8")
    drafter = FakeDrafter()
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    run_write(write_draft, tmp_path, "Bonjour PERSON_1.", monkeypatch, capsys,
              reply_to="EMAIL_1", reply_name="PERSON_1")

    assert drafter.calls == [
        ("m1", "t1", "Bonjour Sylvie Lachance.",
         "sylvie.lachance@videotron.ca", "Sylvie Lachance")
    ]


def test_the_stage_journals_the_draft_it_created(write_draft, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: FakeDrafter())

    run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)

    entries = read_entries(tmp_path / "drafts.jsonl")
    assert [(e["message_id"], e["draft_id"], e["status"]) for e in entries] == [
        ("m1", "d1", "ok")
    ]


def test_a_draft_the_journal_does_not_name_is_left_to_its_human(
    write_draft, tmp_path, monkeypatch, capsys
):
    """STU-1548, run `4ed439fb`: the reviewer's own forward of the parent's message was
    rewritten with our reply 29 seconds before they sent it."""
    drafter = FakeDrafter(standing="human-draft")
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    with pytest.raises(SystemExit, match="human-draft"):
        run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)

    assert drafter.calls == []
    entries = read_entries(tmp_path / "drafts.jsonl")
    assert [(e["status"], e["draft_id"]) for e in entries] == [("held", "human-draft")]
    assert drafts.vanished_draft_id(entries, "t1") is None


def test_a_draft_of_ours_nobody_touched_is_rewritten(
    write_draft, tmp_path, monkeypatch, capsys
):
    journal_rows(tmp_path, [{"run_id": "run-0", "message_id": "m1", "thread_id": "t1",
                             "draft_id": "d1", "status": "ok",
                             "timestamp": "2026-09-19T20:00:00+00:00"}])
    drafter = FakeDrafter(standing="d1", modified="2026-09-19T20:00:30Z")
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)

    assert len(drafter.calls) == 1


def test_a_draft_of_ours_a_human_edited_since_is_left_alone(
    write_draft, tmp_path, monkeypatch, capsys
):
    journal_rows(tmp_path, [{"run_id": "run-0", "message_id": "m1", "thread_id": "t1",
                             "draft_id": "d1", "status": "ok",
                             "timestamp": "2026-09-19T20:00:00+00:00"}])
    drafter = FakeDrafter(standing="d1", modified="2026-09-19T20:30:00Z")
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    with pytest.raises(SystemExit):
        run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)

    assert drafter.calls == []


def journal_rows(tmp_path, rows):
    (tmp_path / "drafts.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_a_drafting_attempt_writes_to_the_mailbox_exactly_once(
    write_draft, tmp_path, monkeypatch, capsys
):
    """STU-1030: the agent held the write and fired it on every revision — 28 mailbox
    writes for 5 attempts on run 2498b0b3. One stage run is now one Graph write, so a
    journal line counts an attempt rather than a draft the model changed its mind about."""
    drafter = FakeDrafter()
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)

    assert len(drafter.calls) == 1
    assert len(read_entries(tmp_path / "drafts.jsonl")) == 1


def test_every_journal_line_names_the_run_that_wrote_it(
    write_draft, tmp_path, monkeypatch, capsys
):
    """STU-917: two runs over the same mailbox share their `needs_reply` set, so a
    message id alone hands the first run's drafts to the second."""
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: FakeDrafter())

    run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys, run_id="run-1")
    run_write(write_draft, tmp_path, "Bonjour encore.", monkeypatch, capsys, run_id="run-2")

    entries = read_entries(tmp_path / "drafts.jsonl")
    assert [(e["run_id"], e["status"]) for e in entries] == [("run-1", "ok"), ("run-2", "ok")]


def test_a_redraft_journals_no_run_because_it_answers_a_human(
    write_draft, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: FakeDrafter())

    run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys, run_id="")

    assert read_entries(tmp_path / "drafts.jsonl")[0]["run_id"] is None


def test_a_re_draft_is_journaled_as_an_update_of_the_same_draft(
    write_draft, tmp_path, monkeypatch, capsys
):
    """STU-894: the group's second iteration overwrites; the journal has to say so."""
    class Upserting(FakeDrafter):
        def upsert_reply_draft(self, *args, **kwargs):
            result = super().upsert_reply_draft(*args, **kwargs)
            return {**result, "updated": len(self.calls) > 1}

    drafter = Upserting()
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)
    run_write(write_draft, tmp_path, "Bonjour, corrige.", monkeypatch, capsys)

    entries = read_entries(tmp_path / "drafts.jsonl")
    assert [(e["draft_id"], e["updated"]) for e in entries] == [("d1", False), ("d1", True)]


def test_the_journal_row_names_the_prompt_the_draft_was_written_under(
    write_draft, tmp_path, monkeypatch, capsys
):
    """STU-886: the next run rewrites a draft of ours whose prompt is no longer today's,
    and the row that made it is the only place that answer can come from."""
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: FakeDrafter())

    run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys,
              prompt_fingerprint="fp-old")

    assert [e["prompt_fingerprint"] for e in read_entries(tmp_path / "drafts.jsonl")] == [
        "fp-old"
    ]


def test_a_failed_draft_is_journaled_and_fails_the_stage(
    write_draft, tmp_path, monkeypatch, capsys
):
    def explode(provider):
        raise RuntimeError("graph 503")

    monkeypatch.setattr(write_draft, "get_drafter", explode)

    with pytest.raises(RuntimeError):
        run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)

    entries = read_entries(tmp_path / "drafts.jsonl")
    assert [(e["status"], e["draft_id"]) for e in entries] == [("error", None)]


def test_the_stage_refuses_an_empty_body(write_draft, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: FakeDrafter())

    with pytest.raises(SystemExit):
        run_write(write_draft, tmp_path, "   \n", monkeypatch, capsys)


def test_the_stage_refuses_to_draft_without_a_write_grant(
    write_draft, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(write_draft, "preflight_write", lambda provider: "no Graph scopes cached")
    drafter = FakeDrafter()
    monkeypatch.setattr(write_draft, "get_drafter", lambda provider: drafter)

    with pytest.raises(SystemExit):
        run_write(write_draft, tmp_path, "Bonjour.", monkeypatch, capsys)

    assert drafter.calls == []


# ── the Gmail drafter ────────────────────────────────────────────────────────

SOURCE_HEADERS = [
    {"name": "From", "value": "Parent Tremblay <parent@example.ca>"},
    {"name": "Subject", "value": "Horaire des pratiques"},
    {"name": "Message-ID", "value": "<abc@mail.example.ca>"},
]


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _GmailDrafts:
    """Fake drafts collection. No `send` method: a send path would crash."""

    def __init__(self, existing=None, page_size=500):
        self.store = dict(existing or {})  # draft id -> {threadId, raw}
        self.created = []
        self.updated = []
        self._page_size = page_size

    def list(self, *, userId, maxResults, pageToken):
        items = [
            {"id": draft_id, "message": {"threadId": draft["threadId"]}}
            for draft_id, draft in self.store.items()
        ]
        start = int(pageToken or 0)
        end = start + self._page_size
        page = {"drafts": items[start:end]}
        if end < len(items):
            page["nextPageToken"] = str(end)
        return _Exec(page)

    def create(self, *, userId, body):
        draft_id = f"d{len(self.store) + 1}"
        self.store[draft_id] = dict(body["message"])
        self.created.append(draft_id)
        return _Exec({"id": draft_id})

    def update(self, *, userId, id, body):
        self.store[id] = dict(body["message"])
        self.updated.append(id)
        return _Exec({"id": id})

    def get(self, *, userId, id, format):
        if id not in self.store:
            raise _Gone()
        return _Exec({"id": id, "message": {"internalDate": "1787220000000"}})

    def delete(self, *, userId, id):
        del self.store[id]
        return _Exec(None)


class _Gone(Exception):
    """What the Google client raises for a draft that no longer resolves — a 404 on
    `resp`, which is all `get_draft` reads off it."""

    resp = type("Response", (), {"status": 404})()


class _GmailMessages:
    def __init__(self, headers):
        self._headers = headers

    def get(self, *, userId, id, format, metadataHeaders):
        return _Exec({"payload": {"headers": self._headers}})


class _GmailService:
    def __init__(self, drafts, headers=SOURCE_HEADERS):
        self._drafts = drafts
        self._messages = _GmailMessages(headers)

    def users(self):
        return self

    def drafts(self):
        return self._drafts

    def messages(self):
        return self._messages


def sent_message(drafts, draft_id):
    """Decode a stored draft back into a parsed MIME message."""
    return message_from_bytes(
        base64.urlsafe_b64decode(drafts.store[draft_id]["raw"].encode()),
        policy=email_policy,
    )


def test_a_gmail_draft_replies_in_the_thread_and_is_never_sent():
    drafts_api = _GmailDrafts()

    result = GmailDrafter(_GmailService(drafts_api)).upsert_reply_draft(
        "m1", "t1", "Bonjour."
    )

    assert result == {"draft_id": "d1", "updated": False}
    assert drafts_api.store["d1"]["threadId"] == "t1"
    message = sent_message(drafts_api, "d1")
    assert message["To"] == "Parent Tremblay <parent@example.ca>"
    assert message["Subject"] == "Re: Horaire des pratiques"
    assert message["In-Reply-To"] == "<abc@mail.example.ca>"
    assert message.get_content().strip() == "Bonjour."
    assert not hasattr(drafts_api, "send")


def test_a_second_gmail_draft_on_the_same_thread_rewrites_the_first():
    drafts_api = _GmailDrafts({"d1": {"threadId": "t1", "raw": ""}})

    result = GmailDrafter(_GmailService(drafts_api)).upsert_reply_draft(
        "m1", "t1", "Version corrigée."
    )

    assert result == {"draft_id": "d1", "updated": True}
    assert (drafts_api.created, drafts_api.updated) == ([], ["d1"])
    assert sent_message(drafts_api, "d1").get_content().strip() == "Version corrigée."


def test_a_gmail_draft_on_another_thread_is_a_new_draft():
    drafts_api = _GmailDrafts({"d1": {"threadId": "t9", "raw": ""}})

    result = GmailDrafter(_GmailService(drafts_api)).upsert_reply_draft(
        "m1", "t1", "Bonjour."
    )

    assert result["updated"] is False
    assert len(drafts_api.store) == 2


def test_a_draft_past_the_first_page_is_still_found():
    drafts_api = _GmailDrafts(
        {f"d{i}": {"threadId": f"t{i}", "raw": ""} for i in range(1, 6)}, page_size=2
    )

    result = GmailDrafter(_GmailService(drafts_api)).upsert_reply_draft(
        "m5", "t5", "Bonjour."
    )

    assert result == {"draft_id": "d5", "updated": True}


def test_an_already_prefixed_subject_is_not_prefixed_twice():
    headers = [{"name": "Subject", "value": "Re: Horaire des pratiques"}]
    drafts_api = _GmailDrafts()

    GmailDrafter(_GmailService(drafts_api, headers)).upsert_reply_draft(
        "m1", "t1", "Bonjour."
    )

    assert sent_message(drafts_api, "d1")["Subject"] == "Re: Horaire des pratiques"


def test_a_parsed_recipient_wins_over_the_reply_to_header():
    """STU-888: a contact form's `Reply-To` is still the robot's."""
    headers = [*SOURCE_HEADERS, {"name": "Reply-To", "value": "noreply@club.example"}]
    drafts_api = _GmailDrafts()

    GmailDrafter(_GmailService(drafts_api, headers)).upsert_reply_draft(
        "m1", "t1", "Bonjour.", "sylvie.lachance@videotron.ca", "Sylvie Lachance"
    )

    assert sent_message(drafts_api, "d1")["To"] == (
        "Sylvie Lachance <sylvie.lachance@videotron.ca>"
    )


def test_a_reply_to_header_wins_over_the_sender():
    headers = [*SOURCE_HEADERS, {"name": "Reply-To", "value": "info@example.ca"}]
    drafts_api = _GmailDrafts()

    GmailDrafter(_GmailService(drafts_api, headers)).upsert_reply_draft(
        "m1", "t1", "Bonjour."
    )

    assert sent_message(drafts_api, "d1")["To"] == "info@example.ca"


def test_a_gmail_draft_is_deleted_by_id():
    drafts_api = _GmailDrafts({"d1": {"threadId": "t1", "raw": ""}})

    GmailDrafter(_GmailService(drafts_api)).delete_draft("d1")

    assert drafts_api.store == {}


def test_a_gmail_draft_reports_its_last_save_in_the_neutral_shape():
    """STU-854: `undo` compares this against its journal stamp, so both providers
    must answer in the same shape."""
    drafts_api = _GmailDrafts({"d1": {"threadId": "t1", "raw": ""}})

    draft = GmailDrafter(_GmailService(drafts_api)).get_draft("d1")

    assert draft == {"id": "d1", "isDraft": True,
                     "lastModifiedDateTime": "2026-08-20T10:00:00+00:00"}


def test_a_gmail_draft_a_human_already_removed_reads_as_gone():
    assert GmailDrafter(_GmailService(_GmailDrafts())).get_draft("d1") is None


# ── the pre stage ────────────────────────────────────────────────────────────

def test_only_needs_reply_verdicts_are_drafted(drafts_pre, tmp_path, monkeypatch, capsys):
    corpus = write_corpus(tmp_path, [EMAIL, {**EMAIL, "message_id": "m2", "thread_id": "t2"}])
    report = write_report(tmp_path, actions=("label_archive", "needs_reply"))

    out = run_script_stage(
        drafts_pre, pre_ctx(tmp_path, corpus=str(corpus), report=str(report)),
        monkeypatch, capsys,
    )

    assert out["needs_draft"] is True
    assert [item["message_id"] for item in out["pending"]] == ["m2"]


def test_a_conversation_owing_a_reply_is_drafted_once_on_its_latest_message(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """A draft is a mailbox outcome, so it takes the conversation as its unit (STU-940).
    Two messages of one thread each asking for a reply are one reply."""
    corpus = write_corpus(tmp_path, [
        {**EMAIL, "message_id": "m1", "thread_id": "t1", "date": "2026-06-15T09:00:00+00:00"},
        {**EMAIL, "message_id": "m2", "thread_id": "t1", "date": "2026-06-16T09:00:00+00:00"},
    ])
    report = write_report(tmp_path, actions=("needs_reply", "needs_reply"), thread_id="t1")

    out = run_script_stage(
        drafts_pre, pre_ctx(tmp_path, corpus=str(corpus), report=str(report)),
        monkeypatch, capsys,
    )

    assert [item["message_id"] for item in out["pending"]] == ["m2"]


def test_a_conversation_closed_by_its_latest_message_is_not_drafted(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """`remerciement_selections`: the question was answered and the parent said merci.
    Read per message, the opener still owes a reply forever."""
    corpus = write_corpus(tmp_path, [
        {**EMAIL, "message_id": "m1", "thread_id": "t1", "date": "2026-06-15T09:00:00+00:00"},
        {**EMAIL, "message_id": "m2", "thread_id": "t1", "date": "2026-06-16T09:00:00+00:00"},
    ])
    report = write_report(
        tmp_path, actions=("needs_reply", "keep_visible"), thread_id="t1"
    )

    ctx = pre_ctx(tmp_path, corpus=str(corpus), report=str(report))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"additional_context": yaml.safe_dump(ctx)}))
    )
    drafts_pre.main()
    captured = capsys.readouterr()

    assert json.loads(captured.out)["needs_draft"] is False
    assert json.loads(captured.out)["pending"] == []
    assert "held" not in captured.err  # a closed thread is a decision, not a drop


def _held_report(tmp_path, patch):
    report = json.loads(
        write_report(tmp_path, actions=("needs_reply", "needs_reply"), thread_id="t1")
        .read_text(encoding="utf-8")
    )
    report["verdicts"][1] |= patch
    path = tmp_path / "held.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "error", "bucket": None, "suggested_action": None},
        {"bucket": "C", "suggested_action": "label_archive"},
    ],
    ids=["conversation not decided whole", "clamped by the reconciled bucket"],
)
def test_a_reply_the_conversation_drops_is_named(
    drafts_pre, tmp_path, monkeypatch, capsys, patch
):
    """Two ways to lose a reply with nobody deciding nothing was owed. Apply names the
    same conversations; a mail silently left unanswered is worse than one left unmoved."""
    corpus = write_corpus(tmp_path, [
        {**EMAIL, "message_id": "m1", "thread_id": "t1", "date": "2026-06-15T09:00:00+00:00"},
        {**EMAIL, "message_id": "m2", "thread_id": "t1", "date": "2026-06-16T09:00:00+00:00"},
    ])

    ctx = pre_ctx(tmp_path, corpus=str(corpus), report=str(_held_report(tmp_path, patch)))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"additional_context": yaml.safe_dump(ctx)}))
    )
    drafts_pre.main()
    captured = capsys.readouterr()

    assert json.loads(captured.out)["pending"] == []
    assert "m1" in captured.err


def test_a_conversation_that_already_holds_a_draft_is_skipped(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """A reply already waiting for a human is not rewritten by the next run."""
    corpus = write_corpus(tmp_path, [EMAIL, {**EMAIL, "message_id": "m2", "thread_id": "t2"}])
    report = write_report(tmp_path, actions=("needs_reply", "needs_reply"))
    existing_drafts.thread_ids = {"t1"}

    out = run_script_stage(
        drafts_pre, pre_ctx(tmp_path, corpus=str(corpus), report=str(report)),
        monkeypatch, capsys,
    )

    assert [item["message_id"] for item in out["pending"]] == ["m2"]


def write_journal(tmp_path, rows):
    path = tmp_path / "drafts.jsonl"
    path.write_text(
        # The stamp defaults to the moment `_ExistingDrafts` saved the draft: an
        # untouched draft is never newer than the row that wrote it (STU-886).
        "".join(json.dumps({"run_id": "run-0", "message_id": "m1", "thread_id": "t1",
                            "timestamp": "2026-06-12T09:00:00+00:00",
                            "updated": False, "error": None, **row}) + "\n"
                for row in rows),
        encoding="utf-8",
    )
    return path


def test_a_draft_that_vanished_between_runs_is_named(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """STU-998: nobody here deleted it, so the conversation is drafted again — and the
    journal says why, which two draft ids across runs cannot."""
    write_journal(tmp_path, [{"draft_id": "d1", "status": "ok"}])

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert [item["message_id"] for item in out["pending"]] == ["m1"]
    assert "m1: vanished d1" in reported
    assert [(e["status"], e["draft_id"]) for e in read_entries(tmp_path / "drafts.jsonl")] == [
        ("ok", "d1"), ("vanished", "d1")
    ]


def test_a_draft_this_pipeline_deleted_is_not_reported_as_vanished(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """`undo` and `redraft` journal every deletion of ours, so the loss is accounted
    for and the next draft is the ordinary one."""
    write_journal(tmp_path, [{"draft_id": "d1", "status": "ok"},
                             {"draft_id": "d1", "status": "deleted"}])

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert [item["message_id"] for item in out["pending"]] == ["m1"]
    assert "vanished" not in reported


def test_a_loss_already_named_is_not_named_again():
    """STU-1028: a `vanished` row carries a draft id, so left to count as the thread's
    last word it makes every later run report the same loss over again."""
    entries = [{"thread_id": "t1", "draft_id": "d1", "status": "ok"},
               {"thread_id": "t1", "draft_id": "d1", "status": "vanished"}]

    assert drafts.vanished_draft_id(entries, "t1") is None


def test_a_draft_made_and_lost_after_that_is_named_in_turn():
    """The row is silenced by the report already made, not by the thread's history:
    a second loss is a second draft gone, and an audit has to see it."""
    entries = [{"thread_id": "t1", "draft_id": "d1", "status": "ok"},
               {"thread_id": "t1", "draft_id": "d1", "status": "vanished"},
               {"thread_id": "t1", "draft_id": "d2", "status": "ok"}]

    assert drafts.vanished_draft_id(entries, "t1") == "d2"


def test_a_run_that_fails_to_replace_a_vanished_draft_adds_no_row(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """The stage twice over one loss: `drafts` is the stage `mailbox-full` tolerates in
    failure (STU-850), so the run that named the loss routinely dies before replacing
    the draft. A count of `vanished` rows has to stay a count of lost drafts."""
    write_journal(tmp_path, [{"draft_id": "d1", "status": "ok"}])

    run_script_stage_reported(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)
    after_first = read_entries(tmp_path / "drafts.jsonl")
    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert [item["message_id"] for item in out["pending"]] == ["m1"]
    assert "vanished" not in reported
    assert read_entries(tmp_path / "drafts.jsonl") == after_first


def test_a_sent_draft_is_journaled_sent_and_a_deleted_one_vanished(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """STU-1549: leaving Drafts is what sending does too, and on the serving host it was 19 of
    22 `vanished` rows. Only a draft the mailbox no longer holds is lost; neither row
    is written again by the run after."""
    corpus = write_corpus(tmp_path, [EMAIL, {**EMAIL, "message_id": "m2", "thread_id": "t2"}])
    report = write_report(tmp_path, actions=("needs_reply", "needs_reply"))
    write_journal(tmp_path, [{"draft_id": "d1", "status": "ok"},
                             {"message_id": "m2", "thread_id": "t2",
                              "draft_id": "d2", "status": "ok"}])
    existing_drafts.left = {
        "d1": {"id": "d1", "isDraft": False, "lastModifiedDateTime": "2026-06-12T10:00:00Z",
               "sentDateTime": "2026-06-12T10:00:00Z"},
        "d2": None,
    }
    ctx = pre_ctx(tmp_path, corpus=str(corpus), report=str(report))

    _, reported = run_script_stage_reported(drafts_pre, ctx, monkeypatch, capsys)
    after_first = read_entries(tmp_path / "drafts.jsonl")
    out, again = run_script_stage_reported(drafts_pre, ctx, monkeypatch, capsys)

    assert "m1: sent d1" in reported and "m2: vanished d2" in reported
    assert [(e["status"], e["draft_id"], e.get("sent_at")) for e in after_first[2:]] == [
        ("sent", "d1", "2026-06-12T10:00:00Z"), ("vanished", "d2", None)]
    assert "m1: sent" not in again and "vanished" not in again
    assert read_entries(tmp_path / "drafts.jsonl") == after_first
    assert [item["message_id"] for item in out["pending"]] == ["m1", "m2"]


def test_a_message_that_is_no_longer_a_draft_but_names_no_send_is_vanished():
    assert drafts.sent_at({"id": "d1", "isDraft": False,
                           "lastModifiedDateTime": "2026-06-12T10:00:00Z"}) is None
    assert drafts.sent_at({"id": "d1", "isDraft": True, "sentDateTime": None,
                           "lastModifiedDateTime": "2026-06-12T10:00:00Z"}) is None
    assert drafts.sent_at(None) is None


def test_a_conversation_nobody_has_drafted_reports_nothing_vanished(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert [item["message_id"] for item in out["pending"]] == ["m1"]
    assert "vanished" not in reported


def test_new_mail_after_a_sent_reply_is_drafted_again(
    drafts_pre, threads, tmp_path, monkeypatch, capsys
):
    """STU-945: the club answered the thread's earlier turn, then this mail arrived."""
    threads.last_sent = {"t1": "2026-06-11T14:00:00+00:00"}

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert [item["message_id"] for item in out["pending"]] == ["m1"]


def test_a_thread_the_club_already_answered_is_not_answered_twice(
    drafts_pre, threads, tmp_path, monkeypatch, capsys
):
    """STU-945: no draft, because the reply was written and sent. The mail keeps
    its `needs_reply` verdict, and drafting it again would answer it twice."""
    threads.last_sent = {"t1": "2026-06-12T09:00:00+00:00"}

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert out["needs_draft"] is False
    assert "m1: answered" in reported


def test_a_draft_a_human_replied_past_is_reported_not_rewritten(
    drafts_pre, existing_drafts, threads, tmp_path, monkeypatch, capsys
):
    """STU-945: the human answered from the mailbox while our draft sat there.

    The draft is stale, but it is not ours to rewrite or delete on that basis —
    the run says so in the journal and leaves it where it is.
    """
    existing_drafts.thread_ids = {"t1"}
    threads.last_sent = {"t1": "2026-06-12T11:00:00+00:00"}

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert out["needs_draft"] is False
    assert "m1: superseded" in reported
    entries = read_entries(tmp_path / "drafts.jsonl")
    assert [(e["message_id"], e["draft_id"], e["status"]) for e in entries] == [
        ("m1", "draft_t1", "superseded")
    ]


def test_a_mailbox_already_fully_drafted_leaves_nothing_to_do(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    existing_drafts.thread_ids = {"t1"}

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert out["needs_draft"] is False
    assert out["pending"] == []


# ── a corrected prompt reaches a draft already written (STU-886) ─────────────

def current_fingerprint(tmp_path):
    """Today's prompt fingerprint — what `write-draft` journals on a draft it writes.

    Derived the way `drafts-pre` derives it rather than pinned: a fixture value would
    make the "unchanged prompt" test pass under a fingerprint nothing produces.
    """
    return drafts.prompt_fingerprint(
        STUDIO, load_policy(write_policy(tmp_path)).anonymization_fields)


def ours(tmp_path, **row):
    """The mailbox holds a draft this pipeline wrote, described by that row."""
    write_journal(tmp_path, [{"draft_id": "draft_t1", "status": "ok", **row}])


def test_a_draft_written_under_todays_prompt_is_left_alone(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """Nothing the drafter reads has changed, so the draft still says what this repo
    would say — and rewriting it would spend a drafting group to say it again."""
    existing_drafts.thread_ids = {"t1"}
    ours(tmp_path, prompt_fingerprint=current_fingerprint(tmp_path))

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert out["pending"] == []
    assert "m1: blocked" in reported


def test_a_draft_written_before_a_prompt_correction_is_drafted_again(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """The failure this exists for: a fee fixed in the facts skill reaches
    every pending reply, and a draft held behind `blocked` is never in the fan-out to
    notice the fingerprint moved."""
    existing_drafts.thread_ids = {"t1"}
    ours(tmp_path, prompt_fingerprint="fp-before-the-fix")

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert [item["message_id"] for item in out["pending"]] == ["m1"]
    assert "m1: redraft" in reported


def test_a_draft_a_human_edited_is_left_alone_whatever_the_prompt_says(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """They opened it and started editing. Destroying that is worse than a stale draft
    they can still see, so the edit test runs before the fingerprint does."""
    existing_drafts.thread_ids = {"t1"}
    existing_drafts.saved_at = "2026-06-12T14:00:00Z"
    ours(tmp_path, prompt_fingerprint="fp-before-the-fix")

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert out["pending"] == []
    assert "m1: blocked" in reported


def test_a_draft_no_write_of_ours_claims_is_a_humans(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """`superseded` and `vanished` rows carry a draft id too, and neither composed the
    draft it names — only an `ok` row is a write of ours."""
    existing_drafts.thread_ids = {"t1"}
    write_journal(tmp_path, [{"draft_id": "draft_t1", "status": "superseded"}])

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert out["pending"] == []


def test_a_draft_answers_for_the_prompt_it_was_last_rewritten_under(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """`upsert_reply_draft` rewrites in place, so one draft id collects a row per
    attempt — the first says what the body no longer says."""
    existing_drafts.thread_ids = {"t1"}
    write_journal(tmp_path, [
        {"draft_id": "draft_t1", "status": "ok", "prompt_fingerprint": "fp-before"},
        {"draft_id": "draft_t1", "status": "ok",
         "prompt_fingerprint": current_fingerprint(tmp_path)},
    ])

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert out["pending"] == []


def test_a_draft_from_before_the_field_existed_is_drafted_again(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """The one answer this cannot measure. Reading a nameless row as today's would
    freeze exactly the drafts this unfreezes; reading it as older costs one re-draft,
    once, and cannot touch a draft a human owns."""
    existing_drafts.thread_ids = {"t1"}
    ours(tmp_path)

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert [item["message_id"] for item in out["pending"]] == ["m1"]


def test_a_re_drafted_conversation_reports_nothing_vanished(
    drafts_pre, existing_drafts, tmp_path, monkeypatch, capsys
):
    """The draft is right there, about to be rewritten in place. Naming it lost would
    stop the `vanished` count being a count of lost drafts (STU-1028)."""
    existing_drafts.thread_ids = {"t1"}
    ours(tmp_path, prompt_fingerprint="fp-before-the-fix")

    out, reported = run_script_stage_reported(
        drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys
    )

    assert [item["message_id"] for item in out["pending"]] == ["m1"]
    assert "vanished" not in reported
    assert [e["status"] for e in read_entries(tmp_path / "drafts.jsonl")] == ["ok"]


def test_an_errored_verdict_is_never_drafted(drafts_pre, tmp_path, monkeypatch, capsys):
    report = json.loads(write_report(tmp_path).read_text(encoding="utf-8"))
    report["verdicts"][0]["status"] = "error"
    path = tmp_path / "errored.json"
    path.write_text(json.dumps(report), encoding="utf-8")

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path, report=str(path)), monkeypatch, capsys)

    assert out["needs_draft"] is False
    assert out["pending"] == []


def test_the_item_carries_what_the_tool_needs_and_the_agent_must_not_choose(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    item = out["pending"][0]
    assert item["message_id"] == "m1"
    assert item["thread_id"] == "t1"
    assert item["keymap"] == str(tmp_path / "keymap.json")
    assert item["journal"] == str(tmp_path / "drafts.jsonl")
    assert item["run_id"] == "run-1"


@pytest.mark.parametrize("utc, expected", [
    # Summer, UTC-4: the cut lands at 20:00Z.
    ("2026-08-30T19:59:00+00:00", drafts.DAY),
    ("2026-08-30T20:00:00+00:00", drafts.EVENING),
    # Winter, UTC-5: the same local 16h is 21:00Z. An implementation that read the
    # hour off UTC would file this evening mail under `day` for half the year.
    ("2026-01-15T20:59:00+00:00", drafts.DAY),
    ("2026-01-15T21:00:00+00:00", drafts.EVENING),
])
def test_the_wish_turns_at_16h_in_the_clubs_timezone(utc, expected, monkeypatch):
    """STU-1119: 16h local calls 366 of the archive's 404 closing wishes right.

    The cutoff is the deployment's own measurement (`rules/voice.yaml`), pinned here to
    16h so what is asserted is the local-time conversion, not one archive."""
    monkeypatch.setattr(drafts, "EVENING_FROM_HOUR", 16)
    assert drafts.period_at(datetime.fromisoformat(utc)) == expected


def test_no_measured_cutoff_never_guesses_a_split(monkeypatch):
    """STU-1472: a deployment with no archive has no basis for the split, so every
    draft is `day` rather than a fabricated evening."""
    monkeypatch.setattr(drafts, "EVENING_FROM_HOUR", None)
    assert drafts.period_at(datetime.fromisoformat("2026-08-30T23:59:00+00:00")) == drafts.DAY


def test_the_item_says_which_half_of_the_day_it_was_written_in(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """STU-1119: the drafter cannot read a clock, so the run reads it for them."""
    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert out["pending"][0]["written_period"] in (drafts.DAY, drafts.EVENING)


def test_the_drafts_stage_adds_to_the_run_keymap_classify_batch_started(
    drafts_pre, bodies, tmp_path, monkeypatch, capsys
):
    """STU-924: `mailbox-full` tokenizes twice under one run id, a minute apart. The
    second stage seeds from the shared keymap, so it counts the first's entries as
    carried in — overwriting would lose them and undercount the run."""
    bodies._bodies = {"m1": "Bonjour, écrire à parent@exemple.ca."}
    run_keymap = tmp_path / "run-1.json"
    run_keymap.write_text(json.dumps({"EMAIL_1": "classifie@exemple.ca"}), encoding="utf-8")
    (tmp_path / "keymap.json").write_text(
        json.dumps({"EMAIL_1": "classifie@exemple.ca"}), encoding="utf-8")

    run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    minted = json.loads(run_keymap.read_text(encoding="utf-8"))
    assert "classifie@exemple.ca" in minted.values()
    assert "parent@exemple.ca" in minted.values()


def test_ordinary_mail_carries_no_reply_override(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert out["pending"][0]["reply_to"] == ""
    assert out["pending"][0]["reply_name"] == ""


HISTORY = [
    {"date": "2026-06-10T08:00:00+00:00", "direction": "received",
     "body": "Bonjour, quel est l'horaire? — parent@example.ca"},
    {"date": "2026-06-10T18:00:00+00:00", "direction": "sent",
     "body": "Les pratiques sont le mardi."},
]


def test_the_item_carries_the_conversations_earlier_turns(
    drafts_pre, threads, tmp_path, monkeypatch, capsys
):
    """STU-941: a follow-up is unanswerable without the reply it follows."""
    threads.history = {"t1": HISTORY}

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    history = out["pending"][0]["thread_history"]
    assert [turn["direction"] for turn in history] == ["received", "sent"]
    assert history[1]["body"] == "Les pratiques sont le mardi."
    # The mail being answered is `body_text`; asking for it twice only spends tokens.
    assert threads.asked == [("t1", "m1")]


def test_a_turn_is_tokenized_with_the_batch_keymap_not_flattened(
    drafts_pre, threads, tmp_path, monkeypatch, capsys
):
    """Unlike a precedent, this is the same exchange: the sender keeps their token."""
    threads.history = {"t1": HISTORY}

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    item = out["pending"][0]
    assert "parent@example.ca" not in json.dumps(item)
    assert item["from_address"] in item["thread_history"][0]["body"]


def test_anonymization_off_leaves_the_turns_in_the_clear(
    drafts_pre, threads, tmp_path, monkeypatch, capsys
):
    threads.history = {"t1": HISTORY}
    ctx = pre_ctx(tmp_path, policy=str(write_policy(tmp_path, scope=None)))

    out = run_script_stage(drafts_pre, ctx, monkeypatch, capsys)

    assert out["pending"][0]["thread_history"] == HISTORY


def test_the_item_carries_the_memory_index_and_not_the_memories(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """The index is what reaches the prompt; a file costs a tool call (STU-873)."""
    memory = tmp_path / "club-memory"
    club_memory.remember(memory, "u15-practice-gym", "U15 practise at Leblanc",
                         "PERSON_1", "The U15 girls practise at école Leblanc.")
    monkeypatch.setattr(club_memory, "MEMORY_DIR", memory)

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    item = out["pending"][0]
    assert "U15 practise at Leblanc" in item["memory_index"]
    assert "école Leblanc" not in item["memory_index"]


@requires_gliner
@pytest.mark.real_ner
def test_a_hand_edited_memory_reaches_the_prompt_tokenized(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """The store is where a human corrects an agent, so it holds real names (STU-879)."""
    memory = tmp_path / "club-memory"
    club_memory.remember(memory, "u15-practice-gym", "Gym confirmed by Mme Isabelle Bergeron",
                         "Mme Isabelle Bergeron <isabelle.bergeron@example.ca>",
                         "The U15 girls practise at école Leblanc.")
    monkeypatch.setattr(club_memory, "MEMORY_DIR", memory)

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    index = out["pending"][0]["memory_index"]
    assert "Isabelle Bergeron" not in index
    assert "isabelle.bergeron@example.ca" not in index
    assert "[u15-practice-gym](u15-practice-gym.md)" in index
    assert "Gym confirmed by" in index
    keymap = json.loads((tmp_path / "keymap.json").read_text(encoding="utf-8"))
    assert "isabelle.bergeron@example.ca" in keymap.values()


def test_full_scope_tokenizes_the_sender_and_the_subject(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    out = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    item = out["pending"][0]
    assert "parent@example.ca" not in json.dumps(item)
    keymap = json.loads((tmp_path / "keymap.json").read_text(encoding="utf-8"))
    assert "parent@example.ca" in keymap.values()


def test_body_only_scope_leaves_the_sender_in_the_clear(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    ctx = pre_ctx(tmp_path, policy=str(write_policy(tmp_path, scope="body_only")))

    out = run_script_stage(drafts_pre, ctx, monkeypatch, capsys)

    assert out["pending"][0]["from_address"] == "parent@example.ca"


def test_the_item_carries_the_body_past_the_preview_cutoff(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    ctx = pre_ctx(tmp_path, policy=str(write_policy(tmp_path, scope="body_only")))

    out = run_script_stage(drafts_pre, ctx, monkeypatch, capsys)

    # The corpus preview stops at the first question; the second one only exists
    # in the body the mailbox served.
    assert "coût" not in EMAIL["body_snippet"]
    assert "coût" in out["pending"][0]["body_text"]


def test_only_the_pending_ids_have_their_body_read(
    drafts_pre, bodies, tmp_path, monkeypatch, capsys
):
    corpus = write_corpus(tmp_path, [EMAIL, {**EMAIL, "message_id": "m2", "thread_id": "t2"}])
    report = write_report(tmp_path, actions=("label_archive", "needs_reply"))

    run_script_stage(
        drafts_pre, pre_ctx(tmp_path, corpus=str(corpus), report=str(report)),
        monkeypatch, capsys,
    )

    assert bodies.asked == [["m2"]]


def test_no_pending_mail_never_touches_the_mailbox(
    drafts_pre, bodies, threads, tmp_path, monkeypatch, capsys
):
    report = write_report(tmp_path, actions=("label_archive",))

    out = run_script_stage(drafts_pre, pre_ctx(tmp_path, report=str(report)), monkeypatch, capsys)

    assert out["pending"] == []
    assert bodies.asked == []
    assert threads.asked == []


def test_a_mail_with_no_body_falls_back_to_the_preview(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(drafts_pre, "get_body_fetcher", lambda provider: _Bodies({}))
    ctx = pre_ctx(tmp_path, policy=str(write_policy(tmp_path, scope=None)))

    out = run_script_stage(drafts_pre, ctx, monkeypatch, capsys)

    assert out["pending"][0]["body_text"] == EMAIL["body_snippet"]


def test_the_run_id_is_the_only_run_scoped_field_in_the_item(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """STU-917: the item is the resume key, so every field in it is a cache buster.
    `run_id` is in there because the tool's journal line is unattributable without
    it; nothing else run-scoped may join it."""
    first = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)
    second_report = write_report(tmp_path, run_id="run-2")

    second = run_script_stage(
        drafts_pre, pre_ctx(tmp_path, report=str(second_report)), monkeypatch, capsys
    )

    assert first["run_id"] != second["run_id"]
    assert [item["run_id"] for item in first["pending"]] == ["run-1"]
    assert [item["run_id"] for item in second["pending"]] == ["run-2"]
    assert ([{**i, "run_id": None} for i in first["pending"]]
            == [{**i, "run_id": None} for i in second["pending"]])


def test_a_report_from_another_dump_fails_loudly(drafts_pre, tmp_path, monkeypatch, capsys):
    corpus = write_corpus(tmp_path, [{**EMAIL, "message_id": "other"}])

    with pytest.raises(SystemExit):
        run_script_stage(drafts_pre, pre_ctx(tmp_path, corpus=str(corpus)), monkeypatch, capsys)


def test_the_prompt_fingerprint_covers_every_drafting_skill(studio_copy):
    # `studio_copy` and never `STUDIO`: editing the checkout's own skill writes state
    # every other worker reads, and under `-n auto` it flaked two unrelated tests
    # (STU-1085). The copy carries the real files, so what is asserted is unchanged —
    # each skill the drafting agents declare moving has to move the fingerprint. The
    # voice skill sits in the same prompt as the facts skill and moves every draft
    # (STU-1007): a skill left out of the sources would be edited and nothing re-drafted.
    skills = {path for agent in ("drafter", "draft-reviewer")
              for path in prompt_sources.skill_paths(studio_copy, agent).values()}
    assert skills
    for relative in sorted(skills):
        before = drafts.prompt_fingerprint(studio_copy, ["body_snippet"])
        skill = studio_copy / relative
        skill.write_text(skill.read_text(encoding="utf-8") + "\n- Appended.\n",
                         encoding="utf-8")
        assert drafts.prompt_fingerprint(studio_copy, ["body_snippet"]) != before, relative


# ── the post stage ───────────────────────────────────────────────────────────

def post_payload(tmp_path, pending=("m1",), fanout=None):
    pre = {
        "run_id": "run-1",
        "needs_reply": len(pending),
        "pending": [{"message_id": message_id} for message_id in pending],
        "paths": {
            "draft_journal": str(tmp_path / "drafts.jsonl"),
            "stage_log": str(tmp_path / "stages.jsonl"),
        },
    }
    outputs = {"drafts-pre": pre}
    if fanout is not None:
        outputs["draft-fanout"] = fanout
    return {"previous_outputs": outputs}


def journal(tmp_path, entries):
    """Lines the draft tool wrote — `run_id` defaults to the payload's own run."""
    path = tmp_path / "drafts.jsonl"
    path.write_text("".join(json.dumps({"run_id": "run-1", **entry}) + "\n"
                            for entry in entries), encoding="utf-8")


def test_drafts_are_counted_from_the_journal_not_from_the_agents(
    drafts_post, tmp_path, monkeypatch, capsys
):
    journal(tmp_path, [{"message_id": "m1", "draft_id": "d1", "status": "ok"}])
    fanout = {"results": [
        {"index": 0, "status": "success", "output": {"draft_id": "d1"}},
        {"index": 1, "status": "success", "output": {"draft_id": "invented"}},
    ]}

    out, _ = run_stage(drafts_post, post_payload(tmp_path, ("m1", "m2"), fanout), monkeypatch, capsys)

    assert (out["pending"], out["drafted"], out["failed"]) == (2, 1, 0)


def test_a_failed_item_is_counted_and_does_not_fail_the_stage(
    drafts_post, tmp_path, monkeypatch, capsys
):
    journal(tmp_path, [{"message_id": "m1", "draft_id": "d1", "status": "ok"}])
    fanout = {"results": [
        {"index": 0, "status": "success"},
        {"index": 1, "status": "failed", "error": "rejected after 3 iterations"},
    ]}

    out, _ = run_stage(drafts_post, post_payload(tmp_path, ("m1", "m2"), fanout), monkeypatch, capsys)

    assert (out["drafted"], out["failed"]) == (1, 1)


def _held_payload(tmp_path, fanout):
    payload = post_payload(tmp_path, ("m1", "m2"), fanout)
    pre = payload["previous_outputs"]["drafts-pre"]
    pre["paths"]["policy"] = str(write_policy(tmp_path))
    pre["pending"] = [{"message_id": "m1", "thread_id": "t1"},
                      {"message_id": "m2", "thread_id": "t2"}]
    pre["selection"] = {"selected": 2, "held": {"answered": 1}, "unreconciled": {}}
    return payload


def _stage_line(tmp_path):
    return read_entries(tmp_path / "stages.jsonl")[-1]


def test_a_group_that_ends_rejected_leaves_no_draft_behind(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """STU-1547: `write-draft` runs before `draft-qa`, so a group rejected on its last
    iteration had already put an unapproved body in Drafts."""
    journal(tmp_path, [
        {"message_id": "m1", "thread_id": "t1", "draft_id": "d1", "status": "ok",
         "timestamp": "2026-09-19T20:00:00+00:00"},
        {"message_id": "m2", "thread_id": "t2", "draft_id": "d2", "status": "ok",
         "timestamp": "2026-09-19T20:00:00+00:00"},
    ])
    drafter = FakeDrafter(modified="2026-09-19T20:00:30Z")
    monkeypatch.setattr(drafts_post, "get_drafter", lambda provider: drafter)
    fanout = {"results": [
        {"index": 0, "status": "success"},
        {"index": 1, "status": "failed", "error": "rejected after 3 iterations"},
    ]}

    out, _ = run_stage(drafts_post, _held_payload(tmp_path, fanout), monkeypatch, capsys)

    assert drafter.deleted == ["d2"]
    assert (out["drafted"], out["failed"]) == (1, 1)
    rows = read_entries(tmp_path / "drafts.jsonl")
    assert (rows[-1]["status"], rows[-1]["draft_id"]) == ("deleted", "d2")
    line = _stage_line(tmp_path)
    assert line["selection"]["selected"] == 1
    assert line["selection"]["held"] == {"answered": 1, "rejected": 1}


def test_a_rejected_draft_a_human_edited_meanwhile_stays(
    drafts_post, tmp_path, monkeypatch, capsys
):
    journal(tmp_path, [{"message_id": "m2", "thread_id": "t2", "draft_id": "d2",
                        "status": "ok", "timestamp": "2026-09-19T20:00:00+00:00"}])
    drafter = FakeDrafter(modified="2026-09-19T20:30:00Z")
    monkeypatch.setattr(drafts_post, "get_drafter", lambda provider: drafter)
    fanout = {"results": [{"index": 0, "status": "success"},
                          {"index": 1, "status": "failed"}]}

    run_stage(drafts_post, _held_payload(tmp_path, fanout), monkeypatch, capsys)

    assert drafter.deleted == []
    assert _stage_line(tmp_path)["selection"]["selected"] == 2


def test_a_conversation_the_write_left_to_a_human_is_counted_held(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """STU-1548: no second draft beside the human's, and the line says why."""
    journal(tmp_path, [{"message_id": "m2", "thread_id": "t2", "draft_id": "human",
                        "status": "held"}])
    monkeypatch.setattr(drafts_post, "get_drafter",
                        lambda provider: pytest.fail("nothing of ours to delete"))
    fanout = {"results": [{"index": 0, "status": "success"},
                          {"index": 1, "status": "failed"}]}

    run_stage(drafts_post, _held_payload(tmp_path, fanout), monkeypatch, capsys)

    line = _stage_line(tmp_path)
    assert line["selection"]["held"] == {"answered": 1, "blocked": 1}
    assert line["selection"]["selected"] == 1


def test_zero_draftable_mail_leaves_the_run_successful(
    drafts_post, tmp_path, monkeypatch, capsys
):
    out, _ = run_stage(drafts_post, post_payload(tmp_path, pending=()), monkeypatch, capsys)

    assert (out["pending"], out["drafted"], out["failed"]) == (0, 0, 0)


def test_a_dead_fanout_still_produces_a_summary(drafts_post, tmp_path, monkeypatch, capsys):
    journal(tmp_path, [{"message_id": "m9", "draft_id": "d9", "status": "ok"}])

    out, _ = run_stage(drafts_post, post_payload(tmp_path), monkeypatch, capsys)

    assert (out["pending"], out["drafted"], out["failed"]) == (1, 0, 0)


def test_another_runs_draft_on_the_same_mail_is_not_counted_as_this_ones(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """The journal is shared and two runs over one mailbox share their pending set
    (STU-1003). A draft a human deleted puts its mail back in this run's fan-out,
    so message id alone credits this run with a draft it never made."""
    journal(tmp_path, [{"run_id": "run-0", "message_id": "m1", "draft_id": "d1",
                        "status": "ok"}])
    fanout = {"results": [{"index": 0, "status": "failed", "error": "drafter died"}]}

    out, _ = run_stage(drafts_post, post_payload(tmp_path, ("m1",), fanout),
                       monkeypatch, capsys)

    assert (out["pending"], out["drafted"], out["failed"]) == (1, 0, 1)


def test_a_journal_line_naming_no_run_is_counted_by_none(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """`redraft` answers a human, not a run, and lines older than STU-917 name
    nothing. Unattributable is the answer; the clock would only guess."""
    journal(tmp_path, [{"run_id": None, "message_id": "m1", "draft_id": "d1",
                        "status": "ok"}])

    out, _ = run_stage(drafts_post, post_payload(tmp_path, ("m1",)), monkeypatch, capsys)

    assert out["drafted"] == 0


# ── the stage's own record (STU-917) ─────────────────────────────────────────

def stage_lines(tmp_path):
    return read_entries(tmp_path / "stages.jsonl")


def test_the_stage_opens_its_record_before_touching_the_mailbox(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert [(e["run_id"], e["status"], e["needs_reply"]) for e in stage_lines(tmp_path)] == [
        ("run-1", "started", 1)
    ]


def test_a_drafter_that_dies_leaves_a_recorded_failure_not_a_silence(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """The stage stays tolerated — `mailbox-full` lets it fail (STU-850) — but a
    tolerated failure still has to be a recorded one. Before STU-917 a dead stage
    and a mailbox with nothing to answer left the same artifacts: none."""
    class DeadDrafter:
        def find_draft(self, thread_id):
            raise RuntimeError("graph 503")

    monkeypatch.setattr(drafts_pre, "get_drafter", lambda provider: DeadDrafter())

    with pytest.raises(RuntimeError):
        run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    started, failed = stage_lines(tmp_path)
    assert (started["run_id"], started["status"]) == ("run-1", "started")
    assert (failed["run_id"], failed["status"], failed["drafted"]) == ("run-1", "failed", 0)
    assert failed["error"] == "RuntimeError: graph 503"
    assert failed["needs_reply"] == 1


def test_a_report_from_another_dump_is_a_recorded_failure_too(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """The refusal is a `SystemExit`, which is not an `Exception` — a stage death
    that would otherwise slip past the handler unrecorded."""
    corpus = write_corpus(tmp_path, [{**EMAIL, "message_id": "other"}])

    with pytest.raises(SystemExit):
        run_script_stage(drafts_pre, pre_ctx(tmp_path, corpus=str(corpus)), monkeypatch, capsys)

    assert stage_lines(tmp_path)[-1]["status"] == "failed"


def test_the_stage_record_closes_with_what_the_mailbox_received(
    drafts_post, tmp_path, monkeypatch, capsys
):
    journal(tmp_path, [{"message_id": "m1", "draft_id": "d1", "status": "ok"}])
    fanout = {"results": [{"index": 0, "status": "success"}]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1", "m2"), fanout), monkeypatch, capsys)

    line = stage_lines(tmp_path)[-1]
    assert (line["run_id"], line["status"]) == ("run-1", "completed")
    assert (line["needs_reply"], line["drafted"]) == (2, 1)


def test_a_stage_that_drafted_nothing_still_says_it_ran(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """The run that filed STU-917: zero drafts, and no artifact saying whether the
    stage answered an empty mailbox or died before reaching one."""
    run_stage(drafts_post, post_payload(tmp_path, pending=()), monkeypatch, capsys)

    line = stage_lines(tmp_path)[-1]
    assert (line["status"], line["needs_reply"], line["drafted"]) == ("completed", 0, 0)


# ── what the leg spent, off the record its children reported (STU-1210) ─────

USAGE = {"by_model": {"claude-opus-4-8": {"completion_tokens": 500,
                                          "total_tokens": 500}}}


def child_stage(name, attempts, usage=None):
    """One entry of the `stages` record a child run carries back (STU-1064)."""
    return {"stage": name, "status": "success", "attempts": attempts,
            **({"token_usage": usage} if usage else {})}


def attempt(generate=1, qa=1, usage=USAGE):
    """One pass of the `email-draft` group as its child reports it: an agent stage,
    the run's own script write, an agent stage. A script stage reports 0 attempts
    (Studio >= 0.20.2, STU-1245), so `write-draft` is not counted as a call."""
    return [child_stage("generate-draft", generate, usage),
            child_stage("write-draft", 0),
            child_stage("draft-qa", qa, usage)]


def test_the_stage_record_says_what_the_fanout_spent(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """STU-1012: most of a mailbox-full run's tokens go here, and this line is the
    only artifact that outlives the worktree to say so."""
    fanout = {"results": [
        {"index": 0, "status": "success", "stages": attempt()},
        {"index": 1, "status": "success", "stages": attempt()},
    ]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1", "m2"), fanout), monkeypatch, capsys)

    spend = stage_lines(tmp_path)[-1]["tokens"]
    # Two agent stages per attempt, and the script write between them is neither.
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (4, 0)
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 2000


def test_a_group_that_took_a_rejection_reports_the_calls_the_retry_cost(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """The child appends a stage entry per group iteration, so a rejection shows up
    as another pair rather than being folded into one flat usage (STU-1056). Run
    96f5c2b6 reported 5 calls for 5 conversations across 7 writes."""
    fanout = {"results": [
        {"index": 0, "status": "success", "stages": [*attempt(), *attempt()]},
        {"index": 1, "status": "success", "stages": attempt()},
    ]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1", "m2"), fanout), monkeypatch, capsys)

    assert stage_lines(tmp_path)[-1]["tokens"]["llm_calls"] == 6


def test_a_ralph_retry_inside_a_stage_is_a_call_the_journal_could_not_see(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """The half `attempted_calls` was structurally blind to (STU-1058): a retry
    leaves no journal row, because nothing reached the mailbox between attempts."""
    fanout = {"results": [{"index": 0, "status": "success",
                           "stages": attempt(generate=3)}]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1",), fanout), monkeypatch, capsys)

    assert stage_lines(tmp_path)[-1]["tokens"]["llm_calls"] == 4


def test_a_failed_item_is_priced_from_what_its_child_reported(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """The whole of STU-1210: the item died, and its record still says what it made
    and what it burned, so none of it has to stand in `unpriced_calls`."""
    fanout = {"results": [{"index": 0, "status": "failed", "error": "graph 503",
                           "stages": [child_stage("generate-draft", 3, USAGE)]}]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1",), fanout), monkeypatch, capsys)

    spend = stage_lines(tmp_path)[-1]["tokens"]
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (3, 0)
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 500


def test_only_a_stage_that_reported_nothing_is_unpriced(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """A group that exhausted its iterations against a provider that answered none of
    them: six calls, all owed, and the stage beside them that did answer is not."""
    fanout = {"results": [{"index": 0, "status": "failed", "error": "qa rejected",
                           "stages": [child_stage("generate-draft", 3),
                                      child_stage("draft-qa", 3, USAGE)]}]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1",), fanout), monkeypatch, capsys)

    spend = stage_lines(tmp_path)[-1]["tokens"]
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (6, 3)


def test_a_resumed_item_is_not_a_call_this_run_paid_for(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """Its draft was bought by an earlier attempt — the same reason it still counts
    as drafted, and the reason it is dropped rather than counted unpriced."""
    journal(tmp_path, [{"message_id": "m1", "draft_id": "d1", "status": "ok"}])
    fanout = {"results": [{"index": 0, "status": "success", "cached": True}]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1",), fanout), monkeypatch, capsys)

    spend = stage_lines(tmp_path)[-1]["tokens"]
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (0, 0)


def test_an_item_with_no_record_at_all_is_still_a_call_it_made(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """A throw from before the child run started, or a Studio too old to report the
    record. Unlike a resumed item, it may have spent something and cannot say — so it
    keeps the floor from before STU-1064 rather than reading as free."""
    fanout = {"results": [{"index": 0, "status": "failed", "error": "spawn refused"}]}

    run_stage(drafts_post, post_payload(tmp_path, ("m1",), fanout), monkeypatch, capsys)

    spend = stage_lines(tmp_path)[-1]["tokens"]
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (1, 1)


# A structural extract of the serving host's journal for run b8a30626 (2026-09-18): one
# conversation whose group was rejected on all three iterations. Usage and stage
# records only, no mail.
REJECTED_RUN = "b8a30626-89cb-43ef-83ca-961752748234"
REJECTED_JOURNAL = REPO_ROOT / "tests/data/2026-09-18T12h38m-drafts-318e36a8.jsonl"


def test_a_lone_rejected_conversation_is_priced_off_the_journal(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """STU-1503: every item failed, so the map failed the tolerated `call:` and no
    fan-out output reached this stage. The leg was written as 0 calls on a run that
    spent 1.2M tokens."""
    drafts_post.RUNS_DIR.mkdir()
    (drafts_post.RUNS_DIR / REJECTED_JOURNAL.name).write_text(REJECTED_JOURNAL.read_text())
    payload = post_payload(tmp_path, ("m1",))
    payload["previous_outputs"]["drafts-pre"]["run_id"] = REJECTED_RUN

    out, _ = run_stage(drafts_post, payload, monkeypatch, capsys)

    spend = stage_lines(tmp_path)[-1]["tokens"]
    # Three iterations of two agent stages, plus the three usage-less `write-draft`
    # attempts a host below Studio 0.20.2 still reported (STU-1245).
    assert (spend["llm_calls"], spend["unpriced_calls"], spend["unrecorded_items"]) == (9, 3, 0)
    assert sum(c["total_tokens"] for c in spend["by_model"].values()) == 1_220_125
    assert out["failed"] == 1


def test_a_fanout_nothing_recorded_is_an_unrecorded_floor_not_a_free_leg(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """No output and no journal to recover it from: the item still ran, so the leg
    says it cannot tell rather than reading as a leg that cost nothing (STU-912)."""
    run_stage(drafts_post, post_payload(tmp_path, ("m1", "m2")), monkeypatch, capsys)

    spend = stage_lines(tmp_path)[-1]["tokens"]
    assert (spend["llm_calls"], spend["unpriced_calls"], spend["unrecorded_items"]) == (2, 2, 2)


# ── why a conversation due a reply was not drafted (STU-1186) ────────────────

def run_leg(drafts_pre_module, drafts_post_module, tmp_path, monkeypatch, capsys,
            fanout=None, **ctx):
    """Both stages of the drafting leg over one report, so the terminal line is the
    one a real run leaves: `drafts-pre` decides, `drafts-post` closes."""
    pre = run_script_stage(drafts_pre_module, pre_ctx(tmp_path, **ctx), monkeypatch, capsys)
    outputs = {"drafts-pre": pre}
    if fanout is not None:
        outputs["draft-fanout"] = fanout
    run_stage(drafts_post_module, {"previous_outputs": outputs}, monkeypatch, capsys)
    return stage_lines(tmp_path)[-1]


def test_a_conversation_held_because_it_already_carries_a_draft_says_so(
    drafts_pre, drafts_post, existing_drafts, tmp_path, monkeypatch, capsys
):
    """Run `af590fa8`: `needs_reply: 1, drafted: 0`, 1.6 s, no LLM call and no journal
    row. Reading `stages.jsonl` that was indistinguishable from a leg that dropped a
    reply, and it took the Studio journal — pruned at 30 days — to find the
    conversation already held a draft."""
    existing_drafts.thread_ids = {"t1"}

    line = run_leg(drafts_pre, drafts_post, tmp_path, monkeypatch, capsys)

    assert (line["needs_reply"], line["drafted"]) == (1, 0)
    assert line["selection"] == {"selected": 0, "held": {"blocked": 1},
                                 "unreconciled": {}}
    # The arithmetic that makes the line readable: nothing reached the fan-out, so
    # `needs_reply > drafted` is accounted for and no parent is waiting.
    selection = line["selection"]
    assert selection["selected"] + sum(selection["held"].values()) == line["needs_reply"]


def test_a_conversation_the_leg_selected_and_lost_does_not_wear_the_same_shape(
    drafts_pre, drafts_post, tmp_path, monkeypatch, capsys
):
    """The opposite cause of the same disagreement: nothing was held, the fan-out was
    handed the conversation and it died. `selected` minus `drafted` is the parent still
    waiting."""
    fanout = {"results": [{"index": 0, "status": "failed", "error": "drafter died"}]}

    line = run_leg(drafts_pre, drafts_post, tmp_path, monkeypatch, capsys, fanout=fanout)

    assert (line["needs_reply"], line["drafted"]) == (1, 0)
    assert line["selection"] == {"selected": 1, "held": {}, "unreconciled": {}}


def test_a_re_draft_counts_as_selected_and_not_as_held(
    drafts_pre, drafts_post, existing_drafts, tmp_path, monkeypatch, capsys
):
    """STU-886: the conversation went to the fan-out like any other, and the draft it
    already carries is one the leg is asked to replace — so a lost re-draft is a parent
    still reading the uncorrected reply, not a hold."""
    existing_drafts.thread_ids = {"t1"}
    write_journal(tmp_path, [{"draft_id": "draft_t1", "status": "ok",
                              "prompt_fingerprint": "fp-before-the-fix"}])

    line = run_leg(drafts_pre, drafts_post, tmp_path, monkeypatch, capsys)

    assert line["selection"] == {"selected": 1, "held": {}, "unreconciled": {}}


def test_the_reason_a_hold_carries_is_the_conversation_verdict_that_made_it(
    drafts_pre, drafts_post, threads, tmp_path, monkeypatch, capsys
):
    """`answered` and `blocked` are not one reason spelled twice: here the mailbox holds
    no draft at all — the club sent the reply — so "already drafted" would be false."""
    threads.last_sent = {"t1": "2026-06-12T09:00:00+00:00"}

    line = run_leg(drafts_pre, drafts_post, tmp_path, monkeypatch, capsys)

    assert line["selection"]["held"] == {"answered": 1}


# STU-2003: synthetic stand-ins for the two shapes the reviewer answers by hand.
_QUOTE = ("\n\nLe 22 sept. 2026 à 21:07, Club <club@example.ca> a écrit :\n\n"
          "Bonjour,\n\nUne place s'est libérée. Laissez-moi savoir si vous la voulez?\n")
THANKS = "Bonjour,\nParfait, vous pouvez changer son équipe.\nMerci!\nLéa" + _QUOTE
APPEAL = ("Bonjour,\nJe vous écris à la suite de l'annonce de la composition des équipes. "
          "J'aimerais comprendre pourquoi ma fille n'a pas été retenue cette année.\nMerci")
HOLDS_FILE = REPO_ROOT / "tests" / "data" / "draft_holds.yaml"
HOLDS = drafts.load_holds(HOLDS_FILE)


@pytest.mark.parametrize("body, hold", [
    (THANKS, "acknowledged"),
    (APPEAL, "selection_appeal"),
    # A thanks that asks something, with or without the question mark, still owes a reply.
    ("Merci! Et quand commencent les pratiques" + _QUOTE, None),
    ("Merci pour l'info, est-ce possible de payer en deux fois" + _QUOTE, None),
    # `Merci de ...` is a request, not a thanks.
    ("Merci de m'envoyer le formulaire." + _QUOTE, None),
    # Without the club's quote below it, nothing says the thanks answers us.
    ("Bonjour,\nParfait, vous pouvez changer son équipe.\nMerci!", None),
    # Asking how selection works is a question the draft can answer; only a decision
    # already made is held.
    ("Pouvez-vous m'expliquer comment se fera la composition des équipes?", None),
], ids=["thanks", "appeal", "thanks-and-question", "thanks-and-unmarked-question",
        "merci-de", "unquoted", "selection-question"])
def test_a_hold_reads_the_senders_own_text(body, hold):
    assert drafts.content_hold(body, HOLDS) == hold


def test_the_quote_does_not_count_against_the_thanks():
    """The quoted club message asks a question; the sender's own text does not."""
    assert "?" in _QUOTE
    assert drafts.content_hold(THANKS, HOLDS) == "acknowledged"


def test_no_holds_file_holds_nothing(tmp_path):
    assert drafts.load_holds(tmp_path / "absent.yaml") == {}


@pytest.mark.parametrize("rule", [
    {"blocked": {"all": ["merci"]}},
    {"thanks": {"max_words": 40}},
], ids=["verdict-name", "no-all"])
def test_a_hold_that_would_blur_the_count_refuses(tmp_path, rule):
    path = tmp_path / "holds.yaml"
    path.write_text(yaml.safe_dump({"holds": rule}), encoding="utf-8")
    with pytest.raises(ValueError):
        drafts.load_holds(path)


@pytest.mark.parametrize("body, reason", [
    (THANKS, "acknowledged"), (APPEAL, "selection_appeal"),
], ids=["thanks", "appeal"])
def test_a_held_mail_gets_no_draft_and_the_line_names_why(
    drafts_pre, drafts_post, existing_drafts, bodies, tmp_path, monkeypatch, capsys,
    body, reason
):
    """`needs_reply` stays the classifier's; the drafts selection is what holds it, and
    before the mailbox is asked anything."""
    corpus = write_corpus(tmp_path, [{**EMAIL, "body_snippet": body}])
    existing_drafts.thread_ids = {"t1"}

    line = run_leg(drafts_pre, drafts_post, tmp_path, monkeypatch, capsys,
                   corpus=str(corpus), draft_holds=str(HOLDS_FILE))

    assert (line["needs_reply"], line["drafted"]) == (1, 0)
    assert line["selection"] == {"selected": 0, "held": {reason: 1}, "unreconciled": {}}
    assert bodies.asked == []


def _thread_report(tmp_path, messages):
    """A report whose messages share one conversation, each with its own bucket."""
    verdicts = [{
        "message_id": message_id, "thread_id": "t1", "path": "deterministic",
        "status": status, "bucket": bucket, "suggested_action": action,
        "confidence": "high", "rule_id": "r1", "subtype": None, "reasoning": None,
        "error": None, "date": date,
    } for message_id, bucket, action, date, status in messages]
    path = tmp_path / "thread-report.json"
    path.write_text(json.dumps({"run_id": "run-1", "verdicts": verdicts}),
                    encoding="utf-8")
    return path


def test_a_reply_the_conversation_unit_never_selected_is_counted_apart(
    drafts_pre, drafts_post, tmp_path, monkeypatch, capsys
):
    """A notification posting last on a thread that holds a question: the clamp bends
    the latest message's action to fit the reconciled bucket, so `reply_targets` never
    selected it and `needs_reply` is 0.

    Counted under `unreconciled` and not under `held` for exactly that reason — adding
    the two together would make `selected` plus the holds exceed `needs_reply`, which
    is the arithmetic the split exists to prevent.
    """
    report = _thread_report(tmp_path, [
        ("m1", "A", "needs_reply", "2026-06-15T09:00:00+00:00", "ok"),
        ("m2", "C", "label_archive", "2026-06-16T09:00:00+00:00", "ok"),
    ])

    line = run_leg(drafts_pre, drafts_post, tmp_path, monkeypatch, capsys,
                   report=str(report))

    assert (line["needs_reply"], line["drafted"]) == (0, 0)
    assert line["selection"] == {"selected": 0, "held": {},
                                 "unreconciled": {"clamped_by_bucket": 1}}


def test_a_thread_the_report_did_not_decide_whole_is_named_as_that(
    drafts_pre, drafts_post, tmp_path, monkeypatch, capsys
):
    """The other way `reply_targets` drops a message that asked: reconciliation is
    all-or-nothing, and a sibling still on its way to stage 2 leaves the conversation
    without a verdict. A different repair from a clamp, so a different word."""
    report = _thread_report(tmp_path, [
        ("m1", "A", "needs_reply", "2026-06-15T09:00:00+00:00", "ok"),
        ("m2", "A", "escalate_to_llm", "2026-06-16T09:00:00+00:00", "ok"),
    ])

    line = run_leg(drafts_pre, drafts_post, tmp_path, monkeypatch, capsys,
                   report=str(report))

    assert line["selection"]["unreconciled"] == {"thread_undecided": 1}


def test_a_stage_that_died_deciding_claims_no_breakdown(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """`started` and `failed` are written by a stage that has not decided or could not
    finish deciding. Null, never an empty breakdown: `{}` would say it held nothing
    back, which is the reading that turns a lost reply into a clean run."""
    class DeadDrafter:
        def find_draft(self, thread_id):
            raise RuntimeError("graph 503")

    monkeypatch.setattr(drafts_pre, "get_drafter", lambda provider: DeadDrafter())

    with pytest.raises(RuntimeError):
        run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)

    assert [e["selection"] for e in stage_lines(tmp_path)] == [None, None]


def test_a_pre_output_written_before_the_field_closes_the_line_unknown(
    drafts_post, tmp_path, monkeypatch, capsys
):
    """A replay of a run whose `drafts-pre` output predates STU-1186 must not have a
    breakdown invented for it — the reader's job is to say it cannot answer."""
    payload = post_payload(tmp_path, pending=())

    run_stage(drafts_post, payload, monkeypatch, capsys)

    assert stage_lines(tmp_path)[-1]["selection"] is None


# ── wiring ───────────────────────────────────────────────────────────────────

def load_yaml(relative):
    return yaml.safe_load((STUDIO / relative).read_text(encoding="utf-8"))


def test_the_group_regenerates_the_draft_when_qa_rejects():
    group = load_yaml("pipelines/email-draft.pipeline.yaml")["stages"][0]

    assert group["group"] and group["max_iterations"] > 1
    assert [s["name"] for s in group["stages"]] == [
        "generate-draft", "write-draft", "draft-qa"]
    # Only the last stage of a group can restart it, and only through a rejection.
    assert "group_feedback" in group["stages"][0]["context"]["include"]


def test_the_classify_child_names_the_one_agent_stage_it_runs():
    """The other half of the same guard, for the other child this repo fans out over
    (STU-1210): `call-floor` reads the journal by stage name, so a rename here would
    have it measure a whole classify leg against zero calls."""
    stages = load_yaml("pipelines/classify.pipeline.yaml")["stages"]

    assert {s["name"] for s in stages if s.get("agent")} == set(
        cost.CLASSIFY_AGENT_STAGES)


def test_the_agent_stages_of_the_whole_draft_pipeline_are_the_ones_counted():
    """`journal_calls` reads a drafting call by stage name off the Studio journal
    (STU-1058), so a rename has it find nothing and report a run as having made no
    call at all.

    The whole pipeline and not the group alone: an agent stage appended beside the
    group runs on every attempt just as one inside it does, and neither side would
    count it."""
    def agent_names(stages):
        names = set()
        for stage in stages:
            names |= agent_names(stage["stages"]) if "group" in stage else (
                {stage["name"]} if stage.get("agent") else set())
        return names

    assert agent_names(load_yaml("pipelines/email-draft.pipeline.yaml")["stages"]) == \
        set(cost.DRAFTING_AGENT_STAGES)


def test_a_qa_rejection_is_a_rejection_not_a_failure():
    detection = load_yaml("contracts/draft-qa.contract.yaml")["post_validation"][
        "rejection_detection"]

    assert detection["field"] == "verdict"
    assert detection["rejected_values"] == ["rejected"]
    assert detection["details_field"] == "issues"


def test_the_mailbox_write_is_the_runs_and_not_the_models():
    """STU-1030: no agent holds a tool that writes a draft, so no prompt has to ask
    for one write and no contract has to count them."""
    stages = load_yaml("pipelines/email-draft.pipeline.yaml")["stages"][0]["stages"]
    write = next(s for s in stages if s["name"] == "write-draft")

    assert write["executor"] == "script" and write["script"] == "scripts/write_draft.py"
    holders = {agent for agent in ("drafter", "draft-reviewer", "classifier")
               if any("mail_drafts" in tool
                      for tool in load_yaml(f"agents/{agent}.agent.yaml").get("tools") or [])}
    assert holders == set()


def test_both_drafting_agents_ask_for_their_prefix_to_be_cached():
    """STU-1051: `auto` caches only when the contract obliges a tool call, and STU-1030
    took `email-draft`'s floor away with the mailbox-writing tool — so the stage the
    group re-runs on every rejection stopped writing a cache for the next attempt to
    read. Measured 0.0 on run d45962c2, against 0.545 on the same run's classify leg."""
    assert load_yaml("agents/drafter.agent.yaml")["prompt_cache"] == "on"
    assert load_yaml("agents/draft-reviewer.agent.yaml")["prompt_cache"] == "on"


def test_the_reviewer_reads_every_skill_the_drafter_writes_against():
    """STU-1070: a judge cannot enforce a rule it cannot read.

    The reviewer's own prose named three voice gates while `club-voice.skill.md` — the
    file that states them as counts over 832 sent replies — was in the drafter's prompt
    and not in its own. A hand-copied subset is what STU-1007 replaced with a measured
    source, and what this one dropped shipped: a reply claiming it had forwarded a
    message, approved. Superset and not equality — the drafter holds nothing the
    reviewer must be blind to, but a skill written for judging alone would be fine.
    """
    drafter = set(load_yaml("agents/drafter.agent.yaml")["skills"])
    reviewer = set(load_yaml("agents/draft-reviewer.agent.yaml")["skills"])

    assert drafter <= reviewer


def test_the_reviewer_judges_what_the_mailbox_holds():
    stages = load_yaml("pipelines/email-draft.pipeline.yaml")["stages"][0]["stages"]
    write, qa = stages[-2], stages[-1]

    assert "previous_stage_output" in qa["context"]["include"]
    assert "draft_id" in load_yaml(
        f"contracts/{write['contract']}.contract.yaml")["schema"]["required_fields"]


def test_the_write_stage_reads_every_path_the_item_carries(
    drafts_pre, tmp_path, monkeypatch, capsys
):
    """A renamed item field leaves the stage writing under the wrong keymap — or, for
    `run_id`, a journal line nothing can attribute (STU-917)."""
    item = run_script_stage(drafts_pre, pre_ctx(tmp_path), monkeypatch, capsys)["pending"][0]

    assert {"policy", "keymap", "journal", "run_id", "message_id", "thread_id",
            "reply_to", "reply_name"} <= set(item)


def test_studios_script_stages_take_the_same_interpreter_as_the_targets():
    """STU-856 gave a venv outside `.venv/` one button; the mailbox write is a script
    stage now, and Studio sniffs a `.venv/` beside the cwd unless told otherwise."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    assert "STUDIO_PYTHON_BIN := $(PYTHON)" in makefile


def test_the_fanout_resumes_per_item_and_collects_failures():
    stage = load_yaml("pipelines/draft-items.pipeline.yaml")["stages"][0]

    assert stage["pipeline"] == "email-draft"
    assert stage["resume"] is True
    assert stage["on_item_failure"] == "collect-all"
    assert stage["concurrency"] >= 1


def test_a_dead_fanout_never_fails_the_drafts_run():
    stages = load_yaml("pipelines/drafts.pipeline.yaml")["stages"]

    call = next(s for s in stages if "call" in s)
    assert call["on_failure"] == "continue"
    assert call["condition"] == "stages.drafts-pre.output.needs_draft == true"


def test_the_club_mailbox_is_never_read_in_the_clear():
    policy = yaml.safe_load((deployment.ROOT / "rules" / "policy.yaml").read_text(encoding="utf-8"))

    assert policy["anonymization"]["scope"] == "full"


def test_each_turn_is_tokenized_against_its_own_senders_address():
    """STU-1467: a club-authored turn is vetoed by the club's address, not the head's."""
    from email_classifier.core.schema import EmailInput

    seen = []

    class Recorder:
        def anonymize(self, text, *, from_address=None):
            seen.append(from_address)
            return text

        def redact_known(self, text):
            return text

    email = EmailInput(
        message_id="m1", thread_id="t1", from_address="parent@example.ca",
        subject="s", date="2026-06-11T08:00:00+00:00", body_snippet="b", headers={},
    )
    turns = [
        {"date": "d", "direction": "received", "body": "q", "from_address": "parent@example.ca"},
        {"date": "d", "direction": "sent", "body": "a", "from_address": "inbox@club.org"},
        {"date": "d", "direction": "received", "body": "no sender"},
    ]

    item = drafts.draft_item(
        email, "body", turns, Recorder(), ["body_text"], prompt_fp="fp", paths={"policy": "p", "keymap": "k", "draft_journal": "j"},
        memory_index="", run_id="r", written_period="day",
    )

    assert seen[:3] == ["parent@example.ca", "inbox@club.org", "parent@example.ca"]
    assert all("from_address" not in turn for turn in item["thread_history"])


def test_a_name_first_minted_in_body_text_is_redacted_in_the_history(monkeypatch):
    """History is tokenized before `body_text`, so a name only the body made the
    detector claim was unknown to the turns until they are settled after (STU-1993)."""
    from email_classifier import anonymize
    from email_classifier.core.types import EmailInput
    from tests.conftest import FindsTheChildInTheHeadOnly

    monkeypatch.setattr(anonymize, "_model", FindsTheChildInTheHeadOnly)
    email = EmailInput(
        message_id="m1", thread_id="t1", from_address="parent@example.ca",
        subject="s", date="2026-06-11T08:00:00+00:00", body_snippet="b", headers={},
    )
    turns = [{"date": "d", "direction": "received", "body": "Je l'annonce à Rosalie ce soir."}]
    item = drafts.draft_item(
        email, "Rosalie a reçu l'invitation.", turns, anonymize.Tokenizer(), ["body_text"],
        prompt_fp="fp", paths={"policy": "p", "keymap": "k", "draft_journal": "j"},
        memory_index="", run_id="r", written_period="day",
    )
    assert item["body_text"] == "PERSON_1 a reçu l'invitation."
    assert item["thread_history"][0]["body"] == "Je l'annonce à PERSON_1 ce soir."
