"""Tests for the Gmail list->get->project loop (STU-391).

Only the pagination/capping logic is exercised, with a fake that mimics the
googleapiclient fluent service. OAuth/credential code is pure I/O and untested.
"""

import json
import sys
import types

import pytest

from email_classifier.fetch import gmail


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _Messages:
    def __init__(self, pages, by_id):
        self._pages = pages
        self._by_id = by_id
        self._page_idx = 0
        self.get_ids = []
        self.list_maxresults = []
        self.list_queries = []

    def list(self, *, userId, q, maxResults, pageToken=None):
        self.list_maxresults.append(maxResults)
        self.list_queries.append(q)
        resp = self._pages[self._page_idx]
        self._page_idx += 1
        return _Exec(resp)

    def get(self, *, userId, id, format, metadataHeaders):
        self.get_ids.append(id)
        return _Exec(self._by_id[id])


class _Threads:
    """`threads().get`, served from the same canned messages (STU-963)."""

    def __init__(self, by_id):
        self._by_id = by_id

    def get(self, *, userId, id, format):
        return _Exec({"messages": [
            {"id": raw["id"], "labelIds": raw["labelIds"]}
            for raw in self._by_id.values()
            if raw["threadId"] == id
        ]})


class _Labels:
    """`labels().list`, the id -> name map `conversation_message_ids` reads the
    marker through (STU-983). Ids are deliberately not the names: only Gmail's
    system labels are their own id, and `ec/processed` is not one."""

    def __init__(self, names):
        self._names = names
        self.list_calls = 0

    def list(self, *, userId):
        self.list_calls += 1
        return _Exec({"labels": [
            {"id": lid, "name": name} for lid, name in self._names.items()
        ]})


class _Users:
    def __init__(self, messages, labels):
        self._messages = messages
        self._threads = _Threads(messages._by_id)
        self._labels = labels

    def messages(self):
        return self._messages

    def threads(self):
        return self._threads

    def labels(self):
        return self._labels


class _Service:
    def __init__(self, messages, label_names=None):
        self.labels = _Labels(label_names or {})
        self._users = _Users(messages, self.labels)

    def users(self):
        return self._users


def _msg(mid, thread):
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": ["INBOX"],
        "snippet": "snip",
        "internalDate": "0",
        "payload": {"headers": [{"name": "From", "value": f"{mid}@x.com"}]},
    }


def test_fetch_inputs_paginates_until_max_results():
    pages = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
        {"messages": [{"id": "c"}]},
    ]
    by_id = {k: _msg(k, f"t_{k}") for k in ("a", "b", "c")}
    messages = _Messages(pages, by_id)
    service = _Service(messages)

    result = gmail.GmailFetcher(service).fetch_inputs(query="label:inbox", max_results=3)

    assert [r["thread_id"] for r in result] == ["t_a", "t_b", "t_c"]
    assert messages.get_ids == ["a", "b", "c"]
    assert [r["message_id"] for r in result] == ["a", "b", "c"]


def test_fetch_inputs_caps_at_max_results_and_skips_extra_gets():
    pages = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
        {"messages": [{"id": "c"}]},
    ]
    by_id = {k: _msg(k, f"t_{k}") for k in ("a", "b", "c")}
    messages = _Messages(pages, by_id)
    service = _Service(messages)

    result = gmail.GmailFetcher(service).fetch_inputs(query="", max_results=2)

    assert [r["thread_id"] for r in result] == ["t_a", "t_b"]
    # "c" must never be fetched once the cap is reached.
    assert messages.get_ids == ["a", "b"]


def test_a_conversation_lists_every_live_message_of_its_thread():
    by_id = {
        "a": _msg("a", "t1"),
        "b": _msg("b", "t1"),
        "c": _msg("c", "t2"),
    }
    service = _Service(_Messages([], by_id))

    assert gmail.GmailFetcher(service).conversation_message_ids("t1") == ["a", "b"]


@pytest.mark.parametrize("label", ["TRASH", "SPAM", "DRAFT", "SENT"])
def test_a_gone_or_sent_message_is_not_one_to_classify(label):
    """SENT is in the list for a different reason than the others: our own reply is
    not incoming mail, and left in it would be scored and archived out of Sent Mail."""
    by_id = {"a": _msg("a", "t1"), "b": _msg("b", "t1")}
    by_id["b"]["labelIds"] = [label]
    service = _Service(_Messages([], by_id))

    assert gmail.GmailFetcher(service).conversation_message_ids("t1") == ["a"]


@pytest.mark.parametrize("label", ["TRASH", "SPAM", "DRAFT", "SENT"])
def test_an_id_the_mailbox_no_longer_holds_as_incoming_is_dropped(label):
    """The push path names ids off a notification and never lists (STU-993)."""
    by_id = {"a": _msg("a", "t1"), "b": _msg("b", "t1")}
    by_id["b"]["labelIds"] = [label]
    service = _Service(_Messages([], by_id))

    result = gmail.GmailFetcher(service).fetch_inputs_by_ids(["a", "b"])

    assert [r["message_id"] for r in result] == ["a"]


def test_fetch_inputs_never_requests_more_than_page_size():
    pages = [{"messages": [{"id": "a"}]}]
    by_id = {"a": _msg("a", "t_a")}
    messages = _Messages(pages, by_id)
    service = _Service(messages)

    gmail.GmailFetcher(service).fetch_inputs(query="", max_results=10_000)

    assert messages.list_maxresults[0] <= 500


# --- OAuth refusal on a headless host (STU-948) --------------------------------
#
# `get_credentials` imports the Google libraries lazily, so the whole stack is
# stubbed into sys.modules rather than installed: the suite stays hermetic
# (STU-857), and the stub is what makes `run_local_server` observable at all.


class _RefreshError(Exception):
    pass


class _FakeCreds:
    def __init__(self, valid=False, expired=False, refresh_token=None, on_refresh=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token
        self.scopes = list(gmail.SCOPES)
        self._on_refresh = on_refresh

    def refresh(self, request):
        if self._on_refresh:
            self._on_refresh()

    def to_json(self):
        return "{}"


class _FakeFlow:
    def __init__(self, recorder):
        self._recorder = recorder

    def run_local_server(self, port):
        self._recorder.append(port)
        return _FakeCreds(valid=True)


def _stub_google(monkeypatch, cached_creds, consent_calls):
    class _Credentials:
        @staticmethod
        def from_authorized_user_info(info, scopes):
            return cached_creds

    class _InstalledAppFlow:
        @staticmethod
        def from_client_secrets_file(path, scopes):
            return _FakeFlow(consent_calls)

    def stub(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    stub("google")
    stub("google.auth")
    stub("google.auth.exceptions", RefreshError=_RefreshError)
    stub("google.auth.transport")
    stub("google.auth.transport.requests", Request=lambda: object())
    stub("google.oauth2")
    stub("google.oauth2.credentials", Credentials=_Credentials)
    stub("google_auth_oauthlib")
    stub("google_auth_oauthlib.flow", InstalledAppFlow=_InstalledAppFlow)


def _cached_token(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"scopes": list(gmail.SCOPES)}), encoding="utf-8")
    return token


def _dead_grant():
    def revoked():
        raise _RefreshError("invalid_grant")

    return _FakeCreds(expired=True, refresh_token="rt", on_refresh=revoked)


def test_get_credentials_refuses_a_dead_refresh_token_without_a_browser(monkeypatch, tmp_path):
    consent_calls = []
    _stub_google(monkeypatch, _dead_grant(), consent_calls)

    with pytest.raises(gmail.GrantLost) as raised:
        gmail.get_credentials(token_file=_cached_token(tmp_path), interactive=False)

    assert gmail.CONSENT_COMMAND in str(raised.value)
    assert consent_calls == []


def test_get_credentials_refuses_an_absent_token_without_a_browser(monkeypatch, tmp_path):
    consent_calls = []
    _stub_google(monkeypatch, None, consent_calls)

    with pytest.raises(gmail.GrantLost, match="no cached token"):
        gmail.get_credentials(token_file=tmp_path / "absent.json", interactive=False)

    assert consent_calls == []


def test_get_credentials_still_consents_when_a_browser_is_available(monkeypatch, tmp_path):
    consent_calls = []
    _stub_google(monkeypatch, _dead_grant(), consent_calls)

    creds = gmail.get_credentials(token_file=_cached_token(tmp_path), interactive=True)

    assert creds.valid
    assert consent_calls == [0]


# --- a grant that dies mid-run (STU-953) ---------------------------------------


@pytest.fixture
def stub_refresh_error(monkeypatch):
    """`_guard` imports google-auth lazily; the suite stays hermetic (STU-857)."""
    module = types.ModuleType("google.auth.exceptions")
    module.RefreshError = _RefreshError
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.auth", types.ModuleType("google.auth"))
    monkeypatch.setitem(sys.modules, "google.auth.exceptions", module)
    return _RefreshError


class _HttpError(Exception):
    """The shape googleapiclient's HttpError exposes: a status on `.resp`."""

    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.resp = types.SimpleNamespace(status=status)


class _Raising:
    def __init__(self, error):
        self._error = error

    def users(self):
        return self

    def messages(self):
        return self

    def get(self, **_kwargs):
        return self

    def execute(self):
        raise self._error


def test_a_401_mid_run_is_the_grant_not_this_call(stub_refresh_error):
    service = gmail.GrantAware(_Raising(_HttpError(401)))

    with pytest.raises(gmail.GrantLost) as raised:
        service.users().messages().get(id="m1").execute()

    assert gmail.CONSENT_COMMAND in str(raised.value)


def test_a_revoked_refresh_token_mid_run_is_the_grant(stub_refresh_error):
    """google-auth redeems the refresh token inside `execute()`, so a revoked
    grant surfaces as RefreshError rather than as a 401 that reached Gmail."""
    service = gmail.GrantAware(_Raising(_RefreshError("invalid_grant")))

    with pytest.raises(gmail.GrantLost) as raised:
        service.users().messages().get(id="m1").execute()

    assert gmail.CONSENT_COMMAND in str(raised.value)


def test_a_403_stays_a_scope_problem(stub_refresh_error):
    service = gmail.GrantAware(_Raising(_HttpError(403)))

    with pytest.raises(_HttpError):
        service.users().messages().get(id="m1").execute()


def test_the_guard_leaves_a_working_call_alone(stub_refresh_error):
    pages = [{"messages": [{"id": "a"}]}]
    by_id = {"a": _msg("a", "t_a")}
    service = gmail.GrantAware(_Service(_Messages(pages, by_id)))

    inputs = gmail.GmailFetcher(service).fetch_inputs(query="", max_results=10)

    assert [i["message_id"] for i in inputs] == ["a"]


# ── a message this app trashed rejoins its conversation (STU-983) ────────────
#
# "Somebody threw it out" is only true of a message we did not trash ourselves.
# Our own is the marker with no bucket label: apply trashes straight out of the
# Inbox and strips any ec/<bucket> first, so a bucket in the trash means a human
# moved on from a message we had filed in the Archive.

_MARKER = "Label_7"
_DELETED = "Label_9"
_LABEL_NAMES = {_MARKER: "ec/processed", _DELETED: "ec/deleted"}


def _trashed_thread(labels):
    by_id = {"a": _msg("a", "t1"), "b": _msg("b", "t1")}
    by_id["b"]["labelIds"] = labels
    return _Service(_Messages([], by_id), label_names=_LABEL_NAMES)


def test_a_message_this_app_trashed_rejoins_its_conversation():
    """A reply can reconcile the thread back to a live bucket, and untrashing the
    half an earlier run threw out is what closes the split."""
    service = _trashed_thread(["TRASH", _MARKER, _DELETED])

    assert gmail.GmailFetcher(service).conversation_message_ids("t1") == ["a", "b"]


@pytest.mark.parametrize(
    "labels",
    [
        # Never processed by this app: a human's deletion, start to finish.
        ["TRASH"],
        # Processed — but as keep_visible, which leaves exactly this state. Only
        # `ec/deleted` separates it from a message this app threw out.
        ["TRASH", _MARKER],
    ],
)
def test_a_message_a_human_threw_out_stays_out(labels):
    """It would otherwise be re-classified every run, vote in the thread's verdict,
    and be untrashed against them."""
    service = _trashed_thread(labels)

    assert gmail.GmailFetcher(service).conversation_message_ids("t1") == ["a"]


def test_the_label_names_are_listed_once_per_fetcher():
    service = _trashed_thread(["TRASH", _MARKER, _DELETED])
    fetcher = gmail.GmailFetcher(service)

    fetcher.conversation_message_ids("t1")
    fetcher.conversation_message_ids("t1")

    assert service.labels.list_calls == 1


def test_a_thread_with_nothing_trashed_never_lists_the_labels():
    """The lookup is what the marker test costs; a conversation with no trashed
    message must not pay it."""
    service = _trashed_thread(["INBOX"])

    gmail.GmailFetcher(service).conversation_message_ids("t1")

    assert service.labels.list_calls == 0


def test_a_trashed_sibling_survives_the_fetch_that_reads_it_back():
    """The round trip, not either half: `conversation_message_ids` offers the id and
    `fetch_inputs_by_ids` reads it. Both drop TRASH, so both need the same exemption
    — testing them apart lets one take back what the other offered."""
    service = _trashed_thread(["TRASH", _MARKER, _DELETED])
    fetcher = gmail.GmailFetcher(service)

    ids = fetcher.conversation_message_ids("t1")

    assert [item["message_id"] for item in fetcher.fetch_inputs_by_ids(ids)] == ids


def test_a_message_a_human_deleted_is_dropped_by_the_read_back_too():
    """The window STU-993 closed is still closed: an id named by a notification whose
    message a human threw out in the meantime carries no `ec/deleted`."""
    service = _trashed_thread(["TRASH", _MARKER])

    inputs = gmail.GmailFetcher(service).fetch_inputs_by_ids(["a", "b"])

    assert [item["message_id"] for item in inputs] == ["a"]


# --- the run's date floor, in Gmail's own query language (STU-1123) ----------

def test_the_floor_reaches_the_listing_as_an_after_clause():
    messages = _Messages([{"messages": [{"id": "a"}]}], {"a": _msg("a", "t")})
    gmail.GmailFetcher(_Service(messages)).fetch_inputs(
        query="", max_results=10, since="2026-08-19T00:00:00+00:00"
    )
    assert messages.list_queries == ["after:2026/08/18"]


def test_the_after_clause_is_a_day_wide_of_the_real_floor():
    """`after:` is a date read in the account's timezone, so an exact translation
    is not on offer. Widening costs a few listed ids; narrowing would lose mail
    the floor calls in scope, and `window.in_scope` is the boundary either way."""
    messages = _Messages([{"messages": []}], {})
    gmail.GmailFetcher(_Service(messages)).fetch_inputs(
        query="", max_results=10, since="2026-08-19T00:00:00+00:00"
    )
    assert messages.list_queries == ["after:2026/08/18"]


def test_the_floor_is_anded_with_the_caller_query():
    messages = _Messages([{"messages": []}], {})
    gmail.GmailFetcher(_Service(messages)).fetch_inputs(
        query="label:promotions", max_results=10, since="2026-08-19T00:00:00+00:00"
    )
    assert messages.list_queries == ["label:promotions after:2026/08/18"]


def test_no_floor_leaves_the_query_alone():
    messages = _Messages([{"messages": []}], {})
    gmail.GmailFetcher(_Service(messages)).fetch_inputs(query="from:x", max_results=10)
    assert messages.list_queries == ["from:x"]
