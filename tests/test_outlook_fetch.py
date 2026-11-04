"""Tests for the Graph list->get->project loop (STU-325).

Only the pagination/capping logic is exercised, with a fake Graph session.
OAuth/credential code is pure I/O and untested.
"""

import pytest

from email_classifier.fetch import outlook


class _Session:
    """Fake GraphSession: serves canned list pages, then per-message resources."""

    def __init__(self, pages, by_id):
        self._pages = pages
        self._by_id = by_id
        self._page_idx = 0
        self.get_ids = []
        self.list_params = []
        self.get_calls = []
        self.folder_lookups = []

    def get(self, url, params=None, prefer=None):
        if url.startswith("/me/mailFolders('"):
            name = url.split("'")[1]
            self.folder_lookups.append(name)
            return {"id": f"{name}-id"}
        if url.startswith("/me/messages/"):
            message_id = url.rsplit("/", 1)[-1]
            self.get_ids.append(message_id)
            self.get_calls.append((params, prefer))
            return self._by_id[message_id]
        self.list_params.append(params)
        page = self._pages[self._page_idx]
        self._page_idx += 1
        return page


def _msg(mid, conversation):
    return {
        "id": mid,
        "conversationId": conversation,
        "from": {"emailAddress": {"address": f"{mid}@x.com"}},
        "subject": "s",
        "uniqueBody": {"contentType": "text", "content": "snip"},
        "receivedDateTime": "2024-06-15T12:00:00Z",
        "inferenceClassification": "focused",
    }


def _fake():
    pages = [
        {
            "value": [{"id": "a"}, {"id": "b"}],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skip=2",
        },
        {"value": [{"id": "c"}]},
    ]
    by_id = {k: _msg(k, f"t_{k}") for k in ("a", "b", "c")}
    return _Session(pages, by_id)


def test_fetch_inputs_follows_nextlink_until_max_results():
    session = _fake()

    result = outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=3)

    assert [r["thread_id"] for r in result] == ["t_a", "t_b", "t_c"]
    assert session.get_ids == ["a", "b", "c"]
    assert [r["message_id"] for r in result] == ["a", "b", "c"]


def test_fetch_inputs_caps_at_max_results_and_skips_extra_gets():
    session = _fake()

    result = outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=2)

    assert [r["thread_id"] for r in result] == ["t_a", "t_b"]
    # "c" must never be fetched once the cap is reached.
    assert session.get_ids == ["a", "b"]


def test_nextlink_is_followed_without_re_sending_query_options():
    session = _fake()

    outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=3)

    # The nextLink already carries $select/$top/$orderby; re-sending them 400s.
    assert session.list_params[1] is None


def test_fetch_inputs_never_requests_more_than_the_page_size():
    session = _fake()

    outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=10_000)

    assert session.list_params[0]["$top"] <= 100


def test_query_is_sent_as_search_without_orderby():
    session = _fake()

    outlook.OutlookFetcher(session).fetch_inputs(query="from:a@b.com", max_results=1)

    # Graph rejects $search combined with $orderby.
    assert session.list_params[0]["$search"] == '"from:a@b.com"'
    assert "$orderby" not in session.list_params[0]


def test_empty_query_lists_the_most_recent_messages():
    session = _fake()

    outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=1)

    assert session.list_params[0]["$orderby"] == "receivedDateTime desc"
    assert "$search" not in session.list_params[0]


def test_a_deleted_or_junked_message_is_not_part_of_the_corpus():
    """Apply writes to this corpus, so a trashed mail in it gets un-deleted (STU-973)."""
    session = _Session(
        [{"value": [
            {"id": "a", "parentFolderId": "inbox-id"},
            {"id": "trashed", "parentFolderId": "deleteditems-id"},
            {"id": "junked", "parentFolderId": "junkemail-id"},
            {"id": "b", "parentFolderId": "archive-id"},
        ]}],
        {k: _msg(k, f"t_{k}") for k in ("a", "b")},
    )

    result = outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=10)

    assert [r["message_id"] for r in result] == ["a", "b"]
    assert sorted(session.folder_lookups) == ["deleteditems", "junkemail", "sentitems"]


def test_a_draft_or_sent_reply_is_not_part_of_the_corpus():
    """Our own mail is not incoming mail (STU-996): listed, a reply draft an
    earlier run wrote is scored like a parent's, is its conversation's most recent
    message, and apply stamps it and moves it out of Drafts."""
    session = _Session(
        [{"value": [
            {"id": "a", "parentFolderId": "inbox-id"},
            {"id": "draft", "parentFolderId": "drafts-id", "isDraft": True},
            {"id": "sent", "parentFolderId": "sentitems-id"},
        ]}],
        {"a": _msg("a", "t_a")},
    )

    result = outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=10)

    assert [r["message_id"] for r in result] == ["a"]
    # Never fetched, so they cost neither a GET nor a stage-2 escalation.
    assert session.get_ids == ["a"]


def test_a_draft_or_sent_reply_is_dropped_on_the_get_too():
    """The push path names ids off a notification and never lists (STU-996)."""
    session = _Session([], {
        "draft": {**_msg("draft", "t"), "parentFolderId": "drafts-id", "isDraft": True},
        "sent": {**_msg("sent", "t"), "parentFolderId": "sentitems-id"},
        "live": {**_msg("live", "t"), "parentFolderId": "inbox-id"},
    })

    result = outlook.OutlookFetcher(session).fetch_inputs_by_ids(
        ["draft", "sent", "live"]
    )

    assert [r["message_id"] for r in result] == ["live"]
    assert "isDraft" in session.get_calls[0][0]["$select"].split(",")


def test_a_message_trashed_since_the_notification_is_dropped_on_the_get():
    """The push path names ids off a notification and never lists (STU-988)."""
    session = _Session([], {
        "trashed": {**_msg("trashed", "t"), "parentFolderId": "deleteditems-id"},
        "junked": {**_msg("junked", "t"), "parentFolderId": "junkemail-id"},
        "live": {**_msg("live", "t"), "parentFolderId": "inbox-id"},
    })

    result = outlook.OutlookFetcher(session).fetch_inputs_by_ids(
        ["trashed", "junked", "live"]
    )

    assert [r["message_id"] for r in result] == ["live"]
    assert "parentFolderId" in session.get_calls[0][0]["$select"].split(",")


# ── the drafting subset's bodies (STU-852) ───────────────────────────────────

def test_the_classify_path_never_asks_graph_for_a_body():
    session = _fake()

    outlook.OutlookFetcher(session).fetch_inputs(query="", max_results=1)

    assert "body" not in session.get_calls[0][0]["$select"].split(",")


def test_bodies_are_fetched_as_plain_text_for_the_named_ids_only():
    session = _Session([], {"a": {"id": "a", "uniqueBody": {
        "contentType": "text", "content": "Bonjour,\n\nQuestion au 3e paragraphe."}}})

    bodies = outlook.OutlookFetcher(session).fetch_bodies_by_ids(["a"])

    assert bodies == {"a": "Bonjour,\n\nQuestion au 3e paragraphe."}
    params, prefer = session.get_calls[0]
    assert params["$select"] == "id,uniqueBody"
    assert prefer == ['outlook.body-content-type="text"']


def test_a_message_with_no_body_yields_an_empty_string():
    session = _Session([], {"a": {"id": "a"}})

    assert outlook.OutlookFetcher(session).fetch_bodies_by_ids(["a"]) == {"a": ""}


# ── the answered conversation's earlier turns (STU-941) ──────────────────────

class _Conversation:
    """Fake GraphSession: one conversation, some of whose messages we sent."""

    def __init__(self, turns, sent_ids=(), page_size=None, folders=None, categories=None):
        self._turns = turns  # id -> (receivedDateTime, body)
        self._sent = list(sent_ids)
        self._page_size = page_size or len(turns) or 1
        self._folders = folders or {}  # id -> well-known folder name
        self._categories = categories or {}  # id -> the email-tool/* names it carries
        self.list_params = []
        self.folder_lookups = []

    def get(self, url, params=None, prefer=None):
        if url.startswith("/me/mailFolders('") and not url.endswith("/messages"):
            name = url.split("'")[1]
            self.folder_lookups.append(name)
            return {"id": f"{name}-id"}
        if url.endswith("/messages") or url.startswith("nextlink:"):
            listing, skip = (url.split(":", 1)[1].split("@") if params is None
                             else (url, "0"))
            self.list_params.append((listing, params))
            ids = list(self._sent if "sentitems" in listing else self._turns)
            page = ids[int(skip):int(skip) + self._page_size]
            result = {"value": [
                {"id": i, "receivedDateTime": self._turns[i][0],
                 "parentFolderId": f"{self._folders.get(i, 'archive')}-id",
                 "categories": list(self._categories.get(i, []))}
                for i in page]}
            if int(skip) + self._page_size < len(ids):
                result["@odata.nextLink"] = (
                    f"nextlink:{listing}@{int(skip) + self._page_size}")
            return result
        message_id = url.rsplit("/", 1)[-1]
        received, body = self._turns[message_id]
        # Enough of `_SELECT` for the projection, so a single-message GET can be
        # read back through `fetch_inputs_by_ids` and not only as a thread turn.
        return {"id": message_id, "receivedDateTime": received,
                "uniqueBody": {"contentType": "text", "content": body},
                "conversationId": "t1", "subject": "sujet",
                "from": {"emailAddress": {"address": f"{message_id}@x.com"}},
                "parentFolderId": f"{self._folders.get(message_id, 'archive')}-id",
                "categories": list(self._categories.get(message_id, []))}


def _conversation():
    return _Conversation(
        {
            "ask": ("2026-06-10T08:00:00Z", "Quel est l'horaire?"),
            "answer": ("2026-06-10T18:00:00Z", "Les pratiques sont le mardi."),
            "followup": ("2026-06-12T08:00:00Z", "ok mais et pour le samedi?"),
        },
        sent_ids=["answer"],
    )


def test_thread_history_is_the_conversation_minus_the_mail_being_answered():
    session = _conversation()

    history = outlook.OutlookFetcher(session).fetch_thread_history("t1", "followup")

    assert [turn["body"] for turn in history] == [
        "Quel est l'horaire?", "Les pratiques sont le mardi."]


def test_a_turn_the_club_sent_is_marked_as_such():
    session = _conversation()

    history = outlook.OutlookFetcher(session).fetch_thread_history("t1", "followup")

    assert [turn["direction"] for turn in history] == ["received", "sent"]
    assert history[0]["date"] == "2026-06-10T08:00:00+00:00"


def test_thread_history_lists_the_whole_mailbox_not_the_inbox():
    """An earlier turn a previous run applied was archived out of the inbox."""
    session = _conversation()

    outlook.OutlookFetcher(session).fetch_thread_history("t1", "followup")

    assert [url for url, _ in session.list_params] == [
        "/me/mailFolders('sentitems')/messages", "/me/messages"]
    assert all("isDraft eq false" in params["$filter"] for _, params in session.list_params)


def test_a_first_message_has_no_history():
    session = _Conversation({"only": ("2026-06-12T08:00:00Z", "Bonjour")})

    assert outlook.OutlookFetcher(session).fetch_thread_history("t1", "only") == []


def _long_conversation(count, page_size, folders=None):
    return _Conversation(
        {f"m{i}": (f"2026-06-{i + 1:02}T08:00:00Z", f"tour {i}") for i in range(count)},
        sent_ids=[f"m{i}" for i in range(count) if i % 2],
        page_size=page_size,
        folders=folders,
    )


def test_a_conversation_past_one_page_still_knows_which_turns_it_sent():
    """A truncated sentitems slice does not lose a turn, it relabels one."""
    session = _long_conversation(count=5, page_size=2)

    history = outlook.OutlookFetcher(session).fetch_thread_history("t1", "m4")

    assert [turn["direction"] for turn in history] == [
        "received", "sent", "received", "sent"]


def test_only_the_most_recent_turns_reach_the_item():
    session = _long_conversation(count=12, page_size=100)

    history = outlook.OutlookFetcher(session).fetch_thread_history("t1", "m11")

    assert [turn["body"] for turn in history] == [
        f"tour {i}" for i in range(5, 11)]


def test_a_deleted_or_junked_turn_is_not_conversation_context():
    """Deleting a turn says it should not be read again (STU-962)."""
    session = _Conversation(
        {
            "trashed": ("2026-06-10T08:00:00Z", "Quel est l'horaire?"),
            "junked": ("2026-06-10T12:00:00Z", "OFFRE EXCLUSIVE"),
            "answer": ("2026-06-10T18:00:00Z", "Les pratiques sont le mardi."),
            "followup": ("2026-06-12T08:00:00Z", "ok mais et pour le samedi?"),
        },
        sent_ids=["answer"],
        folders={"trashed": "deleteditems", "junked": "junkemail"},
    )

    history = outlook.OutlookFetcher(session).fetch_thread_history("t1", "followup")

    assert [turn["body"] for turn in history] == ["Les pratiques sont le mardi."]


def test_a_deleted_turn_does_not_spend_a_history_slot():
    session = _long_conversation(
        count=12, page_size=100, folders={"m9": "deleteditems"})

    history = outlook.OutlookFetcher(session).fetch_thread_history("t1", "m11")

    assert [turn["body"] for turn in history] == [
        f"tour {i}" for i in (4, 5, 6, 7, 8, 10)]


def test_the_gone_folders_are_resolved_once_per_fetcher():
    session = _conversation()
    fetcher = outlook.OutlookFetcher(session)

    fetcher.fetch_thread_history("t1", "followup")
    fetcher.fetch_thread_history("t1", "answer")

    assert sorted(session.folder_lookups) == ["deleteditems", "junkemail"]


# ── when the club last replied (STU-945) ─────────────────────────────────────

def test_the_last_reply_is_dated_from_sent_items_alone():
    session = _conversation()

    assert outlook.OutlookFetcher(session).last_sent_at("t1") == "2026-06-10T18:00:00+00:00"
    assert [url for url, _ in session.list_params] == [
        "/me/mailFolders('sentitems')/messages"]


def test_a_conversation_the_club_never_replied_to_has_no_last_reply():
    session = _Conversation({"ask": ("2026-06-12T08:00:00Z", "Bonjour")})

    assert outlook.OutlookFetcher(session).last_sent_at("t1") is None


def test_a_draft_is_paired_with_the_first_send_after_it_not_the_newest():
    """STU-1998: the newest send often answers a later question."""
    session = _Conversation(
        {
            "old": ("2026-06-01T09:00:00Z", "Une vieille reponse."),
            "reply": ("2026-06-10T18:00:00Z", "Les pratiques sont le mardi."),
            "later": ("2026-06-20T18:00:00Z", "Le tournoi est en juillet."),
        },
        sent_ids=["old", "later", "reply"],
    )
    fetcher = outlook.OutlookFetcher(session)

    replies = fetcher.sent_replies({"t1": "2026-06-10T12:00:00+00:00"})
    assert replies["t1"] == {"sent_at": "2026-06-10T18:00:00+00:00",
                             "body": "Les pratiques sont le mardi."}
    assert fetcher.sent_replies({"t1": "2026-06-21T00:00:00+00:00"}) == {}


# ── a conversation's live message ids (STU-963) ──────────────────────────────

def test_a_conversation_lists_every_message_the_mailbox_still_holds():
    session = _conversation()

    ids = outlook.OutlookFetcher(session).conversation_message_ids("t1")

    assert ids == ["ask", "answer", "followup"]
    # Mailbox-wide, not inbox-scoped: the half an earlier run archived is the point.
    assert [url for url, _ in session.list_params] == ["/me/messages"]


def test_a_deleted_junked_or_sent_message_is_not_one_to_classify():
    """Sent Items for a bigger reason than the other two: our own reply is not
    incoming mail, and left in it would be scored, could decide the thread's action
    and would be archived out of Sent Items by apply."""
    session = _Conversation(
        {
            "ask": ("2026-06-10T08:00:00Z", "Quel est l'horaire?"),
            "trashed": ("2026-06-11T08:00:00Z", "oups"),
            "junked": ("2026-06-11T09:00:00Z", "spam"),
            "answer": ("2026-06-11T10:00:00Z", "Les pratiques sont le mardi."),
        },
        folders={
            "trashed": "deleteditems", "junked": "junkemail", "answer": "sentitems",
        },
    )

    assert outlook.OutlookFetcher(session).conversation_message_ids("t1") == ["ask"]


# ── a message this app trashed rejoins its conversation (STU-983) ────────────
#
# "Somebody threw it out" is only true of a message we did not trash ourselves.
# Our own is the marker with no bucket category: apply trashes straight out of
# the Inbox and strips any email-tool/<bucket> first, so a bucket in Deleted Items means
# a human moved on from a message we had filed in the Archive.


def _thread_with_a_trashed_opener(categories):
    return _Conversation(
        {
            "trashed": ("2026-06-10T08:00:00Z", "Rabais sur les genouillères"),
            "reply": ("2026-06-11T08:00:00Z", "Est-ce encore valide?"),
        },
        folders={"trashed": "deleteditems", "reply": "inbox"},
        categories={"trashed": categories},
    )


def test_a_message_this_app_trashed_rejoins_its_conversation():
    """A reply can reconcile the thread back to a live bucket, and untrashing the
    half an earlier run threw out is what closes the split."""
    session = _thread_with_a_trashed_opener(["email-tool/processed", "email-tool/deleted"])

    ids = outlook.OutlookFetcher(session).conversation_message_ids("t1")

    assert ids == ["trashed", "reply"]


@pytest.mark.parametrize(
    "categories",
    [
        # Never processed by this app: a human's deletion, start to finish.
        [],
        # Processed — but as keep_visible, which leaves exactly this state. Only
        # `email-tool/deleted` separates it from a message this app threw out.
        ["email-tool/processed"],
    ],
)
def test_a_message_a_human_threw_out_stays_out(categories):
    """It would otherwise be re-classified every run, vote in the thread's verdict,
    and be untrashed against them."""
    session = _thread_with_a_trashed_opener(categories)

    assert outlook.OutlookFetcher(session).conversation_message_ids("t1") == ["reply"]


def test_a_trashed_sibling_survives_the_fetch_that_reads_it_back():
    """The round trip, not either half: `conversation_message_ids` offers the id and
    `fetch_inputs_by_ids` reads it. Both drop Deleted Items, so both need the same
    exemption — testing them apart lets one take back what the other offered."""
    session = _thread_with_a_trashed_opener(["email-tool/processed", "email-tool/deleted"])
    fetcher = outlook.OutlookFetcher(session)

    ids = fetcher.conversation_message_ids("t1")

    assert [item["message_id"] for item in fetcher.fetch_inputs_by_ids(ids)] == ids


def test_a_message_a_human_deleted_is_dropped_by_the_read_back_too():
    """The window STU-988 closed is still closed: an id named by a notification whose
    message a human threw out in the meantime carries no `email-tool/deleted`."""
    session = _thread_with_a_trashed_opener(["email-tool/processed"])

    inputs = outlook.OutlookFetcher(session).fetch_inputs_by_ids(["trashed", "reply"])

    assert [item["message_id"] for item in inputs] == ["reply"]


# --- the run's date floor, in Graph's own query language (STU-1123) ---------

def _floor_params(query: str, since: str | None):
    session = _Session([{"value": []}], {})
    outlook.OutlookFetcher(session).fetch_inputs(
        query=query, max_results=10, since=since
    )
    return session.list_params[0]


def test_the_floor_reaches_the_listing_as_a_received_filter():
    params = _floor_params("", "2026-08-19T00:00:00+00:00")
    assert params["$filter"] == "receivedDateTime ge 2026-08-19T00:00:00Z"


def test_the_filter_is_ge_so_the_floor_day_itself_counts():
    assert _floor_params("", "2026-08-19T00:00:00+00:00")["$filter"].split()[1] == "ge"


def test_a_searched_listing_carries_no_filter():
    """Graph rejects `$filter` alongside `$search`, so a searched fetch is bounded
    by `window.in_scope` alone — its window can be spent on mail the run drops."""
    params = _floor_params("musique", "2026-08-19T00:00:00+00:00")
    assert "$filter" not in params
    assert params["$search"] == '"musique"'


def test_no_floor_leaves_the_listing_unfiltered():
    assert "$filter" not in _floor_params("", None)
