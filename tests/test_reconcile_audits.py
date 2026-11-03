"""What `make pull-webhook` may discard on the host and what stops it (STU-1081).

The host writes `audits/skipped.json` and the workstation commits the same entry, so the
uncommitted change the pull aborts on is usually the merge's own content. The comparison
is what makes clearing it safe, so the tests that matter are the ones where it must not:
an entry the incoming version does not carry, a same-run entry whose reason was edited,
and a file that cannot be read as an entry list at all.
"""

import json
import subprocess

import pytest

from scripts import reconcile_audits

SKIPPED = "audits/skipped.json"


def entry(run_id, reason="a throwaway run"):
    return {"run_id": run_id, "date": "2026-08-29", "reason": reason}


def dumped(entries):
    return json.dumps(entries, indent=2, ensure_ascii=False) + "\n"


def test_an_identical_working_copy_loses_nothing():
    same = dumped([entry("r1")])
    assert reconcile_audits.lost_entries(same, same) == []


def test_a_working_copy_the_commit_extended_loses_nothing():
    local = dumped([entry("r1")])
    incoming = dumped([entry("r1"), entry("r2")])
    assert reconcile_audits.lost_entries(local, incoming) == []


def test_an_entry_the_pull_does_not_carry_is_lost():
    local = dumped([entry("r1"), entry("r2")])
    incoming = dumped([entry("r1")])
    assert reconcile_audits.lost_entries(local, incoming) == [entry("r2")]


def test_a_run_whose_reason_was_edited_is_lost():
    """The run id is carried; the reason is not, and the reason is the audit."""
    local = dumped([entry("r1", "what the host wrote")])
    incoming = dumped([entry("r1", "what the commit says")])
    assert reconcile_audits.lost_entries(local, incoming) == [entry("r1", "what the host wrote")]


@pytest.mark.parametrize("unreadable", ["{not json", '{"run_id": "r1"}', '["r1"]'])
def test_a_file_that_is_not_an_entry_list_is_unanswerable(unreadable):
    """None, never [] — unreadable must not read as nothing-to-lose."""
    assert reconcile_audits.lost_entries(unreadable, dumped([entry("r1")])) is None
    assert reconcile_audits.lost_entries(dumped([entry("r1")]), unreadable) is None


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def host(tmp_path):
    """A clone one commit behind an origin that extended audits/skipped.json."""
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "t@t")
    git(origin, "config", "user.name", "t")
    (origin / "audits").mkdir()
    (origin / SKIPPED).write_text(dumped([entry("r1")]), encoding="utf-8")
    (origin / "audits" / "audit_log.json").write_text(dumped([entry("r9")]), encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "one")

    clone = tmp_path / "host"
    git(tmp_path, "clone", "-q", str(origin), str(clone))

    (origin / SKIPPED).write_text(dumped([entry("r1"), entry("r2")]), encoding="utf-8")
    git(origin, "commit", "-qam", "two")
    return clone


def reconciled(clone):
    return reconcile_audits.main(["--repo", str(clone)])


def status(clone):
    return reconcile_audits._git("status", "--porcelain", repo=clone)


def test_the_pull_completes_over_a_working_copy_it_already_carries(host, capsys):
    (host / SKIPPED).write_text(dumped([entry("r1"), entry("r2")]), encoding="utf-8")

    assert reconciled(host) == 0
    assert status(host) == ""
    assert SKIPPED in capsys.readouterr().out
    git(host, "merge", "--ff-only", "FETCH_HEAD")


def test_the_pull_stops_and_names_the_run_it_would_have_dropped(host, capsys):
    divergent = dumped([entry("r1"), entry("r2"), entry("r3")])
    (host / SKIPPED).write_text(divergent, encoding="utf-8")

    assert reconciled(host) == 1
    assert "r3" in capsys.readouterr().err
    assert (host / SKIPPED).read_text(encoding="utf-8") == divergent


def test_a_dirty_file_the_merge_does_not_touch_is_left_alone(host):
    """Only what the pull aborts on is a candidate; the rest is a local change."""
    (host / "audits" / "audit_log.json").write_text(dumped([entry("r9"), entry("r10")]),
                                                    encoding="utf-8")

    assert reconciled(host) == 0
    assert "audit_log.json" in status(host)
