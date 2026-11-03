"""How long an unreviewed audit entry has before its evidence goes (STU-1168).

STU-1084 made `retention.audit_protection` cover only a *reviewed* entry, and named
the price: a run the push path filed for itself, that nobody reads inside the reports
shelf's window, loses the report, the anonymization record and the apply journal a
reviewer would have re-derived from — and the entry is then an unfalsifiable row of
counters. The price is accepted. What was missing is any line saying it is about to
be paid: `coverage` prints how much is owed, never how long is left to pay it.

Announced from `scripts/check_webhook_health.py` rather than from `coverage`, which
nobody runs on a schedule. That check is the one job on the serving host whose purpose
is that a silence gets broken, and it already owns the "something expires soon" family
under a code per condition (STU-1095, STU-1126). Saying it in both places would put
the deadline in two files.

Two rules the module keeps, both borrowed from `webhook/credential.py`:

* **No reports on disk, no question.** A laptop or a worktree carries the committed
  log and none of the artifacts it points at, and every one of its entries would read
  as evidence already lost. Only a tree that holds `reports/` can answer.
* **A window, not a per-entry alarm.** The 34 entries measured on the serving host on
  2026-09-01 expire across 34 consecutive days; alerting per entry would be 34
  notifications for one condition, and the dedup key is the exit code alone.

What the window does *not* cover is an entry whose report is already gone. Nothing
repairs that — the artifacts are not coming back — and a check that fires forever on
it is one nobody reads the day it means something (STU-1145). It is counted and named
on the same line instead, so the loss is still said once rather than dropped.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from email_classifier import deployment
from email_classifier.audit import unreviewed_runs
from email_classifier.retention import DAY_SECONDS, REPORTS, SHELVES, run_id_of

WARN_WITHIN_DAYS = 7

REPO_ROOT = deployment.ROOT

_SHELF = next(shelf for shelf in SHELVES if shelf.path == REPORTS)


@dataclasses.dataclass(frozen=True)
class Expiry:
    """What the unreviewed entries have left, measured against the reports shelf.

    `nearest_days` is None exactly when no unreviewed entry still has a report on
    disk, which is not the same as nothing being owed: `lost` is how many entries
    are past that already, and `total` is how many the log carries at all.
    """

    total: int
    nearest_days: int | None = None
    nearest_run_id: str | None = None
    within_window: int = 0
    lost: int = 0

    @property
    def soon(self) -> bool:
        return self.within_window > 0

    def summary(self) -> str:
        if not self.total:
            return "no unreviewed audit entries"
        entries = f"entr{'y' if self.total == 1 else 'ies'}"
        if self.nearest_days is None:
            parts = [f"{self.total} unreviewed audit {entries}, none with a report "
                     "still on disk"]
        else:
            scope = (f"{self.within_window} of {self.total} inside the window"
                     if self.within_window else f"{self.total} filed")
            parts = [f"{scope}, nearest {self.nearest_run_id} {self._when()}"]
            parts[0] = f"unreviewed audit {entries}: {parts[0]}"
        if self.lost:
            parts.append(f"{self.lost} already unauditable, their reports gone")
        return "; ".join(parts)

    def _when(self) -> str:
        """A report past the shelf is still on disk until a prune runs, so it is the
        most urgent state rather than a lost one — and it cannot say "in -2 days"."""
        if self.nearest_days < 0:
            return "past the shelf, going on the next prune"
        if self.nearest_days == 0:
            return "losing its artifacts today"
        plural = "day" if self.nearest_days == 1 else "days"
        return f"losing its artifacts in {self.nearest_days} {plural}"


def measure(root: Path | None = None, *, now: float | None = None,
            entries: list[dict] | None = None) -> Expiry | None:
    """What the log's unreviewed entries have left, or None when nothing can answer.

    None rather than an empty `Expiry`: a tree without `reports/` is not a tree where
    nothing expires, it is one that cannot see whether anything does — the distinction
    `webhook/credential.py` draws between no secret and an unreadable expiry date.

    The shelf's newest `keep_last` reports are exempt, because they are: the floor
    under the age outlives any window, so a host holding three reports would otherwise
    count down forever against a deadline that never arrives.
    """
    root = REPO_ROOT if root is None else root
    directory = root / REPORTS
    if not directory.is_dir():
        return None
    if entries is None:
        entries = _log_entries(root)
        if entries is None:
            return None

    unreviewed = set(unreviewed_runs(entries))
    if not unreviewed:
        return Expiry(total=0)

    now = time.time() if now is None else now
    ages, exempt = {}, set()
    for path in sorted(directory.glob("*.json")):
        run_id = run_id_of(path.name)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if run_id:
            ages[run_id] = mtime
    for run_id, _ in sorted(ages.items(), key=lambda item: -item[1])[:_SHELF.keep_last]:
        exempt.add(run_id)

    left = sorted((int(_SHELF.max_age_days - (now - mtime) / DAY_SECONDS), run_id)
                  for run_id, mtime in ages.items()
                  if run_id in unreviewed and run_id not in exempt)
    lost = len(unreviewed) - len(left) - len(unreviewed & exempt)
    if not left:
        return Expiry(total=len(unreviewed), lost=lost)
    return Expiry(
        total=len(unreviewed),
        nearest_days=left[0][0],
        nearest_run_id=left[0][1],
        within_window=sum(1 for days, _ in left if days <= WARN_WITHIN_DAYS),
        lost=lost,
    )


def _log_entries(root: Path) -> list[dict] | None:
    """The audit log, or None when it cannot be read.

    Unreadable is not "nothing is owed" — the same direction `_streak` takes in the
    health check, where a measurement that could not be made is never invented.
    """
    try:
        entries = json.loads((root / "audits" / "audit_log.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return entries if isinstance(entries, list) else None
