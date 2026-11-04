"""Tests for the apply CLI (STU-407): dry-run default, journal paths, undo."""

import json
from pathlib import Path

import pytest

import apply_email
from email_classifier.apply.executor import PROCESSED_LABEL
from email_classifier.apply.journal import read_entries
from tests.conftest import FakeWriter, inboxed
from tests.test_undo import FakeDrafter
from email_classifier import deployment

# The real deployment taxonomy, not `tmp_path`-relative: unlike `policy_file` below,
# nothing here exercises a specific bucket shape, so there is nothing to gain from a
# taxonomy the test owns, and a test that monkeypatches `apply_email.REPO_ROOT` (STU-1461)
# must not also lose the file `--taxonomy` defaults to when nobody names one.
REAL_TAXONOMY = deployment.ROOT / "rules" / "taxonomy.yaml"


def policy_file(tmp_path, provider="gmail"):
    """A policy the tests own — rules/policy.yaml is deployment config, and an edit
    there must not decide whether a unit test reaches for real credentials."""
    path = tmp_path / f"policy-{provider}.yaml"
    path.write_text(
        "deterministic_can_finalize: true\n"
        "destructive_actions_allowed: [mark_for_delete]\n"
        f"email:\n  provider: {provider}\n",
        encoding="utf-8",
    )
    return path


def write_report(tmp_path, run_id="run-1", action="label_archive"):
    report = {
        "run_id": run_id,
        "verdicts": [{
            "message_id": "m1", "thread_id": "t1", "path": "deterministic", "status": "ok",
            "bucket": "B", "suggested_action": action, "confidence": "high",
            "rule_id": "r1", "subtype": None, "reasoning": None, "error": None,
            "error_detail": None,
        }],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


@pytest.fixture
def writer(monkeypatch):
    fake = inboxed(FakeWriter(), "m1")
    monkeypatch.setattr(apply_email, "_writer", lambda policy: fake)
    return fake


def cli(tmp_path, *argv):
    return apply_email.main([
        "--journal-dir", str(tmp_path / "applied"),
        "--policy", str(policy_file(tmp_path)),
        "--taxonomy", str(REAL_TAXONOMY),
        "--draft-journal", str(tmp_path / "drafts.jsonl"),
        *argv,
    ])


def test_run_without_apply_is_a_dry_run(tmp_path, writer, capsys):
    report = write_report(tmp_path)

    assert cli(tmp_path, "run", str(report)) == 0

    assert writer.labels_of("m1") == {"INBOX"}
    assert "dry-run, no mutation" in capsys.readouterr().out
    plan = read_entries(tmp_path / "applied" / "run-1.plan.jsonl")
    assert {e["status"] for e in plan} == {"planned"}
    assert not (tmp_path / "applied" / "run-1.jsonl").exists()


def test_run_with_apply_mutates_and_journals(tmp_path, writer):
    report = write_report(tmp_path)

    assert cli(tmp_path, "run", str(report), "--apply") == 0

    assert writer.labels_of("m1") == {"email-tool/B", PROCESSED_LABEL}
    journaled = read_entries(tmp_path / "applied" / "run-1.jsonl")
    assert {e["status"] for e in journaled} == {"ok"}


def test_undo_reverts_an_applied_run(tmp_path, writer):
    report = write_report(tmp_path)
    cli(tmp_path, "run", str(report), "--apply")

    assert cli(tmp_path, "undo", "run-1") == 0

    assert writer.labels_of("m1") == {"INBOX"}
    assert (tmp_path / "applied" / "run-1.undo.jsonl").exists()


def test_undo_without_journal_fails_loud(tmp_path, writer, capsys):
    assert cli(tmp_path, "undo", "nope") == 1
    assert "no apply journal" in capsys.readouterr().err


def drafted(tmp_path, message_id="m1", draft_id="d1"):
    """A drafts journal saying the run put `draft_id` in the mailbox for `message_id`."""
    path = tmp_path / "drafts.jsonl"
    path.write_text(json.dumps({
        "timestamp": "2026-08-20T10:00:00+00:00", "message_id": message_id,
        "thread_id": "t1", "draft_id": draft_id, "status": "ok", "error": None,
    }) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def drafter(monkeypatch):
    fake = FakeDrafter({"d1": {"id": "d1", "isDraft": True,
                               "lastModifiedDateTime": "2026-08-20T09:59:58Z"}})
    monkeypatch.setattr(apply_email, "get_drafter", lambda provider: fake)
    return fake


def test_undo_deletes_the_drafts_the_run_created(tmp_path, writer, drafter, capsys):
    """STU-854: labels reverted and drafts left behind is a half-reverted run."""
    report = write_report(tmp_path, action="needs_reply")
    cli(tmp_path, "run", str(report), "--apply")
    drafted(tmp_path)

    assert cli(tmp_path, "undo", "run-1", "--report", str(report)) == 0

    assert drafter.deleted == ["d1"]
    assert "drafts: 1 deleted" in capsys.readouterr().out


def test_undo_reverts_the_drafts_of_a_run_that_only_planned(tmp_path, writer, drafter,
                                                            capsys):
    """Drafting is outside `auto_apply` (STU-946), so under the default policy a push
    run leaves a plan and a real draft — and the draft is the whole thing to revert."""
    report = write_report(tmp_path, action="needs_reply")
    cli(tmp_path, "run", str(report))
    drafted(tmp_path)

    assert cli(tmp_path, "undo", "run-1", "--report", str(report)) == 0

    assert drafter.deleted == ["d1"]
    assert writer.labels_of("m1") == {"INBOX"}
    out = capsys.readouterr().out
    assert "0 operations reverted" in out
    assert "drafts: 1 deleted" in out


def test_undo_finds_the_report_next_to_the_run_id(tmp_path, writer, drafter, monkeypatch):
    """The default `--report` is `reports/<run-id>.json` — the path `run_email.py` wrote."""
    monkeypatch.setattr(apply_email, "REPO_ROOT", tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    write_report(tmp_path, action="needs_reply").rename(reports / "run-1.json")
    cli(tmp_path, "run", str(reports / "run-1.json"), "--apply")
    drafted(tmp_path)

    assert cli(tmp_path, "undo", "run-1") == 0

    assert drafter.deleted == ["d1"]


def test_undo_without_a_report_reverts_the_labels_and_says_the_drafts_stay(
    tmp_path, writer, drafter, capsys
):
    cli(tmp_path, "run", str(write_report(tmp_path)), "--apply")
    drafted(tmp_path)

    assert cli(tmp_path, "undo", "run-1", "--report", str(tmp_path / "gone.json")) == 0

    assert writer.labels_of("m1") == {"INBOX"}
    assert drafter.deleted == []
    assert "drafts not reverted" in capsys.readouterr().err


def test_a_draft_that_cannot_be_deleted_fails_the_undo(tmp_path, writer, drafter, capsys):
    report = write_report(tmp_path, action="needs_reply")
    cli(tmp_path, "run", str(report), "--apply")
    drafted(tmp_path)
    drafter.fail_on.add("d1")

    assert cli(tmp_path, "undo", "run-1", "--report", str(report)) == 2

    assert "ERROR m1 draft d1" in capsys.readouterr().out


def test_missing_report_fails_loud(tmp_path, writer, capsys):
    assert cli(tmp_path, "run", str(tmp_path / "nope.json")) == 1
    assert "report not found" in capsys.readouterr().err


def test_command_is_required():
    with pytest.raises(SystemExit):
        apply_email.parse_args([])


def outlook_policy(tmp_path, monkeypatch, *scopes):
    """Point the CLI at an Outlook mailbox whose cached grant carries `scopes`."""
    token = tmp_path / "outlook-token.json"
    token.write_text(
        json.dumps({"AccessToken": {"k": {"target": " ".join(scopes)}}}), encoding="utf-8"
    )
    monkeypatch.setenv("OUTLOOK_TOKEN_FILE", str(token))
    return [
        "--journal-dir", str(tmp_path / "applied"),
        "--policy", str(policy_file(tmp_path, "outlook")),
        "--taxonomy", str(REAL_TAXONOMY),
    ]


def test_apply_refuses_a_read_only_outlook_grant(tmp_path, monkeypatch, writer, capsys):
    """STU-844: refuse at startup, name the scope, mutate nothing."""
    argv = outlook_policy(tmp_path, monkeypatch, "Mail.Read")
    report = write_report(tmp_path)

    assert apply_email.main([*argv, "run", str(report), "--apply"]) == 1

    error = capsys.readouterr().err
    assert "Mail.ReadWrite" in error and "make outlook-consent" in error
    assert writer.labels_of("m1") == {"INBOX"}
    assert not (tmp_path / "applied").exists()


def test_a_dry_run_on_a_read_only_outlook_grant_still_plans(tmp_path, monkeypatch, writer):
    """STU-844: a dry-run issues no mutation, so it needs no write scope."""
    argv = outlook_policy(tmp_path, monkeypatch, "Mail.Read")
    report = write_report(tmp_path)

    assert apply_email.main([*argv, "run", str(report)]) == 0

    assert writer.labels_of("m1") == {"INBOX"}
    assert (tmp_path / "applied" / "run-1.plan.jsonl").exists()


def test_apply_proceeds_on_a_re_consented_outlook_grant(tmp_path, monkeypatch, writer):
    argv = outlook_policy(tmp_path, monkeypatch, "Mail.ReadWrite")
    report = write_report(tmp_path)

    assert apply_email.main([*argv, "run", str(report), "--apply"]) == 0

    assert writer.labels_of("m1") == {"email-tool/B", PROCESSED_LABEL}


def test_undo_refuses_a_read_only_outlook_grant(tmp_path, monkeypatch, writer, capsys):
    """Undo mutates too — same gate."""
    argv = outlook_policy(tmp_path, monkeypatch, "Mail.ReadWrite")
    apply_email.main([*argv, "run", str(write_report(tmp_path)), "--apply"])
    read_only = outlook_policy(tmp_path, monkeypatch, "Mail.Read")

    assert apply_email.main([*read_only, "undo", "run-1"]) == 1

    assert "Mail.ReadWrite" in capsys.readouterr().err
    assert writer.labels_of("m1") == {"email-tool/B", PROCESSED_LABEL}
