"""Tests for the Graph side of the synthetic mail seeder (STU-845).

A fake Graph session throughout: no msal/requests, no mailbox.
"""

import json

import pytest

from email_classifier.core.types import EmailInput
from email_classifier.seed import outlook
from scripts import seed_outlook


class _Session:
    """Fake GraphSession recording every call and serving canned responses."""

    def __init__(self, pages=None, by_id=None, me=None, drafts=None, folders=None):
        # A list serves every listing; a dict serves each listing url its own
        # pages, which is how a folder-scoped listing differs from `/me/messages`.
        self._pages = pages if pages is not None else [{"value": []}]
        self._by_id = by_id or {}
        self._me = me or {"mail": "test@outlook.com"}
        self._drafts = drafts or {}
        self._folders = folders or {"inbox_id": "Inbox"}
        self._page_idx = 0
        self.calls = []
        self.list_params = []

    def get(self, url, params=None):
        self.calls.append(("GET", url))
        if url == "/me":
            return self._me
        if url.startswith("/me/messages/"):
            return self._by_id[url.rsplit("/", 1)[-1]]
        if url == "/me/mailFolders/drafts/messages":
            conversation = params["$filter"].split("'")[1]
            draft_id = self._drafts.get(conversation)
            return {"value": [{"id": draft_id}] if draft_id else []}
        if url in ("/me/mailFolders/inbox", "/me/mailFolders/sentitems"):
            return {"id": f"{url.rsplit('/', 1)[-1]}_id"}
        if url.startswith("/me/mailFolders/") and not url.endswith("/messages"):
            return {"displayName": self._folders[url.rsplit("/", 1)[-1]]}
        self.list_params.append(params)
        if params is not None:  # a fresh listing; a nextLink follow carries none
            self._sequence = (
                self._pages.get(url, [{"value": []}])
                if isinstance(self._pages, dict)
                else self._pages
            )
            self._page_idx = 0
        page = self._sequence[self._page_idx]
        self._page_idx += 1
        return page

    def request(self, method, url, params=None, payload=None):
        self.calls.append((method, url, payload))
        return {"id": f"seeded_{len([c for c in self.calls if c[0] == 'POST'])}"}


def _email(**overrides):
    fields = {
        "message_id": "msg_1",
        "thread_id": "t_1",
        "from_address": "news@shop.kobo.com",
        "subject": "Soldes",
        "headers": {},
        "body_snippet": "snippet",
        "date": "2026-06-09T13:05:00+00:00",
        "provider_category": "promotions",
    }
    return EmailInput(**{**fields, **overrides})


def test_to_graph_carries_the_fixture_sender_date_and_both_markers():
    payload = outlook.to_graph(_email(), "run-1", "test@outlook.com")

    assert payload["from"]["emailAddress"]["address"] == "news@shop.kobo.com"
    assert payload["categories"] == [outlook.CATEGORY]
    assert {"name": outlook.HEADER, "value": "run-1"} in payload["internetMessageHeaders"]
    assert {"name": outlook.FIXTURE_HEADER, "value": "msg_1"} in payload[
        "internetMessageHeaders"
    ]


def test_to_graph_clears_the_draft_flag_so_the_seeded_mail_can_be_replied_to():
    payload = outlook.to_graph(_email(), "run-1", "test@outlook.com")

    assert {"id": "Integer 0x0E07", "value": "0"} in payload[
        "singleValueExtendedProperties"
    ]


def test_to_graph_dates_the_message_through_mapi_not_through_receiveddatetime():
    """Graph stamps its own receivedDateTime on a folder write; MAPI survives (STU-889)."""
    payload = outlook.to_graph(_email(), "run-1", "test@outlook.com")

    assert "receivedDateTime" not in payload
    assert "sentDateTime" not in payload
    assert {"id": "SystemTime 0x0E06", "value": "2026-06-09T13:05:00+00:00"} in payload[
        "singleValueExtendedProperties"
    ]
    assert {"id": "SystemTime 0x0039", "value": "2026-06-09T13:05:00+00:00"} in payload[
        "singleValueExtendedProperties"
    ]


def test_to_graph_addresses_the_seeded_mailbox_not_the_fixture_sender():
    payload = outlook.to_graph(_email(), "run-1", "test@outlook.com")

    assert payload["toRecipients"] == [{"emailAddress": {"address": "test@outlook.com"}}]
    assert payload["from"]["emailAddress"]["address"] != "test@outlook.com"


def test_to_graph_drops_headers_graph_refuses_and_keeps_the_x_prefixed_ones():
    payload = outlook.to_graph(
        _email(headers={"List-Unsubscribe": "<https://x>", "X-Mailer": "klaviyo"}),
        "run-1",
        "test@outlook.com",
    )

    names = [h["name"] for h in payload["internetMessageHeaders"]]
    assert "X-Mailer" in names
    assert "List-Unsubscribe" not in names


def test_seed_posts_each_email_into_the_inbox_folder_and_returns_the_ids():
    session = _Session()

    ids = outlook.seed(
        session, [_email(), _email(message_id="msg_2")], "run-1", "test@outlook.com"
    )

    assert ids == ["seeded_1", "seeded_2"]
    assert [c[1] for c in session.calls] == ["/me/mailFolders/inbox/messages"] * 2


def _properties(payload):
    return {p["id"]: p["value"] for p in payload["singleValueExtendedProperties"]}


def test_to_graph_leaves_a_flat_message_unchained():
    assert set(_properties(outlook.to_graph(_email(), "run-1", "test@outlook.com"))) == {
        "Integer 0x0E07", "SystemTime 0x0E06", "SystemTime 0x0039"
    }


def test_to_graph_chains_a_reply_through_mapi_and_not_through_headers():
    """Graph rejects a POSTed `In-Reply-To`; the MAPI property under it is accepted."""
    payload = outlook.to_graph(
        _email(), "run-1", "test@outlook.com", "<mine@seed>", ("<first@seed>", "<second@seed>")
    )

    properties = _properties(payload)
    assert properties["String 0x1035"] == "<mine@seed>"
    assert properties["String 0x1042"] == "<second@seed>"
    assert properties["String 0x1039"] == "<first@seed> <second@seed>"
    assert "In-Reply-To" not in [h["name"] for h in payload["internetMessageHeaders"]]


def test_internet_message_id_is_unique_to_the_round():
    """Reused across rounds, it would splice the new chain onto the old conversation."""
    first = outlook.internet_message_id(_email(), "run-1")
    second = outlook.internet_message_id(_email(), "run-2")

    assert first != second
    assert first.startswith("<msg_1.run-1@")


def test_seed_thread_makes_every_message_answer_the_ones_before_it():
    session = _Session()

    ids = outlook.seed_thread(
        session,
        [_email(), _email(message_id="msg_2"), _email(message_id="msg_3")],
        "run-1",
        "test@outlook.com",
    )

    assert ids == ["seeded_1", "seeded_2", "seeded_3"]
    payloads = [call[2] for call in session.calls]
    first, second, third = (_properties(payload) for payload in payloads)
    assert "String 0x1042" not in first
    assert second["String 0x1042"] == first["String 0x1035"]
    assert third["String 0x1039"] == f"{first['String 0x1035']} {second['String 0x1035']}"


_PARENT = {
    "internetMessageId": "<club-012.run-1@seed.email-classifier.invalid>",
    "subject": "Frais de tournoi",
    "from": {"emailAddress": {"address": "parent@example.ca"}},
    "internetMessageHeaders": [{"name": "X-Email-Classifier-Seed", "value": "run-1"}],
}


def test_sent_reply_answers_the_parent_from_the_mailbox_inside_its_conversation():
    payload = outlook.sent_reply(_PARENT, "test@outlook.com", "run-1", "2026-08-23T04:00:00+00:00")

    assert payload["from"]["emailAddress"]["address"] == "test@outlook.com"
    assert payload["toRecipients"] == [{"emailAddress": {"address": "parent@example.ca"}}]
    assert payload["subject"] == "RE: Frais de tournoi"
    properties = _properties(payload)
    assert properties["String 0x1035"] == _PARENT["internetMessageId"]
    assert properties["String 0x1042"] == _PARENT["internetMessageId"]


def test_sent_reply_dates_the_turn_through_the_property_last_sent_at_reads_back():
    """`receivedDateTime` belongs to the store; PR_MESSAGE_DELIVERY_TIME survives."""
    payload = outlook.sent_reply(_PARENT, "test@outlook.com", "run-1", "2026-08-23T04:00:00+00:00")

    assert _properties(payload)["SystemTime 0x0E06"] == "2026-08-23T04:00:00+00:00"


def test_sent_reply_does_not_stack_a_second_re_prefix():
    parent = _PARENT | {"subject": "RE: Frais de tournoi"}

    payload = outlook.sent_reply(parent, "test@outlook.com", "run-1", "2026-08-23T04:00:00+00:00")

    assert payload["subject"] == "RE: Frais de tournoi"


def test_seed_sent_reply_writes_into_sent_items_under_the_parents_round():
    session = _Session(by_id={"seeded": _PARENT})

    message_id, run_id = outlook.seed_sent_reply(
        session, "seeded", "test@outlook.com", "2026-08-23T04:00:00+00:00"
    )

    assert (message_id, run_id) == ("seeded_1", "run-1")
    method, url, payload = next(call for call in session.calls if call[0] == "POST")
    assert url == "/me/mailFolders/sentitems/messages"
    assert payload["internetMessageHeaders"] == [{"name": outlook.HEADER, "value": "run-1"}]


def test_seed_sent_reply_refuses_a_message_the_seeder_did_not_write():
    """A reply the mailbox owner never wrote, in a real conversation, is not undoable."""
    session = _Session(by_id={"real": _PARENT | {"internetMessageHeaders": []}})

    with pytest.raises(RuntimeError, match="refusing to answer"):
        outlook.seed_sent_reply(session, "real", "test@outlook.com", "2026-08-23T04:00:00+00:00")


def test_divergence_names_the_fields_the_mailbox_did_not_reproduce():
    fixture = _email(headers={"List-Unsubscribe": "<https://x>"})
    landed = _email(headers={}, subject="Soldes")

    assert outlook.divergence(fixture, landed) == ["headers"]
    assert outlook.divergence(fixture, fixture) == []


def test_divergence_reports_a_date_the_store_stamped_over():
    """The loss STU-889 fixed passed silently while `date` was not a fidelity field."""
    fixture = _email()
    landed = _email(date="2026-08-21T01:43:41+00:00")

    assert outlook.divergence(fixture, landed) == ["date"]


def test_divergence_accepts_a_body_truncated_by_bodypreview():
    fixture = _email(body_snippet="Bonjour,\n\nMa fille Léa joue au U14.\n\nMerci,\nJosée")
    truncated = _email(body_snippet="Bonjour, Ma fille Léa joue au U14.")

    assert outlook.divergence(fixture, truncated) == []


def test_divergence_flags_a_body_the_mailbox_altered_or_dropped():
    fixture = _email(body_snippet="Ma fille Léa joue au U14.")

    assert outlook.divergence(fixture, _email(body_snippet="")) == ["body_snippet"]
    assert outlook.divergence(fixture, _email(body_snippet="Autre chose")) == ["body_snippet"]


def test_build_session_refuses_before_the_first_write_on_a_read_only_grant(monkeypatch):
    monkeypatch.setattr(
        outlook.client, "preflight_write_scope", lambda: "missing Mail.ReadWrite"
    )

    with pytest.raises(RuntimeError, match="missing Mail.ReadWrite"):
        outlook.build_session()


def _signed_in_as(monkeypatch, address):
    monkeypatch.setattr(outlook.client, "signed_in_address", lambda: address)


def test_assert_test_mailbox_refuses_when_the_mailbox_is_not_declared(monkeypatch):
    monkeypatch.delenv("OUTLOOK_SEED_MAILBOX", raising=False)

    with pytest.raises(RuntimeError, match="OUTLOOK_SEED_MAILBOX is not set"):
        outlook.assert_test_mailbox()


def test_assert_test_mailbox_refuses_a_mailbox_other_than_the_declared_one(monkeypatch):
    monkeypatch.setenv("OUTLOOK_SEED_MAILBOX", "test@outlook.com")
    _signed_in_as(monkeypatch, "ariane@personal.com")

    with pytest.raises(RuntimeError, match="refusing to seed"):
        outlook.assert_test_mailbox()


def test_assert_test_mailbox_refuses_when_no_account_is_cached(monkeypatch):
    monkeypatch.setenv("OUTLOOK_SEED_MAILBOX", "test@outlook.com")
    _signed_in_as(monkeypatch, None)

    with pytest.raises(RuntimeError, match="no Outlook account cached"):
        outlook.assert_test_mailbox()


def test_assert_test_mailbox_accepts_the_declared_mailbox_case_insensitively(monkeypatch):
    monkeypatch.setenv("OUTLOOK_SEED_MAILBOX", "Test@Outlook.com")
    _signed_in_as(monkeypatch, "test@outlook.com")

    assert outlook.assert_test_mailbox() == "test@outlook.com"


def test_the_mailbox_guard_never_calls_the_profile_endpoint(monkeypatch):
    """`GET /me` needs User.Read, which the seeder never consents to: asking
    Graph for it 403s under a mail-only grant."""
    monkeypatch.setenv("OUTLOOK_SEED_MAILBOX", "test@outlook.com")
    _signed_in_as(monkeypatch, "test@outlook.com")
    session = _Session()

    outlook.assert_test_mailbox()

    assert session.calls == []


def test_find_marked_filters_on_the_category_and_follows_nextlink():
    session = _Session(
        pages=[
            {"value": [{"id": "a"}], "@odata.nextLink": "https://graph/next"},
            {"value": [{"id": "b"}]},
        ]
    )

    assert outlook.find_marked(session) == ["a", "b"]
    assert session.list_params[0]["$filter"] == (
        "categories/any(c:c eq 'email-classifier-seed')"
    )
    assert session.list_params[1] is None


def test_find_standing_spans_the_mailbox_so_a_reviewed_round_still_counts():
    """Standing means the mailbox, not the Inbox (STU-976).

    A message a human filed into a review folder is out of the Inbox listing but
    still in `/me/messages`, still in the golden and still in the dump — so the
    duplicate guard must refuse over it and the merges must keep it. The category
    alone does not make a message ours; the header does.
    """
    session = _Session(
        pages={
            "/me/messages": [
                {
                    "value": [
                        {"id": "a", "internetMessageHeaders": [{"name": "X-Email-Classifier-Seed"}]},
                        {"id": "impostor", "internetMessageHeaders": [{"name": "X-Mailer"}]},
                    ],
                    "@odata.nextLink": "https://graph/next",
                },
                {"value": [{"id": "reviewed", "internetMessageHeaders": [{"name": "x-email-classifier-seed"}]}]},
            ],
            "/me/mailFolders/inbox/messages": [{"value": []}],
        }
    )

    assert outlook.find_standing(session) == ["a", "reviewed"]
    assert session.calls[0] == ("GET", "/me/messages")
    assert session.list_params[0]["$select"] == "id,internetMessageHeaders"
    assert session.list_params[0]["$filter"] == (
        "categories/any(c:c eq 'email-classifier-seed')"
    )
    assert session.list_params[1] is None


def _headers_response(*names, conversation="conv_1", folder="inbox_id"):
    return {
        "conversationId": conversation,
        "parentFolderId": folder,
        "internetMessageHeaders": [{"name": name, "value": "run-1"} for name in names],
    }


def _seeded_session(**overrides):
    return _Session(
        pages=[{"value": [{"id": "seeded"}, {"id": "impostor"}]}],
        by_id={
            "seeded": _headers_response("X-Email-Classifier-Seed"),
            "impostor": _headers_response("X-Mailer", conversation="conv_2"),
        },
        **overrides,
    )


def test_find_seeded_keeps_the_messages_carrying_the_seed_header():
    seeded, skipped = outlook.find_seeded(_seeded_session())

    assert [message.message_id for message in seeded] == ["seeded"]
    assert skipped == ["impostor"]


def test_find_seeded_reads_the_fixture_and_the_run_off_the_headers():
    session = _Session(
        pages=[{"value": [{"id": "seeded"}]}],
        by_id={
            "seeded": {
                "conversationId": "conv_1",
                "parentFolderId": "wrong_id",
                "internetMessageHeaders": [
                    {"name": "X-Email-Classifier-Seed", "value": "run-1"},
                    {"name": "X-Email-Classifier-Fixture", "value": "club-012"},
                ],
            }
        },
    )

    seeded, _ = outlook.find_seeded(session)

    assert seeded[0].fixture == "club-012"
    assert seeded[0].run_id == "run-1"
    assert seeded[0].folder_id == "wrong_id"


def test_find_seeded_leaves_the_fixture_empty_on_a_round_seeded_before_the_header():
    seeded, _ = outlook.find_seeded(_seeded_session())

    assert seeded[0].fixture == ""


def test_review_names_the_folder_a_human_filed_each_message_into():
    session = _Session(folders={"wrong_id": "email-classifier-wrong"})
    seeded = [
        outlook.Seeded("m_1", "conv_1", "wrong_id", "club-012", "run-1"),
        outlook.Seeded("m_2", "conv_2", "wrong_id", "club-013", "run-1"),
    ]

    records = outlook.review(session, seeded)

    assert records == [
        {"message_id": "m_1", "fixture": "club-012",
         "folder": "email-classifier-wrong", "run_id": "run-1"},
        {"message_id": "m_2", "fixture": "club-013",
         "folder": "email-classifier-wrong", "run_id": "run-1"},
    ]
    assert session.calls.count(("GET", "/me/mailFolders/wrong_id")) == 1


def test_seed_folder_ids_covers_both_folders_the_seeder_writes_into():
    assert outlook.seed_folder_ids(_Session()) == {"inbox_id", "sentitems_id"}


def test_moved_out_is_the_messages_the_seeder_never_put_where_they_sit():
    seeded = [
        outlook.Seeded("m_1", "conv_1", "inbox_id", "club-012", "run-1"),
        outlook.Seeded("m_2", "conv_2", "wrong_id", "club-013", "run-1"),
    ]

    assert [m.message_id for m in outlook.moved_out(seeded, {"inbox_id"})] == ["m_2"]


def test_moved_out_does_not_read_a_seeded_sent_turn_as_a_human_review():
    """It sits in Sent Items because `seed-reply` wrote it there (STU-964)."""
    seeded = [outlook.Seeded("sent", "conv_1", "sentitems_id", "", "run-1")]

    assert outlook.moved_out(seeded, {"inbox_id", "sentitems_id"}) == []


def test_teardown_deletes_the_messages_it_is_given():
    session = _Session()
    seeded = [outlook.Seeded("seeded", "conv_1", "inbox_id", "club-012", "run-1")]

    assert outlook.teardown(session, seeded) == ["seeded"]
    assert [c for c in session.calls if c[0] == "DELETE"] == [
        ("DELETE", "/me/messages/seeded", None)
    ]


def test_teardown_deletes_the_reply_draft_left_on_a_seeded_conversation():
    """A draft outliving its parent reads as a reply threaded to nothing."""
    session = _Session(drafts={"conv_1": "draft_1"})
    seeded = [outlook.Seeded("seeded", "conv_1", "inbox_id", "club-012", "run-1")]

    outlook.teardown(session, seeded)

    assert [c for c in session.calls if c[0] == "DELETE"] == [
        ("DELETE", "/me/messages/draft_1", None),
        ("DELETE", "/me/messages/seeded", None),
    ]


def _reviewed_session(folder):
    """A mailbox holding one seeded message, filed into `folder`."""
    return _Session(
        pages=[{"value": [{"id": "seeded"}]}],
        by_id={
            "seeded": {
                "conversationId": "conv_1",
                "parentFolderId": folder,
                "internetMessageHeaders": [
                    {"name": "X-Email-Classifier-Seed", "value": "run-1"},
                    {"name": "X-Email-Classifier-Fixture", "value": "club-012"},
                ],
            }
        },
        folders={"inbox_id": "Inbox", "sentitems_id": "Sent Items",
                 "wrong_id": "email-classifier-wrong"},
    )


def _golden_path(tmp_path):
    return tmp_path / "seeded-golden.json"


def _dump_path(tmp_path):
    return tmp_path / "outlook-seeded.json"


def _run_cli(monkeypatch, session, tmp_path, *argv):
    """Run the CLI against the fake session, with every artifact path under tmp_path.

    A teardown prunes the golden and the dump, so their defaults are overridden
    here rather than per test: left alone, a bare `pytest` would prune the real
    artifacts of whoever's mailbox is seeded.
    """
    monkeypatch.setattr(
        seed_outlook.outlook, "assert_test_mailbox", lambda: "test@outlook.com"
    )
    monkeypatch.setattr(seed_outlook.outlook, "build_session", lambda: session)
    dump = tmp_path / "review.json"
    seeded = (
        ["--golden", str(_golden_path(tmp_path)), "--dump", str(_dump_path(tmp_path))]
        if argv[0] == "teardown"
        else []
    )
    return seed_outlook.main([*argv, "--review-dump", str(dump), *seeded]), dump


def _deletes(session):
    return [c for c in session.calls if c[0] == "DELETE"]


def test_export_review_writes_the_folder_and_the_fixture_of_each_message(
    monkeypatch, tmp_path
):
    session = _reviewed_session("wrong_id")

    code, dump = _run_cli(monkeypatch, session, tmp_path, "export-review")

    assert code == 0
    assert json.loads(dump.read_text(encoding="utf-8")) == {
        "mailbox": "test@outlook.com",
        "messages": [
            {"message_id": "seeded", "fixture": "club-012",
             "folder": "email-classifier-wrong", "run_id": "run-1"}
        ],
    }


def test_teardown_refuses_a_round_a_human_filed_out_of_the_inbox(monkeypatch, tmp_path):
    """The folder is the whole verdict — deleting it is what STU-869 lost."""
    session = _reviewed_session("wrong_id")

    code, dump = _run_cli(monkeypatch, session, tmp_path, "teardown")

    assert code == 1
    assert _deletes(session) == []
    assert dump.exists()


def test_teardown_does_not_refuse_the_sent_turn_seed_reply_left_in_sent_items(
    monkeypatch, tmp_path
):
    """`seed-reply` put it there, so it carries no human verdict to read first (STU-964)."""
    session = _reviewed_session("sentitems_id")

    code, _ = _run_cli(monkeypatch, session, tmp_path, "teardown")

    assert code == 0
    assert _deletes(session) == [("DELETE", "/me/messages/seeded", None)]


def test_teardown_force_deletes_the_reviewed_round_once_the_review_is_exported(
    monkeypatch, tmp_path
):
    session = _reviewed_session("wrong_id")

    code, dump = _run_cli(monkeypatch, session, tmp_path, "teardown", "--force")

    assert code == 0
    assert _deletes(session) == [("DELETE", "/me/messages/seeded", None)]
    assert json.loads(dump.read_text(encoding="utf-8"))["messages"][0]["folder"] == (
        "email-classifier-wrong"
    )


def _round(run, conversation, folder="inbox_id"):
    return {
        "conversationId": conversation,
        "parentFolderId": folder,
        "internetMessageHeaders": [{"name": "X-Email-Classifier-Seed", "value": run}],
    }


def _two_round_session(second_folder="inbox_id"):
    """A mailbox holding two seeded rounds, one message each (STU-966)."""
    return _Session(
        pages=[{"value": [{"id": "first"}, {"id": "second"}]}],
        by_id={
            "first": _round("run-1", "conv_1"),
            "second": _round("run-2", "conv_2", second_folder),
        },
        folders={"inbox_id": "Inbox", "wrong_id": "email-classifier-wrong"},
    )


def test_teardown_run_deletes_only_the_round_it_names(monkeypatch, tmp_path):
    session = _two_round_session()

    code, _ = _run_cli(monkeypatch, session, tmp_path, "teardown", "--run", "run-1")

    assert code == 0
    assert _deletes(session) == [("DELETE", "/me/messages/first", None)]


def test_teardown_run_refuses_a_run_id_no_message_carries(monkeypatch, tmp_path):
    """Sweeping nothing reads exactly like a round that came down."""
    session = _two_round_session()

    with pytest.raises(SystemExit) as refused:
        _run_cli(monkeypatch, session, tmp_path, "teardown", "--run", "run-9")

    assert "run-1, run-2" in str(refused.value)
    assert _deletes(session) == []


def test_teardown_run_exports_the_review_of_every_standing_round(monkeypatch, tmp_path):
    """A scoped export would clobber the record of the rounds left standing."""
    session = _two_round_session()

    _, dump = _run_cli(monkeypatch, session, tmp_path, "teardown", "--run", "run-1")

    exported = json.loads(dump.read_text(encoding="utf-8"))["messages"]
    assert [record["run_id"] for record in exported] == ["run-1", "run-2"]


def test_teardown_run_is_not_blocked_by_another_round_filed_out_of_the_inbox(
    monkeypatch, tmp_path
):
    """The refusal guards the scope, not the mailbox (STU-875)."""
    session = _two_round_session(second_folder="wrong_id")

    code, _ = _run_cli(monkeypatch, session, tmp_path, "teardown", "--run", "run-1")

    assert code == 0
    assert _deletes(session) == [("DELETE", "/me/messages/first", None)]


def _artifacts(tmp_path, message_ids):
    """A golden and a dump describing exactly `message_ids`, as a seeding round leaves them."""
    golden = _golden_path(tmp_path)
    golden.write_text(
        json.dumps(
            {
                message_id: {"path": "catch_all", "bucket": "bucket_c", "action": "archive"}
                for message_id in message_ids
            }
        ),
        encoding="utf-8",
    )
    dump = _dump_path(tmp_path)
    dump.write_text(
        json.dumps([{"message_id": message_id} for message_id in message_ids]),
        encoding="utf-8",
    )
    return golden, dump


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_teardown_run_prunes_both_artifacts_to_the_round_left_standing(
    monkeypatch, tmp_path
):
    """A partly stale golden reads as live, unlike a wholly stale one (STU-977).

    `make eval-seeded` would replay the frozen dump, score the deleted round
    against inputs frozen before the delete, and pass.
    """
    session = _two_round_session()
    golden, dump = _artifacts(tmp_path, ["first", "second"])

    code, _ = _run_cli(monkeypatch, session, tmp_path, "teardown", "--run", "run-1")

    assert code == 0
    assert list(_read(golden)) == ["second"]
    assert _read(dump) == [{"message_id": "second"}]


def test_teardown_empties_both_artifacts_when_it_sweeps_the_mailbox(monkeypatch, tmp_path):
    session = _two_round_session()
    golden, dump = _artifacts(tmp_path, ["first", "second"])

    code, _ = _run_cli(monkeypatch, session, tmp_path, "teardown")

    assert code == 0
    assert _read(golden) == {}
    assert _read(dump) == []


def test_a_refused_teardown_leaves_both_artifacts_alone(monkeypatch, tmp_path):
    """Nothing was deleted, so both still describe the mailbox as it is."""
    session = _reviewed_session("wrong_id")
    golden, dump = _artifacts(tmp_path, ["seeded"])

    code, _ = _run_cli(monkeypatch, session, tmp_path, "teardown")

    assert code == 1
    assert list(_read(golden)) == ["seeded"]
    assert _read(dump) == [{"message_id": "seeded"}]


def test_teardown_exports_the_review_even_when_nothing_was_moved(monkeypatch, tmp_path):
    session = _reviewed_session("inbox_id")

    code, dump = _run_cli(monkeypatch, session, tmp_path, "teardown")

    assert code == 0
    assert _deletes(session) == [("DELETE", "/me/messages/seeded", None)]
    assert json.loads(dump.read_text(encoding="utf-8"))["messages"][0]["folder"] == "Inbox"
