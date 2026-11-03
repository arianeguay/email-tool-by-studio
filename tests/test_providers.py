"""Provider dispatch tests (STU-324): factory routing and unknown-provider failure."""

import pytest

from email_classifier import providers
from email_classifier.apply.gmail import GmailDrafter, GmailWriter
from email_classifier.apply.outlook import OutlookDrafter, OutlookWriter
from email_classifier.fetch.gmail import GmailFetcher
from email_classifier.fetch.outlook import OutlookFetcher


@pytest.fixture
def no_oauth(monkeypatch):
    monkeypatch.setattr(providers, "_gmail_service", lambda interactive=True: object())
    monkeypatch.setattr(providers, "_graph_session", lambda: object())
    monkeypatch.setattr(providers, "_graph_write_session", lambda interactive=True: object())


def test_get_fetcher_gmail(no_oauth):
    assert isinstance(providers.get_fetcher("gmail"), GmailFetcher)


def test_get_writer_gmail(no_oauth):
    assert isinstance(providers.get_writer("gmail"), GmailWriter)


def test_get_fetcher_outlook(no_oauth):
    assert isinstance(providers.get_fetcher("outlook"), OutlookFetcher)


def test_get_writer_outlook(no_oauth):
    assert isinstance(providers.get_writer("outlook"), OutlookWriter)


def test_get_writer_outlook_carries_the_headless_flag_to_the_session(monkeypatch):
    """STU-930: the webhook has no browser, so a dead grant must refuse there."""
    seen = []
    monkeypatch.setattr(providers, "_graph_write_session", lambda interactive=True: seen.append(interactive))

    providers.get_writer("outlook", interactive=False)

    assert seen == [False]


def test_get_writer_gmail_carries_the_headless_flag_to_the_service(monkeypatch):
    """STU-948: same for Gmail, where consent would hang rather than raise."""
    seen = []
    monkeypatch.setattr(providers, "_gmail_service", lambda interactive=True: seen.append(interactive))

    providers.get_writer("gmail", interactive=False)

    assert seen == [False]


def test_the_environment_makes_a_drafter_non_interactive(monkeypatch):
    """The webhook cannot pass `interactive=False` into a `studio` child, so the
    environment carries it instead (STU-946)."""
    seen = []
    monkeypatch.setenv(providers.HEADLESS_ENV, "1")
    monkeypatch.setattr(
        "email_classifier.fetch.outlook.build_session",
        lambda scopes=None, interactive=True: seen.append(interactive),
    )

    providers.get_drafter("outlook")

    assert seen == [False]


def test_an_explicit_argument_still_wins_over_the_environment(monkeypatch):
    monkeypatch.setenv(providers.HEADLESS_ENV, "1")

    assert providers._interactive(True) is True
    assert providers._interactive(None) is False


def test_get_drafter_gmail(no_oauth):
    assert isinstance(providers.get_drafter("gmail"), GmailDrafter)


def test_get_drafter_outlook(no_oauth):
    assert isinstance(providers.get_drafter("outlook"), OutlookDrafter)


def test_get_body_fetcher_outlook(no_oauth):
    assert isinstance(providers.get_body_fetcher("outlook"), OutlookFetcher)


def test_gmail_has_no_body_fetcher():
    with pytest.raises(ValueError, match="gmail"):
        providers.get_body_fetcher("gmail")


@pytest.mark.parametrize(
    "factory",
    [providers.get_body_fetcher, providers.get_thread_reader,
     providers.get_precedent_searcher],
)
def test_can_draft_answers_for_the_factories_that_refuse(factory):
    """The predicate a caller skips ahead on is the one the factories raise on."""
    assert providers.can_draft("outlook")
    assert not providers.can_draft("gmail")
    with pytest.raises(ValueError, match="gmail"):
        factory("gmail")


@pytest.mark.parametrize(
    "factory", [providers.get_fetcher, providers.get_writer, providers.get_drafter]
)
def test_unknown_provider_fails_loud(factory):
    with pytest.raises(ValueError, match="yahoo"):
        factory("yahoo")


def test_configured_mailbox_reads_the_client_the_fetcher_would_open(monkeypatch):
    """STU-1183: one declaration, resolved where the reading happens."""
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")
    assert providers.configured_mailbox("outlook") == "inbox@club.example"


def test_configured_mailbox_is_null_where_nothing_names_one(monkeypatch):
    """Gmail names no mailbox, and a delegated Graph grant addresses `/me`."""
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")
    assert providers.configured_mailbox("gmail") is None
    monkeypatch.delenv("OUTLOOK_MAILBOX")
    assert providers.configured_mailbox("outlook") is None


def test_configured_mailbox_refuses_an_unknown_provider():
    """A null there would read as "nothing names one", which is a guess."""
    with pytest.raises(ValueError):
        providers.configured_mailbox("imap")
