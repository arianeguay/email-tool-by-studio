from datetime import date
import json
from types import SimpleNamespace

import pytest

import run_email
from email_classifier import deployment
from email_classifier.stage1.policy import Policy
from tests.conftest import pin_gliner


def test_parse_args_requires_a_source():
    with pytest.raises(SystemExit):
        run_email.parse_args([])


def test_dump_and_fetch_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        run_email.parse_args(["--dump", "x.json", "--fetch"])


def test_load_emails_from_dump(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text(
        json.dumps([{
            "message_id": "m1", "thread_id": "t1", "from_address": "a@b.com", "subject": "s",
            "headers": {}, "body_snippet": "x", "date": "2026-06-10T00:00:00+00:00",
        }]),
        encoding="utf-8",
    )
    args = run_email.parse_args(["--dump", str(dump)])
    emails, input_path, applied_floor, mailbox = run_email._load_emails(
        args, Policy(True, [], False, False), "run-1", date(2026, 8, 19))
    assert len(emails) == 1
    assert emails[0].thread_id == "t1"
    assert input_path == dump
    # A frozen dump is a fetch somebody else already bounded, replayed: it keeps its
    # older mail, and the report says no floor rather than claiming one (STU-1123).
    assert applied_floor is None
    # Same for the mailbox (STU-1183): this run opened none, and the box this host is
    # pointed at today is routinely not the one those ids came out of.
    assert mailbox is None


def test_load_emails_missing_dump_exits(tmp_path):
    args = run_email.parse_args(["--dump", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit):
        run_email._load_emails(
        args, Policy(True, [], False, False), "run-1", date(2000, 1, 1))


def test_fetch_persists_the_fetched_inputs_as_a_dump(tmp_path, monkeypatch):
    from email_classifier import providers

    item = {
        "message_id": "m1", "thread_id": "t1", "from_address": "a@b.com", "subject": "s",
        "headers": {}, "body_snippet": "x", "date": "2026-06-10T00:00:00+00:00",
    }
    class _Fetcher:
        def fetch_inputs(self, query, max_results, since=None):
            return [item]

        def conversation_message_ids(self, thread_id):
            return [item["message_id"]]

    monkeypatch.setattr(providers, "get_fetcher", lambda provider: _Fetcher())

    args = run_email.parse_args(["--fetch", "--dump-dir", str(tmp_path)])
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")
    emails, input_path, applied_floor, mailbox = run_email._load_emails(
        args, Policy(True, [], False, False, "outlook"), "run-1", date(2000, 1, 1))

    assert len(emails) == 1
    assert json.loads((tmp_path / "run-1.json").read_text(encoding="utf-8")) == [item]
    # The dump a fetch run just wrote is the input its provenance must name (STU-859).
    assert input_path == tmp_path / "run-1.json"
    # And the floor it was read under, which the report then publishes (STU-1123).
    assert applied_floor == date(2000, 1, 1)
    # And the mailbox it opened, resolved off the client that opened it (STU-1183).
    assert mailbox == "inbox@club.example"


def test_a_studio_ignoring_the_scope_aborts_before_any_fetch(monkeypatch, capsys):
    """STU-834: version skew must stop the run, not degrade it silently."""
    import shutil
    import subprocess

    from email_classifier import providers

    # The preflight refuses on the absent extra before it ever probes the version
    # (STU-908), so the skew this test is about is only reachable with a detector
    # installed. Pinning it keeps the assertion on the same clause on both machines.
    pin_gliner(monkeypatch)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/studio")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kwargs: SimpleNamespace(returncode=0, stdout="0.15.1\n", stderr=""),
    )
    monkeypatch.setattr(
        providers, "get_fetcher", lambda provider: pytest.fail("fetched despite version skew")
    )

    rules = deployment.ROOT / "rules"
    assert run_email.main([
        "--fetch", "--rules", str(rules / "rules.yaml"),
        "--policy", str(rules / "policy.yaml"), "--taxonomy", str(rules / "taxonomy.yaml"),
    ]) == 1
    assert "0.15.1" in capsys.readouterr().err
