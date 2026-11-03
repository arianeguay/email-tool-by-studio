"""Retention for the artifacts a run leaves behind (STU-932).

`reports/`, `dumps/`, `logs/applied/`, `logs/anonymization/`, `dumps/web-cache/` and
`.studio/runs/` each
grow once per run and nothing has ever trimmed them. On a laptop driven by the
occasional `make run` that is invisible; on a host classifying every inbound message
it fills a disk.

What may go is decided per directory by an age. What may *not* go is decided by
`audits/audit_log.json`: the audit trail is worth considerably more than the raw
dumps sitting under it, and an entry is a claim about files that have to still be
there for the claim to be answerable. Four protections, each covering a different
thing a prune would destroy:

- **audited** — the log names the run *and* a person wrote its verdict, so its report
  and its journal are the evidence behind a claim anyone can still be asked to
  re-derive. An entry a run filed for itself (STU-1084) makes no claim and protects
  nothing — see `audit_protection` for what counting it would freeze;
- **audit-input** — an entry names its `dump` by path, and `check` cross-checks the
  report against that file (STU-859). Protected by path, not by run id: the dump a
  run consumed is often not named after it;
- **unaudited** — a report the log does not name and `audits/skipped.json` does not
  excuse is exactly what `audit-gate` is holding the next run on (STU-900). Pruning
  it would release the gate, which turns this command into the way to launder a run
  nobody looked at; the dump goes with it, because an audit that cannot cross-check
  its input is not the audit the gate is waiting for;
- **undoable** — `undo` reads both legs: the apply journal inverts the mailbox
  operations, and the report names the drafts to delete (STU-854). A journal that
  outlives its report gives half an undo and leaves the drafts in the mailbox, so a
  journal still on disk holds its report back whatever the two ages say.

A run id is read off the filename rather than out of the report, and the two can in
principle disagree. The direction is safe: a report whose name is not the run id the
audit log knows reads as uncovered, which protects it.

Everything here decides; nothing here deletes until `apply_plan` is called, which is
what lets the tests plan over a synthetic tree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from email_classifier.audit import is_unreviewed, unaudited_runs

DAY_SECONDS = 86400

REPORTS = "reports"
APPLIED = "logs/applied"

# A run id as every writer spells it: the uuid leading the filename. `<run-id>.json`,
# `<run-id>.jsonl`, `<run-id>.plan.jsonl` and `<run-id>.undo.jsonl` all match;
# `gmail-dump.json`, `keymap.json` and a web-cache sha256 do not. Lowercase hex only,
# and a name this does not recognize is never a candidate — the failure direction of
# an unparseable filename has to be "kept forever", not "deleted".
_RUN_ID = re.compile(r"^([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b")


@dataclass(frozen=True)
class Shelf:
    """One directory's retention rule.

    `run_scoped` says the filenames name runs, and only the files that do are
    candidates. `dumps/web-cache/` is the one shelf where they do not: its entries are
    the sha256 of a URL, nothing outside the cache refers to one, and `websearch.py`
    refuses to serve one past `CACHE_TTL_SECONDS` anyway — a day-old entry is already
    dead weight.

    `keep_last` is a floor under the age: the newest few artifacts survive a quiet
    month. Without it a host that has not run in longer than the max age wakes up to
    an empty `reports/`, and `RUN_ID` — what every apply, undo and audit target
    defaults to — resolves to nothing.

    `glob` narrows a shelf to the files it is actually about. `.studio/runs/` is the
    one that needs it: Studio keeps `runs.db` in there beside the logs, and a shelf
    that swept the whole directory would take the index with the entries.
    """

    path: str
    max_age_days: int
    keep_last: int = 0
    run_scoped: bool = True
    glob: str = "*"


SHELVES: tuple[Shelf, ...] = (
    # A skipped run's report is the only report this age ever reaches: an audited one
    # is held by the log, an unaudited one by the gate.
    Shelf(REPORTS, max_age_days=30, keep_last=3),
    # The bulk on disk, and the artifact the audit trail is worth more than. A
    # fortnight is longer than anything but an audit asks of raw mail.
    Shelf("dumps", max_age_days=14, keep_last=3),
    # The undo window. No shorter than the reports, so the journal is never the leg
    # that outlives the run.
    Shelf(APPLIED, max_age_days=30, keep_last=3),
    Shelf("dumps/web-cache", max_age_days=1, run_scoped=False),
    # The recall evidence: what the run sent stage 2, and the entries it minted saying
    # so (STU-1036, STU-924). Both are claims about one report and are unreadable once
    # it is gone, so they carry the reports' age and the two protections that name a
    # run — an unaudited run keeps its evidence for exactly as long as `audit-gate`
    # keeps asking for it. `undoable` does not reach here, and does not need to: `undo`
    # reads the journal and the report, never this. `keymap.json` is the shared seed and
    # names no run, so `run_scoped` leaves it alone, which is what it needs: it is the
    # accumulated state of the machine.
    Shelf("logs/anonymization", max_age_days=30, keep_last=3),
    # Studio's own run logs, prunable since a run records its own cost (STU-1012).
    # They were load-bearing for the audit trail while they were the only place token
    # counts existed, and nothing managed them — 642 files back to 2026-06-17. Their
    # filenames name Studio's run id, not ours, so no protection can reach them and
    # age is the whole rule. Unlike the shelves above, this one really does drop
    # evidence an audited report still points at: a pre-STU-1012 report kept forever
    # loses the only thing that could price it, and `cost` exits 2 on it from then on.
    # 30 days is the bound on that, and `audit-gate` is why it holds — a run is
    # audited before the next one starts, and the audit writes the cost into the PR
    # body. What expires here is the source, long after the number was read off it.
    Shelf(".studio/runs", max_age_days=30, run_scoped=False, glob="*.jsonl"),
)


@dataclass(frozen=True)
class Decision:
    """One file, and why it is going or staying."""

    path: Path
    run_id: str | None
    size: int
    remove: bool
    reason: str


def run_id_of(name: str) -> str | None:
    """The run a filename names, or None when it names none."""
    match = _RUN_ID.match(name)
    return match.group(1) if match else None


def audit_protection(entries: list[dict], root: Path) -> tuple[set[str], set[Path]]:
    """The run ids and the input paths `audits/audit_log.json` still points at.

    An entry a run filed for itself and nobody has read protects nothing (STU-1084).
    The shelf exists so the evidence behind a *claim* stays re-derivable, and a filed
    entry makes no claim — it carries the numbers and says so in `verdict`. Counting
    it would freeze every shelf on the serving host the day filing landed: one entry
    per notification batch, each one permanently exempting its report, its dump and
    its apply journal from every age, so the directory the walk reads grows and can
    never shrink. That is STU-932 undone by the thing meant to keep it honest.

    The price is named rather than dodged: a filed run nobody reads within the report
    shelf's 30 days loses the artifacts a reviewer would have re-derived from, and the
    entry is then all that survives. That is the same 30 days a reviewer already had,
    and an entry older than it is one nobody was going to read.
    """
    run_ids = {rid for e in entries
               if (rid := e.get("run_id")) and not is_unreviewed(e)}
    # `dump` is free text about as often as it is a path — "unknown (...)" for a
    # pre-STU-819 run, `webhook:push-notification` for a live one (STU-859). Resolving
    # all of them is cheaper than telling them apart, and a sentence resolves to a
    # path no artifact on disk will ever equal.
    paths = {(root / str(e["dump"])).resolve() for e in entries if e.get("dump")}
    return run_ids, paths


def uncovered_run_ids(root: Path, audit_entries: list[dict],
                      skipped_entries: list[dict]) -> set[str]:
    """Reports on disk that neither the audit log nor `skipped.json` accounts for.

    The same rule `audit-gate` runs on, asked of the filenames rather than of the
    reports' contents — so what the gate is waiting for is what this refuses to prune.
    """
    directory = root / REPORTS
    if not directory.is_dir():
        return set()
    ids = [rid for path in sorted(directory.glob("*.json"))
           if (rid := run_id_of(path.name))]
    return set(unaudited_runs(ids, audit_entries, skipped_entries))


def _stat_files(directory: Path) -> list[tuple[Path, float, int]]:
    """Every file in a directory, newest first. Subdirectories are another shelf."""
    out = []
    for path in directory.iterdir():
        try:
            st = path.stat()
        except OSError:      # vanished under us; a prune has nothing to do about it
            continue
        if path.is_file():
            out.append((path, st.st_mtime, st.st_size))
    return sorted(out, key=lambda item: (-item[1], item[0].name))


def _decide(root: Path, shelf: Shelf, *, now: float, protections: dict[str, str],
            protected_paths: set[Path]) -> list[Decision]:
    directory = root / shelf.path
    if not directory.is_dir():
        return []

    decisions: list[Decision] = []
    candidates: list[tuple[Path, float, int, str | None]] = []
    for path, mtime, size in _stat_files(directory):
        run_id = run_id_of(path.name)
        if not fnmatch(path.name, shelf.glob) or (shelf.run_scoped and run_id is None):
            decisions.append(Decision(path, None, size, False, "unrecognized"))
            continue
        candidates.append((path, mtime, size, run_id))

    cutoff = now - shelf.max_age_days * DAY_SECONDS
    for rank, (path, mtime, size, run_id) in enumerate(candidates):
        if run_id is not None and run_id in protections:
            reason, remove = protections[run_id], False
        elif path.resolve() in protected_paths:
            reason, remove = "audit-input", False
        elif rank < shelf.keep_last:
            reason, remove = "keep-last", False
        elif mtime >= cutoff:
            reason, remove = "fresh", False
        else:
            reason, remove = "expired", True
        decisions.append(Decision(path, run_id, size, remove, reason))
    return decisions


def plan(root: Path, *, now: float, audit_entries: list[dict],
         skipped_entries: list[dict] = (), shelves: tuple[Shelf, ...] = SHELVES,
         ) -> list[Decision]:
    """What a prune of `root` would remove and what it would keep, and why.

    `logs/applied/` is decided first because the journals that survive it are what
    protect their reports. Uncoupled, a report age shorter than the journal's takes the
    report and leaves the journal, and the undo that pair still offers is half of one.
    """
    audited, protected_paths = audit_protection(list(audit_entries), root)
    base = {rid: "unaudited"
            for rid in uncovered_run_ids(root, list(audit_entries), list(skipped_entries))}
    base.update({rid: "audited" for rid in audited})

    by_shelf: dict[str, list[Decision]] = {}
    undoable: dict[str, str] = {}
    for shelf in shelves:
        if shelf.path != APPLIED:
            continue
        by_shelf[shelf.path] = _decide(root, shelf, now=now, protections=base,
                                       protected_paths=protected_paths)
        undoable = {d.run_id: "undoable" for d in by_shelf[shelf.path]
                    if d.run_id and not d.remove}

    for shelf in shelves:
        if shelf.path in by_shelf:
            continue
        # `undoable` is the reports shelf's alone: a dump is not what `undo` reads,
        # and letting a journal hold one back would retain the bulk on the journal's
        # age instead of its own.
        protections = {**undoable, **base} if shelf.path == REPORTS else base
        by_shelf[shelf.path] = _decide(root, shelf, now=now, protections=protections,
                                       protected_paths=protected_paths)

    return [d for shelf in shelves for d in by_shelf[shelf.path]]


def apply_plan(decisions: list[Decision]) -> tuple[int, int]:
    """Unlink what the plan marked, and report how many files and bytes went.

    A file already gone is not an error: two cron ticks overlapping, or a `make clean`
    in between, reach the same end state this pass was asking for.
    """
    removed = freed = 0
    for decision in decisions:
        if not decision.remove:
            continue
        try:
            decision.path.unlink()
        except FileNotFoundError:
            continue
        removed += 1
        freed += decision.size
    return removed, freed
