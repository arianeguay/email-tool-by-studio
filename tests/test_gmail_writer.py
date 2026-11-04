"""Tests for the Gmail write primitives (STU-405).

Same style as test_gmail_fetch: a fake mimicking the googleapiclient fluent
service, no network. The fake's _Messages exposes modify/trash but NO delete
method — a code path reaching for permanent deletion would crash the tests.
"""

import pytest

from email_classifier.apply.gmail import GmailWriter
from email_classifier.fetch.gmail import client


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _Messages:
    def __init__(self):
        self.modify_calls = []
        self.trash_ids = []
        self.untrash_ids = []
        self.get_calls = []
        self.messages = {}

    def modify(self, *, userId, id, body):
        self.modify_calls.append((id, body))
        return _Exec({})

    def trash(self, *, userId, id):
        self.trash_ids.append(id)
        return _Exec({})

    def untrash(self, *, userId, id):
        self.untrash_ids.append(id)
        return _Exec({})

    def get(self, *, userId, id, format):
        self.get_calls.append((id, format))
        return _Exec(self.messages.get(id, {}))


class _Labels:
    def __init__(self, existing):
        self._existing = existing
        self.created = []

    def list(self, *, userId):
        return _Exec({"labels": list(self._existing)})

    def create(self, *, userId, body):
        self.created.append(body)
        label = {"id": f"Label_{len(self.created)}", "name": body["name"]}
        self._existing.append(label)
        return _Exec(label)


class _Users:
    def __init__(self, messages, labels):
        self._messages = messages
        self._labels = labels

    def messages(self):
        return self._messages

    def labels(self):
        return self._labels


class _Service:
    def __init__(self, existing_labels=()):
        self.messages = _Messages()
        self.labels = _Labels(list(existing_labels))
        self._users = _Users(self.messages, self.labels)

    def users(self):
        return self._users


def test_add_label_sends_add_only():
    service = _Service()
    GmailWriter(service).add_label("m1", "Label_7")

    assert service.messages.modify_calls == [("m1", {"addLabelIds": ["Label_7"]})]


def test_remove_label_sends_remove_only():
    service = _Service()
    GmailWriter(service).remove_label("m1", "INBOX")

    assert service.messages.modify_calls == [("m1", {"removeLabelIds": ["INBOX"]})]


def test_trash_uses_trash_endpoint_never_delete():
    service = _Service()
    GmailWriter(service).trash("m1")

    assert service.messages.trash_ids == ["m1"]
    assert not hasattr(service.messages, "delete")


def test_untrash_restores_a_trashed_message():
    service = _Service()
    GmailWriter(service).untrash("m1")

    assert service.messages.untrash_ids == ["m1"]


def test_find_label_returns_none_without_creating():
    service = _Service(existing_labels=[{"id": "Label_1", "name": "other"}])

    assert GmailWriter(service).find_label("email-tool/processed") is None
    assert service.labels.created == []


def test_label_ids_reads_minimal_format():
    service = _Service()
    service.messages.messages["m1"] = {"labelIds": ["INBOX", "Label_3"]}

    assert GmailWriter(service).label_ids("m1") == ["INBOX", "Label_3"]
    assert service.messages.get_calls == [("m1", "minimal")]


def test_label_ids_defaults_to_empty_when_message_has_none():
    service = _Service()

    assert GmailWriter(service).label_ids("m1") == []


def test_ensure_label_returns_existing_id_without_creating():
    service = _Service(existing_labels=[{"id": "Label_1", "name": "classifier/promo"}])

    label_id = GmailWriter(service).ensure_label("classifier/promo")

    assert label_id == "Label_1"
    assert service.labels.created == []


def test_ensure_label_matches_case_insensitively():
    service = _Service(existing_labels=[{"id": "Label_1", "name": "Promo"}])

    assert GmailWriter(service).ensure_label("promo") == "Label_1"
    assert service.labels.created == []


def test_ensure_label_creates_missing_label():
    service = _Service(existing_labels=[{"id": "existing_1", "name": "other"}])
    writer = GmailWriter(service)

    label_id = writer.ensure_label("classifier/promo")

    assert service.labels.created == [{"name": "classifier/promo"}]
    assert label_id == "Label_1"
    # Second call is idempotent: the label now exists, no second create.
    assert writer.ensure_label("classifier/promo") == label_id
    assert len(service.labels.created) == 1


def test_scope_is_gmail_modify_only():
    assert client.SCOPES == ["https://www.googleapis.com/auth/gmail.modify"]


@pytest.mark.parametrize(
    ("granted", "ok"),
    [
        (["https://www.googleapis.com/auth/gmail.modify"], True),
        (["https://www.googleapis.com/auth/gmail.readonly"], False),
        ([], False),
    ],
)
def test_stale_readonly_token_forces_reconsent(granted, ok):
    assert client._has_required_scopes({"scopes": granted}) is ok


def test_token_without_scopes_field_forces_reconsent():
    assert client._has_required_scopes({}) is False
