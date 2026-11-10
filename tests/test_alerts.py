"""The out-of-band alert channels (STU-930): logged always, sent where configured."""

import logging

import pytest

from email_classifier import alerts

requests = pytest.importorskip("requests")


@pytest.fixture(autouse=True)
def _no_channel_configured(monkeypatch):
    """Neither channel, unless the test says so — the host's env must not leak in."""
    for name in ("ALERT_WEBHOOK_URL", "PUSHOVER_TOKEN", "PUSHOVER_USER"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def journal(monkeypatch):
    """The `logger` calls a send makes, captured so no test writes to the host's log."""
    lines = []

    def run(argv, **kwargs):
        lines.append(argv)

    monkeypatch.setattr(alerts.subprocess, "run", run)
    return lines


class FakePost:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, url, data=None, timeout=None):
        self.calls.append({"url": url, "data": data, "timeout": timeout})
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = 200
        response.url = url
        return response


@pytest.fixture
def post(monkeypatch):
    fake = FakePost()
    monkeypatch.setattr(requests, "post", fake)
    return fake


def _pushover(monkeypatch):
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")
    monkeypatch.setenv("PUSHOVER_USER", "usr")


def test_no_channel_configured_logs_and_posts_nothing(post, caplog):
    with caplog.at_level(logging.CRITICAL):
        alerts.notify("the grant is gone")

    assert "the grant is gone" in caplog.text
    assert post.calls == []


def test_configured_webhook_receives_the_message_as_plain_text(monkeypatch, post):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://ntfy.test/email-classifier")

    alerts.notify("the grant is gone")

    assert len(post.calls) == 1
    assert post.calls[0]["url"] == "https://ntfy.test/email-classifier"
    assert post.calls[0]["data"] == b"the grant is gone"


def test_an_unreachable_webhook_does_not_raise(monkeypatch, caplog):
    """The caller is already handling a failure; a second one would mask it."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://ntfy.test/email-classifier")
    monkeypatch.setattr(requests, "post", FakePost(error=requests.ConnectionError("no route")))

    with caplog.at_level(logging.ERROR):
        alerts.notify("the grant is gone")

    assert "the grant is gone" in caplog.text
    assert "not delivered" in caplog.text


# --- Pushover (STU-1066): form-encoded, not raw-bodied ------------------------


def test_pushover_gets_the_three_form_fields(monkeypatch, post):
    _pushover(monkeypatch)

    alerts.notify("the grant is gone")

    assert len(post.calls) == 1
    assert post.calls[0]["url"] == alerts.PUSHOVER_URL
    # A dict, not bytes: `requests` form-encodes it, which is the only shape the
    # messages endpoint accepts. A raw body is a 400.
    assert post.calls[0]["data"]["token"] == "tok"
    assert post.calls[0]["data"]["user"] == "usr"
    assert post.calls[0]["data"]["message"] == "the grant is gone"


def test_pushover_needs_both_halves_of_the_credential(monkeypatch, post):
    monkeypatch.setenv("PUSHOVER_TOKEN", "tok")

    alerts.notify("the grant is gone")

    assert post.calls == []


def test_an_over_long_message_is_truncated_rather_than_rejected(monkeypatch, post):
    """Pushover 400s a longer message, which would drop the alert entirely."""
    _pushover(monkeypatch)

    alerts.notify("x" * (alerts.PUSHOVER_MAX_MESSAGE + 500))

    assert len(post.calls[0]["data"]["message"]) == alerts.PUSHOVER_MAX_MESSAGE


def test_an_unreachable_pushover_does_not_raise(monkeypatch, caplog):
    _pushover(monkeypatch)
    monkeypatch.setattr(requests, "post", FakePost(error=requests.ConnectionError("no route")))

    with caplog.at_level(logging.ERROR):
        alerts.notify("the grant is gone")

    assert "the grant is gone" in caplog.text
    assert "not delivered" in caplog.text


def test_both_channels_configured_both_get_the_alert(monkeypatch, post):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://ntfy.test/email-classifier")
    _pushover(monkeypatch)

    alerts.notify("the grant is gone")

    assert [call["url"] for call in post.calls] == [
        "https://ntfy.test/email-classifier", alerts.PUSHOVER_URL,
    ]


def test_a_dead_channel_does_not_take_the_other_down(monkeypatch, caplog):
    """Independent sends: the whole point of a second channel is a second chance."""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://ntfy.test/email-classifier")
    _pushover(monkeypatch)
    delivered = []

    def post(url, data=None, timeout=None):
        if url != alerts.PUSHOVER_URL:
            raise requests.ConnectionError("no route")
        delivered.append(data)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        return response

    monkeypatch.setattr(requests, "post", post)

    with caplog.at_level(logging.ERROR):
        alerts.notify("the grant is gone")

    assert [entry["message"] for entry in delivered] == ["the grant is gone"]


# --- The pushover journal (STU-2134): a direct API call leaves the same record ---


def test_an_accepted_pushover_send_is_recorded_under_the_pushover_tag(monkeypatch, post, journal):
    _pushover(monkeypatch)

    alerts.notify("the grant is gone")

    assert [argv[:3] for argv in journal] == [["logger", "-t", "pushover"]] * 2
    sent, accepted = (argv[-1] for argv in journal)
    assert sent.startswith("envoi (priorite 0) : email-classifier\n")
    assert "the grant is gone" in sent
    assert accepted == "accepte : email-classifier"


def test_a_rejected_pushover_send_is_recorded_as_refused(monkeypatch, journal, caplog):
    _pushover(monkeypatch)
    monkeypatch.setattr(requests, "post", FakePost(error=requests.HTTPError("400 Bad Request")))

    with caplog.at_level(logging.ERROR):
        alerts.notify("the grant is gone")

    refused = journal[-1]
    assert refused[refused.index("-p") + 1] == "user.err"
    assert refused[-1] == "refuse : email-classifier : 400 Bad Request"
    assert "not delivered" in caplog.text


def test_no_pushover_configured_records_nothing(post, journal):
    alerts.notify("the grant is gone")

    assert journal == []


def test_a_host_without_logger_still_sends(monkeypatch, post):
    _pushover(monkeypatch)

    def missing(argv, **kwargs):
        raise FileNotFoundError("logger")

    monkeypatch.setattr(alerts.subprocess, "run", missing)

    alerts.notify("the grant is gone")

    assert [call["url"] for call in post.calls] == [alerts.PUSHOVER_URL]
