"""Tests for the `deliver` command's round shaping and threading report (STU-968).

No relay and no mailbox: what is under test is which chains a round is sent in,
and what the command says about the conversations the store landed them under.
"""

import argparse
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.test_seed_cli import load_script

deliver_fixtures = load_script("deliver_fixtures")


def _fixture(name, thread, index):
    return {
        "message_id": name,
        "thread_id": thread,
        "from_address": f"{name}@example.ca",
        "subject": name,
        "headers": {},
        "body_snippet": name,
        "date": f"2026-06-0{index}T00:00:00+00:00",
    }


def _thread_corpus(tmp_path: Path, conversations: dict[str, int]) -> Path:
    for name, length in conversations.items():
        directory = tmp_path / "threads" / name
        directory.mkdir(parents=True)
        for index in range(1, length + 1):
            fixture = _fixture(f"{name}_{index:02d}", f"t_{name}", index)
            (directory / f"{index:02d}.json").write_text(
                json.dumps(fixture), encoding="utf-8"
            )
    return tmp_path / "threads"


def _args(**overrides):
    fields = {"threads": True, "mix": "", "count": None, "random_seed": 0}
    return argparse.Namespace(**{**fields, **overrides})


def test_a_thread_round_keeps_each_conversation_whole_and_in_arrival_order(tmp_path):
    corpus = _thread_corpus(tmp_path, {"alpha": 3, "beta": 2})

    chains = deliver_fixtures._draw(_args(corpus=corpus))

    assert [len(chain) for chain in chains] == [3, 2]
    assert [m.message_id for m in chains[0]] == ["alpha_01", "alpha_02", "alpha_03"]


def test_count_limits_the_conversations_not_the_messages(tmp_path):
    corpus = _thread_corpus(tmp_path, {"alpha": 3, "beta": 2})

    chains = deliver_fixtures._draw(_args(corpus=corpus, count=1))

    assert [len(chain) for chain in chains] == [3]


def test_a_flat_round_is_one_chain_per_message(tmp_path):
    bucket = tmp_path / "corpus" / "bucket_a"
    bucket.mkdir(parents=True)
    for index in (1, 2):
        (bucket / f"a{index}.json").write_text(
            json.dumps(_fixture(f"a{index}", f"t_a{index}", index)), encoding="utf-8"
        )

    chains = deliver_fixtures._draw(_args(corpus=tmp_path / "corpus", threads=False))

    assert [len(chain) for chain in chains] == [1, 1]


def test_a_bucket_mix_over_a_thread_round_is_refused(tmp_path):
    corpus = _thread_corpus(tmp_path, {"alpha": 2})

    with pytest.raises(SystemExit, match="--mix does not apply"):
        deliver_fixtures._draw(_args(corpus=corpus, mix="bucket_a=100"))


def test_a_conversation_that_landed_under_one_id_is_not_reported(tmp_path, capsys):
    chains = deliver_fixtures._draw(_args(corpus=_thread_corpus(tmp_path, {"alpha": 2})))

    split = deliver_fixtures._report_threading(
        chains, {"alpha_01": "conv_1", "alpha_02": "conv_1"}
    )

    assert split == 0
    assert capsys.readouterr().err == ""


def test_a_conversation_the_store_split_is_named(tmp_path, capsys):
    chains = deliver_fixtures._draw(_args(corpus=_thread_corpus(tmp_path, {"alpha": 2})))

    split = deliver_fixtures._report_threading(
        chains, {"alpha_01": "conv_1", "alpha_02": "conv_2"}
    )

    assert split == 1
    assert "t_alpha: landed across 2 conversations" in capsys.readouterr().err


def test_a_conversation_only_half_delivered_is_not_called_split(tmp_path):
    """A message still in flight is a delivery result, not a threading one."""
    chains = deliver_fixtures._draw(_args(corpus=_thread_corpus(tmp_path, {"alpha": 2})))

    assert deliver_fixtures._report_threading(chains, {"alpha_01": "conv_1"}) == 0


class _FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, health):
    """Answer /healthz with `health`, or refuse the connection when it is None."""
    def urlopen(url, timeout=None):
        if health is None:
            raise OSError("connection refused")
        return _FakeResponse(health)

    monkeypatch.setattr(deliver_fixtures.urllib.request, "urlopen", urlopen)


def test_the_stamp_is_read_off_the_webhook_not_the_local_checkout(monkeypatch, capsys):
    """The staging webhook stamps its own host, so a local read never moves (STU-1019)."""
    polled = {}

    def urlopen(url, timeout=None):
        polled["url"] = url
        return _FakeResponse({"last_notification_at": "2026-08-28T12:00:00+00:00"})

    monkeypatch.setenv("WEBHOOK_HEALTH_URL", "http://serving-host.lan:9999/healthz")
    monkeypatch.setattr(deliver_fixtures.urllib.request, "urlopen", urlopen)

    deliver_fixtures._report_notification(
        deliver_fixtures.address.configured_health_url(),
        {"last_notification_at": "2026-08-28T11:00:00+00:00"},
    )

    assert polled["url"] == "http://serving-host.lan:9999/healthz"
    assert "received one at 2026-08-28T12:00:00+00:00" in capsys.readouterr().out


def test_the_stamp_is_read_through_the_forward_when_one_is_named(monkeypatch, capsys):
    """The staging /healthz answers on its own loopback only (STU-1027)."""
    polled = {}

    def urlopen(url, timeout=None):
        polled["url"] = url
        return _FakeResponse({"last_notification_at": "2026-08-28T12:00:00+00:00"})

    @contextmanager
    def reachable(url):
        polled["asked"] = url
        yield "http://127.0.0.1:41234/healthz"

    monkeypatch.setattr(deliver_fixtures.tunnel, "reachable", reachable)
    monkeypatch.setattr(deliver_fixtures.urllib.request, "urlopen", urlopen)

    deliver_fixtures._report_notification("http://127.0.0.1:18080/healthz", None)

    assert polled == {
        "asked": "http://127.0.0.1:18080/healthz",
        "url": "http://127.0.0.1:41234/healthz",
    }
    assert "received one at 2026-08-28T12:00:00+00:00" in capsys.readouterr().out


def test_a_stamp_the_round_did_not_move_is_still_warned_about(monkeypatch, capsys):
    _serve(monkeypatch, {"last_notification_at": "2026-08-28T11:00:00+00:00"})

    deliver_fixtures._report_notification(
        "http://webhook/healthz", {"last_notification_at": "2026-08-28T11:00:00+00:00"}
    )

    assert "[WARN]" in capsys.readouterr().err


def test_no_webhook_answering_is_reported_without_claiming_a_missed_notification(
    monkeypatch, capsys
):
    """Unreachable says nothing about what Graph sent — the false alarm of STU-1019."""
    _serve(monkeypatch, None)

    deliver_fixtures._report_notification("http://webhook/healthz", None)

    captured = capsys.readouterr()
    assert "no webhook answering at http://webhook/healthz" in captured.out
    assert captured.err == ""


def test_a_webhook_that_has_never_been_notified_is_not_a_missed_notification(
    monkeypatch, capsys
):
    _serve(monkeypatch, {"last_notification_at": None})

    deliver_fixtures._report_notification("http://webhook/healthz", None)

    captured = capsys.readouterr()
    assert "has received none, ever" in captured.out
    assert captured.err == ""
