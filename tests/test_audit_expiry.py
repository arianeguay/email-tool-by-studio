"""What an unreviewed audit entry has left before its evidence goes (STU-1168)."""

import json
import os
from pathlib import Path

from email_classifier import audit_expiry

NOW = 1_000_000_000.0


def _tree(root: Path, entries: list[tuple[float, str]]) -> Path:
    """A repo root with one report per entry, aged, and a log filing each verdict."""
    (root / "reports").mkdir(parents=True, exist_ok=True)
    (root / "audits").mkdir(parents=True, exist_ok=True)
    log = []
    for i, (age_days, verdict) in enumerate(entries):
        run_id = f"{i:08x}-0000-0000-0000-000000000000"
        report = root / "reports" / f"{run_id}.json"
        report.write_text("{}", encoding="utf-8")
        os.utime(report, (NOW - age_days * 86400,) * 2)
        log.append({"run_id": run_id, "verdict": verdict})
    (root / "audits" / "audit_log.json").write_text(json.dumps(log), encoding="utf-8")
    return root


UNREVIEWED = "UNREVIEWED (machine-filed by the push path)"
READ = "OK — read by hand, nothing to flag"


def test_a_tree_without_reports_cannot_answer(tmp_path):
    """A laptop carries the committed log and none of the artifacts it names, so every
    entry would read as evidence already lost. None is the absence of the question."""
    (tmp_path / "audits").mkdir()
    (tmp_path / "audits" / "audit_log.json").write_text("[]", encoding="utf-8")

    assert audit_expiry.measure(tmp_path, now=NOW) is None


def test_an_unreadable_log_is_not_a_clean_bill(tmp_path):
    (tmp_path / "reports").mkdir()

    assert audit_expiry.measure(tmp_path, now=NOW) is None


def test_a_reviewed_entry_owes_nothing(tmp_path):
    """`audit_protection` already holds its artifacts open, whatever their age."""
    root = _tree(tmp_path, [(400, READ), (399, READ), (398, READ), (397, READ)])

    measured = audit_expiry.measure(root, now=NOW)
    assert measured.total == 0
    assert not measured.soon
    assert measured.summary() == "no unreviewed audit entries"


def test_the_nearest_entry_is_the_one_named(tmp_path):
    root = _tree(tmp_path, [(26, UNREVIEWED), (25, UNREVIEWED), (3, UNREVIEWED),
                            (2, UNREVIEWED), (1, UNREVIEWED), (1, UNREVIEWED)])

    measured = audit_expiry.measure(root, now=NOW)
    assert (measured.nearest_days, measured.within_window, measured.total) == (4, 2, 6)
    assert measured.soon
    assert measured.nearest_run_id.startswith("00000000")


def test_runway_is_reported_without_alerting(tmp_path):
    """The serving host, 2026-09-01: 34 filed, oldest 3 days old, 27 days left — the reading
    that made this the mechanism rather than the backlog."""
    root = _tree(tmp_path, [(3, UNREVIEWED)] + [(2, UNREVIEWED)] * 3)

    measured = audit_expiry.measure(root, now=NOW)
    assert measured.nearest_days == 27
    assert not measured.soon
    assert "nearest" in measured.summary()


def test_a_report_past_the_shelf_reads_as_urgent_not_as_negative_days(tmp_path):
    """Still on disk until a prune runs, so reviewing it now still works."""
    root = _tree(tmp_path, [(33, UNREVIEWED)] + [(1, UNREVIEWED)] * 3)

    measured = audit_expiry.measure(root, now=NOW)
    assert measured.nearest_days == -3
    assert measured.soon
    assert "past the shelf, going on the next prune" in measured.summary()


def test_an_entry_whose_report_is_gone_is_counted_but_does_not_alert(tmp_path):
    """Nothing repairs it, and a check that fires forever on it is one nobody reads
    the day it means something (STU-1145). Said once instead, on the same line."""
    root = _tree(tmp_path, [(2, UNREVIEWED)] * 4)
    (root / "reports" / "00000000-0000-0000-0000-000000000000.json").unlink()

    measured = audit_expiry.measure(root, now=NOW)
    assert (measured.total, measured.lost) == (4, 1)
    assert not measured.soon
    assert "1 already unauditable, their reports gone" in measured.summary()


def test_the_shelf_floor_exempts_the_newest_reports(tmp_path):
    """`keep_last` outlives any age, so a host holding three reports has no deadline —
    and a countdown against one would never clear."""
    root = _tree(tmp_path, [(400, UNREVIEWED), (399, UNREVIEWED), (398, UNREVIEWED)])

    measured = audit_expiry.measure(root, now=NOW)
    assert (measured.total, measured.nearest_days, measured.lost) == (3, None, 0)
    assert not measured.soon
