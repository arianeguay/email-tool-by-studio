"""Tests for the SMTP delivery path (STU-934).

No relay, no mailbox: the message builder is pure, and the Graph side runs
against the same kind of fake session the seeder's tests use.
"""

import pytest

from email_classifier.core.types import EmailInput
from email_classifier.seed import deliver, outlook


class _Session:
    """Fake GraphSession serving canned Inbox pages and recording writes."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.writes = []
        self.params = []

    def get(self, url, params=None):
        self.params.append(params)
        return self._pages.pop(0) if self._pages else {"value": []}

    def request(self, method, url, params=None, payload=None):
        self.writes.append((method, url, payload))
        return {}


def _email(**overrides):
    fields = {
        "message_id": "msg_1",
        "thread_id": "t_1",
        "from_address": "newsletter@example-gestion-sportive.com",
        "subject": "Infolettre d'août",
        "headers": {"List-Unsubscribe": "<https://example.com/unsub>"},
        "body_snippet": "Nos études de cas du mois.",
        "date": "2026-08-13T14:10:00+00:00",
        "provider_category": "promotions",
    }
    return EmailInput(**{**fields, **overrides})


def _arrival(fixture, run_id, message_id):
    return {
        "id": message_id,
        "internetMessageHeaders": [
            {"name": "X-Email-Classifier-Seed", "value": run_id},
            {"name": "X-Email-Classifier-Fixture", "value": fixture},
        ],
    }


def test_the_message_carries_the_bulk_headers_a_store_write_cannot_set():
    message = deliver.build_message(_email(), "run-1", "sender@gmail.com", "test@outlook.com")

    assert message["List-Unsubscribe"] == "<https://example.com/unsub>"
    assert message["Subject"] == "Infolettre d'août"
    assert message.get_content().strip() == "Nos études de cas du mois."


def test_the_fixture_sender_travels_beside_the_relays_own_from():
    message = deliver.build_message(_email(), "run-1", "sender@gmail.com", "test@outlook.com")

    assert message["From"] == "sender@gmail.com"
    assert message["To"] == "test@outlook.com"
    assert message["Reply-To"] == "newsletter@example-gestion-sportive.com"
    assert message[deliver.FIXTURE_FROM_HEADER] == "newsletter@example-gestion-sportive.com"


def test_both_seed_markers_are_carried_so_teardown_recognizes_the_round():
    message = deliver.build_message(_email(), "run-1", "sender@gmail.com", "test@outlook.com")

    assert message[outlook.HEADER] == "run-1"
    assert message[outlook.FIXTURE_HEADER] == "msg_1"


def test_a_fixture_with_no_sender_gets_no_empty_reply_to():
    message = deliver.build_message(
        _email(from_address=""), "run-1", "sender@gmail.com", "test@outlook.com"
    )

    assert message["Reply-To"] is None
    assert message[deliver.FIXTURE_FROM_HEADER] is None


def test_delivering_from_the_mailbox_under_test_is_refused():
    config = deliver.SmtpConfig("smtp.example.com", 587, "u", "p", "Test@Outlook.com")

    with pytest.raises(RuntimeError, match="mailbox under test"):
        deliver.assert_outside_account(config, "test@outlook.com")


def test_the_smtp_config_names_every_missing_variable_at_once(monkeypatch):
    for name in ("DELIVER_SMTP_HOST", "DELIVER_SMTP_USER", "DELIVER_SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError) as caught:
        deliver.SmtpConfig.from_env()
    assert "DELIVER_SMTP_HOST, DELIVER_SMTP_USER, DELIVER_SMTP_PASSWORD" in str(caught.value)


def test_the_sender_defaults_to_the_authenticated_user(monkeypatch):
    monkeypatch.setenv("DELIVER_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("DELIVER_SMTP_USER", "sender@gmail.com")
    monkeypatch.setenv("DELIVER_SMTP_PASSWORD", "secret")
    monkeypatch.delenv("DELIVER_FROM", raising=False)

    config = deliver.SmtpConfig.from_env()

    assert config.sender == "sender@gmail.com"
    assert config.port == 587


def test_arrivals_are_keyed_by_fixture_and_scoped_to_the_run():
    session = _Session([
        {"value": [_arrival("msg_1", "run-1", "id_1"),
                   _arrival("msg_2", "run-0", "id_old")]},
    ])

    assert deliver.arrived(session, "run-1") == {"msg_1": "id_1"}


def test_arrivals_follow_the_next_link():
    session = _Session([
        {"value": [_arrival("msg_1", "run-1", "id_1")], "@odata.nextLink": "https://next"},
        {"value": [_arrival("msg_2", "run-1", "id_2")]},
    ])

    assert deliver.arrived(session, "run-1") == {"msg_1": "id_1", "msg_2": "id_2"}
    assert session.params[1] is None  # the nextLink already carries the query options


def test_waiting_stops_as_soon_as_the_whole_round_has_landed():
    session = _Session([
        {"value": []},
        {"value": [_arrival("msg_1", "run-1", "id_1")]},
    ])
    slept = []

    found, waited = deliver.await_arrival(
        session, "run-1", 1, interval=10.0,
        now=iter([0.0, 0.0, 10.0]).__next__, sleep=slept.append,
    )

    assert found == {"msg_1": "id_1"}
    assert slept == [10.0]
    assert waited == 10.0


def test_a_partial_delivery_comes_back_at_the_timeout_instead_of_raising():
    session = _Session([{"value": [_arrival("msg_1", "run-1", "id_1")]}])

    found, waited = deliver.await_arrival(
        session, "run-1", 2, timeout=30.0, now=iter([0.0, 30.0]).__next__,
        sleep=lambda _: None,
    )

    assert found == {"msg_1": "id_1"}
    assert waited == 30.0


def test_arrived_messages_are_categorized_so_unseed_finds_them():
    session = _Session([])

    deliver.categorize(session, ["id_1"])

    assert session.writes == [
        ("PATCH", "/me/messages/id_1", {"categories": [outlook.CATEGORY]})
    ]


def test_the_two_structural_losses_are_not_reported_as_divergence():
    landed = _email(from_address="sender@gmail.com", date="2026-08-22T09:00:00+00:00")

    assert deliver.unexpected_divergence(_email(), landed) == []


def test_a_dropped_header_is_reported_as_divergence():
    landed = _email(from_address="sender@gmail.com", headers={})

    assert deliver.unexpected_divergence(_email(), landed) == ["headers"]


def _dump(**overrides):
    item = {
        "message_id": "mailbox_id",
        "thread_id": "conv",
        "from_address": "newsletter@example-gestion-sportive.com",
        "subject": "Infolettre d'août",
        "headers": {"List-Unsubscribe": "<https://example.com/unsub>"},
        "body_snippet": "Nos études de cas du mois.",
        "date": "2026-08-13T14:10:00+00:00",
        "provider_category": "promotions",
    }
    return {**item, **overrides}


def test_comparing_names_the_fields_the_two_paths_disagree_on():
    seeded = [_dump(message_id="seeded_id", headers={})]
    delivered = [_dump(message_id="delivered_id", from_address="sender@gmail.com")]

    differences, unmatched = deliver.compare(delivered, seeded)

    assert unmatched == []
    assert differences[0]["subject"] == "Infolettre d'août"
    assert set(differences[0]["fields"]) == {"from_address", "headers"}


def test_comparing_ignores_how_each_path_reflowed_the_body():
    seeded = [_dump(body_snippet="Nos  études\nde cas du mois.")]
    delivered = [_dump(body_snippet="Nos études de cas du mois.")]

    differences, _ = deliver.compare(delivered, seeded)

    assert differences == []


def test_a_subject_only_one_round_holds_is_reported_from_either_side():
    seeded = [_dump(subject="Seeded only")]
    delivered = [_dump(subject="Delivered only")]

    _, unmatched = deliver.compare(delivered, seeded)

    assert sorted(unmatched) == ["Delivered only", "Seeded only"]


def _chain():
    return [
        _email(message_id="msg_1", thread_id="thread_x", subject="Catégorie de ma fille"),
        _email(message_id="msg_2", thread_id="thread_x", subject="Re: Catégorie de ma fille"),
        _email(message_id="msg_3", thread_id="thread_x", subject="Re: Catégorie de ma fille"),
    ]


def test_a_delivered_conversation_references_every_earlier_turn():
    built = deliver.build_chain(_chain(), "run-1", "sender@gmail.com", "test@outlook.com")
    ids = [message["Message-ID"] for message in built]

    assert built[0]["In-Reply-To"] is None
    assert built[0]["References"] is None
    assert built[1]["In-Reply-To"] == ids[0]
    assert built[1]["References"] == ids[0]
    assert built[2]["In-Reply-To"] == ids[1]
    assert built[2]["References"] == f"{ids[0]} {ids[1]}"


def test_the_message_id_is_minted_per_round_so_a_second_round_opens_its_own_conversation():
    first = deliver.build_chain(_chain(), "run-1", "sender@gmail.com", "test@outlook.com")
    second = deliver.build_chain(_chain(), "run-2", "sender@gmail.com", "test@outlook.com")

    assert first[0]["Message-ID"] == outlook.internet_message_id(_chain()[0], "run-1")
    assert not {m["Message-ID"] for m in first} & {m["Message-ID"] for m in second}
    assert second[1]["In-Reply-To"] == second[0]["Message-ID"]


def test_a_message_delivered_alone_still_carries_its_own_id():
    [message] = deliver.build_chain([_email()], "run-1", "sender@gmail.com", "test@outlook.com")

    assert message["Message-ID"] == outlook.internet_message_id(_email(), "run-1")
    assert message["In-Reply-To"] is None


class _Inbox:
    """Fake session whose Inbox holds the fixtures named, one page per poll."""

    def __init__(self, *polls):
        self._polls = list(polls)
        self.polled = 0

    def get(self, url, params=None):
        fixtures = self._polls.pop(0) if self._polls else ()
        self.polled += 1
        return {"value": [_arrival(f, "run-1", f"id_{f}") for f in fixtures]}


def _sent(submitted):
    return [message[outlook.FIXTURE_HEADER] for message in submitted]


def test_a_reply_is_submitted_only_once_its_parent_is_in_the_inbox():
    """The headers do not thread the chain on their own — the parent has to be there."""
    session = _Inbox((), ("msg_1",))
    seen_when_submitted, submitted = [], []

    unwaited = deliver.send_chain(
        lambda message: (submitted.append(message),
                         seen_when_submitted.append(session.polled)),
        session, _chain()[:2], "run-1", "sender@gmail.com", "test@outlook.com",
        interval=0.0,
    )

    assert _sent(submitted) == ["msg_1", "msg_2"]
    assert unwaited == []
    # The reply went out only after the poll that found the parent — a first poll
    # that came back empty was not taken for an answer.
    assert seen_when_submitted == [0, 2]


def test_a_flat_round_waits_on_nothing():
    session = _Inbox()
    submitted = []

    unwaited = deliver.send_chain(
        submitted.append, session, [_email()], "run-1", "sender@gmail.com",
        "test@outlook.com", interval=0.0,
    )

    assert _sent(submitted) == ["msg_1"]
    assert unwaited == []


def test_a_parent_that_never_lands_is_named_and_its_reply_sent_anyway():
    submitted = []

    unwaited = deliver.send_chain(
        submitted.append, _Inbox(), _chain()[:2], "run-1", "sender@gmail.com",
        "test@outlook.com", timeout=0.0, interval=0.0,
    )

    assert _sent(submitted) == ["msg_1", "msg_2"]
    assert unwaited == ["msg_1"]


def test_waiting_on_a_turn_ignores_a_straggler_from_another_conversation():
    session = _Inbox(("msg_other",), ("msg_other", "msg_1"))
    slept = []

    assert deliver.await_message(
        session, "run-1", "msg_1", interval=5.0,
        now=iter([0.0, 0.0, 5.0]).__next__, sleep=slept.append,
    )
    assert slept == [5.0]
