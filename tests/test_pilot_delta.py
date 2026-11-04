"""Placing a pilot round against the commit that drafted it (STU-1182)."""

import json
from pathlib import Path

from email_classifier import pilot_delta

REPO_ROOT = Path(__file__).resolve().parents[1]

# A sha of the right shape that no clone carries. Not a random string: the point is
# that the lookup fails on a well-formed reference, which is what a report naming a
# commit this checkout never fetched looks like.
ABSENT = "0123456789abcdef0123456789abcdef01234567"


def test_a_commit_this_clone_does_not_carry_is_unknown_not_pre_rules():
    """The third bucket is the whole reason there are three. Folding an unresolvable
    commit into `pre-rules` would let a missing object argue that the rules did nothing
    — a measurement nobody could take reading as one that came back empty (STU-912).
    Measured 2026-09-01: one of the fifteen commits the serving host's reports name is absent."""
    assert pilot_delta.cohort_of(ABSENT, REPO_ROOT) == pilot_delta.UNKNOWN


def test_a_report_with_no_commit_is_unknown():
    assert pilot_delta.cohort_of(None, REPO_ROOT) == pilot_delta.UNKNOWN


def _report(tmp_path, name, commit, run_id, mtime):
    path = tmp_path / name
    path.write_text(json.dumps({"run_id": run_id, "provenance": {
        "git_commit": commit, "mailbox": "inbox@example.test"}}), encoding="utf-8")
    import os
    os.utime(path, (mtime, mtime))
    return path


def test_rounds_skips_an_unreadable_report_rather_than_guessing(tmp_path):
    good = _report(tmp_path, "a.json", pilot_delta.RULES_COMMIT, "run-a", 1000)
    (tmp_path / "b.json").write_text("{ not json", encoding="utf-8")
    placed = pilot_delta.rounds([good, tmp_path / "b.json"], REPO_ROOT)
    assert [r.run_id for r in placed] == ["run-a"]


def test_rounds_reads_the_reports_mtime_not_today(tmp_path):
    """The audit log's rule for dating an entry, and the key the join below orders by:
    a sweep run a week late must not date every round to the day someone looked."""
    path = _report(tmp_path, "a.json", pilot_delta.RULES_COMMIT, "run-a", 1234567.0)
    (placed,) = pilot_delta.rounds([path], REPO_ROOT)
    assert placed.at == 1234567.0
    assert placed.mailbox == "inbox@example.test"


def test_a_conversation_drafted_twice_is_scored_on_its_latest_round(tmp_path):
    """One conversation is one row. Scoring every round would weight a thread the pilot
    kept re-drafting above one it answered first time, and the older body is not what
    the reviewer saw."""
    old = pilot_delta.rounds([_report(tmp_path, "old.json",
                                      pilot_delta.RULES_COMMIT, "old", 100)], REPO_ROOT)[0]
    new = pilot_delta.rounds([_report(tmp_path, "new.json",
                                      pilot_delta.RULES_COMMIT, "new", 900)], REPO_ROOT)[0]
    latest = pilot_delta.latest_drafting_round({"t1": [(old, "first"), (new, "second")]})
    assert latest["t1"][0].run_id == "new"
    assert latest["t1"][1] == "second"


def test_the_carrying_table_announces_a_thin_denominator():
    """Measured 2026-09-01: three conversations, against the issue's floor of ten. The
    table still prints — three is not nothing — but a thin denominator that does not
    announce itself reads as a whole one."""
    assert not pilot_delta.enough(3)
    assert pilot_delta.enough(10)
    assert pilot_delta.enough(12)


def test_the_write_time_is_the_one_of_the_round_that_drafted_the_scored_body(tmp_path):
    """The journal holds a write per run; pairing with an older run's write would pick a
    send that answered a body nobody is scoring. A conversation with no `ok` row has no
    write time and is left out, never guessed."""
    old = pilot_delta.rounds([_report(tmp_path, "old.json",
                                      pilot_delta.RULES_COMMIT, "old", 100)], REPO_ROOT)[0]
    new = pilot_delta.rounds([_report(tmp_path, "new.json",
                                      pilot_delta.RULES_COMMIT, "new", 900)], REPO_ROOT)[0]
    entries = [
        {"run_id": "old", "status": "ok", "thread_id": "t1", "timestamp": "2026-09-01T10:00:00Z"},
        {"run_id": "new", "status": "ok", "thread_id": "t1", "timestamp": "2026-09-02T10:00:00Z"},
    ]
    latest = {"t1": (new, "second"), "t2": (old, "body")}
    assert pilot_delta.written_at(latest, entries) == {"t1": "2026-09-02T10:00:00Z"}


def test_an_orphaned_draft_is_named_apart_and_the_window_keeps_reading(tmp_path, monkeypatch):
    """STU-2075: a `write-draft` naming a thread no `drafts-pre` item carries used to
    raise `OrphanDraft` and stop the whole window. It is counted apart, never scored."""
    from email_classifier import cost

    def complete(stage, output):
        return json.dumps({"event": "stage_complete", "stage": stage, "output": output})

    log = tmp_path / "run.jsonl"
    log.write_text("\n".join([
        complete("drafts-pre", {"pending": [{"thread_id": "t1"}]}),
        complete("write-draft", {"thread_id": "t1", "draft_body": "kept"}),
        complete("write-draft", {"thread_id": "tl", "draft_body": "orphan"}),
    ]), encoding="utf-8")
    monkeypatch.setattr(cost, "find_logs", lambda run_id, dirs: [log])
    rnd = pilot_delta.rounds([_report(tmp_path, "r.json",
                                      pilot_delta.RULES_COMMIT, "run", 100)], REPO_ROOT)[0]

    unmatched: set[str] = set()
    drafted = pilot_delta.drafted_by_round([rnd], [tmp_path], unmatched)

    assert drafted == {"t1": [(rnd, "kept")]}
    assert unmatched == {"tl"}


def test_the_facts_reader_runs_on_the_hosts_provider(monkeypatch):
    """STU-2088: the reader reads real mail, so it follows `STUDIO_PROVIDER`; the
    bake-off default `anthropic` has no key on the serving host."""
    from scripts import pilot_delta as cli

    monkeypatch.setenv("STUDIO_PROVIDER", "claude-code")
    assert cli.parse_args([]).provider == "claude-code"
    assert cli.parse_args(["--provider", "mock"]).provider == "mock"
    monkeypatch.delenv("STUDIO_PROVIDER")
    assert cli.parse_args([]).provider is None
