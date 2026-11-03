"""The draft-versus-sent reading across the whole pilot, and which commit wrote each
draft (STU-1182).

`audit_run.py reviewed` prices one run. The milestone's question spans the pilot, and
answering it on 2026-09-01 took an ad-hoc aggregator over twenty JSON dumps that was
never committed — so the number it produced is not reproducible by anyone else. That is
the first half of what this fixes.

The second half is the one that made the first number not answer its own title. Every
draft in that reading was written by `c67153d8` or earlier, and the voice rules landed
in `c6d7857`; reading the surviving semicolons as "the mechanics rule did not take"
would have been reading a rule that never ran. So a round is placed against the commit
that drafted it, before any figure is averaged, and a round that predates the rules is
reported as such rather than folded in — STU-912's direction, applied to a cohort
instead of a check.

Three placements and not two, because "cannot tell" is its own answer. A report naming
a commit this clone does not carry is not evidence either way, and counting it as
pre-rules would let a missing object quietly argue that the rules did nothing. Measured
2026-09-01: of the fifteen commits the serving host's reports name, one (`82f882a7`) is not in
this clone at all.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

# STU-1124 — scope, length and French mechanics added to `club-voice.skill.md`. A draft
# carries the rules exactly when the commit that wrote it descends from this one.
#
# The three sibling merges of that window (STU-1131, STU-1136, STU-1138) are not tested
# separately: they land inside the same interval and no report names a commit between
# them, so a second ancestry test would draw a line no round falls on.
RULES_COMMIT = "c6d7857"

CARRYING = "carrying"
PRE_RULES = "pre-rules"
UNKNOWN = "unknown"
COHORTS = (CARRYING, PRE_RULES, UNKNOWN)


@dataclass(frozen=True)
class Round:
    """One production report: the run it describes and the commit that served it."""

    path: Path
    run_id: str
    commit: str | None
    cohort: str
    # The report's mtime, never today: a sweep run a week late would otherwise date
    # every round to the day someone looked, and the join below is ordered by it.
    at: float
    mailbox: str | None
    # Two more provenance facts, here so a caller reads the report once. `input_path` is
    # what separates an unattended push round from someone at a keyboard (STU-859), and
    # `anonymized_fields` is what says whether a run claimed a scope at all — without it a
    # recall sweep cannot tell a clean run from one that sent the mail in clear by policy.
    input_path: str | None = None
    anonymized_fields: tuple[str, ...] = ()


def cohort_of(commit: str | None, repo_root: Path, rules_commit: str = RULES_COMMIT) -> str:
    """Which side of the voice rules a commit falls on, or that we cannot say.

    `git merge-base --is-ancestor` and not a date: the serving host moved forward and
    backward across this window, so wall-clock order does not answer ancestry, and a
    report's mtime is when the round ran rather than when its code was written.
    """
    if not commit:
        return UNKNOWN
    if not _known(commit, repo_root) or not _known(rules_commit, repo_root):
        return UNKNOWN
    done = subprocess.run(
        ["git", "merge-base", "--is-ancestor", rules_commit, commit],
        cwd=repo_root, capture_output=True,
    )
    if done.returncode == 0:
        return CARRYING
    if done.returncode == 1:
        return PRE_RULES
    # Any other code is git failing rather than answering, and an unanswered ancestry
    # question is exactly the one this third bucket exists for.
    return UNKNOWN


def _known(commit: str, repo_root: Path) -> bool:
    return subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=repo_root, capture_output=True,
    ).returncode == 0


def rounds(report_paths: list[Path], repo_root: Path) -> list[Round]:
    """Every readable report, placed. An unreadable one is skipped, not guessed at."""
    out = []
    for path in sorted(report_paths):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        provenance = report.get("provenance") or {}
        commit = provenance.get("git_commit")
        out.append(Round(
            path=path,
            run_id=report.get("run_id") or "",
            commit=commit,
            cohort=cohort_of(commit, repo_root),
            at=path.stat().st_mtime,
            mailbox=provenance.get("mailbox"),
            input_path=provenance.get("input_path"),
            anonymized_fields=tuple(provenance.get("anonymized_fields") or ()),
        ))
    return out


def latest_drafting_round(drafted: dict[str, list[tuple[Round, str]]]) -> dict[str, tuple[Round, str]]:
    """One row per conversation: the most recent round that drafted it, and that body.

    A conversation drafted in several rounds is one conversation, not several. Taking
    every round would score the same thread once per attempt and weight a thread the
    pilot kept re-drafting above one it answered first time — and the older body is not
    what the reviewer saw, so it has no sent reply to be compared against.

    Most recent by the round's own mtime, which is the rule the audit log already
    follows for dating an entry.
    """
    return {
        thread_id: max(entries, key=lambda pair: pair[0].at)
        for thread_id, entries in drafted.items()
        if entries
    }


def coverage(rounds_scored: list[Round]) -> dict:
    """How many of the drafts scored were written by a commit carrying the rules.

    Named on both tables rather than inferred from which table a row is in: a reader
    who sees only the carrying figures cannot tell whether they rest on three rounds or
    thirty, and on 2026-09-01 the answer was three conversations.
    """
    counts = {name: sum(1 for r in rounds_scored if r.cohort == name) for name in COHORTS}
    commits = {
        name: sorted({(r.commit or "")[:12] for r in rounds_scored if r.cohort == name})
        for name in COHORTS
    }
    return {
        "rules_commit": RULES_COMMIT,
        "rounds": counts,
        "commits": commits,
        # The question the issue asks in one number, so it cannot be read off the wrong
        # row: rounds whose drafts carry the rules, over rounds we could place at all.
        "placeable": counts[CARRYING] + counts[PRE_RULES],
    }


def enough(carrying_conversations: int, floor: int = 10) -> bool:
    """Whether the carrying table is worth reading yet.

    The issue sets ~10 conversations. Under it the table is still printed — a figure
    measured on three conversations is not nothing — but it is printed saying so, for
    the reason `_lengths` returns null rather than 0: a thin denominator that does not
    announce itself reads as a whole one.
    """
    return carrying_conversations >= floor


def drafted_by_round(rounds_placed: list[Round], runs_dirs) -> dict[str, list[tuple[Round, str]]]:
    """Every conversation each round drafted, and the body it left on it.

    One statement of this and not two: `scripts/pilot_delta.py` scores these bodies and
    `pilot_gate` counts them for the milestone's denominator, and two readings of "what
    did this round draft" would put two numbers behind one claim.

    The body kept per round is the highest attempt's — the group rewrites a draft between
    passes, so an earlier attempt is a body the reviewer never saw. Collapsing across
    rounds is `latest_drafting_round`'s job, deliberately left separate: this answers what
    a round did, that answers what a conversation got.
    """
    from email_classifier.cost import find_logs
    from email_classifier.qa_bakeoff import drafts_from_journal

    drafted: dict[str, list[tuple[Round, str]]] = {}
    for rnd in rounds_placed:
        for log in find_logs(rnd.run_id, runs_dirs) or []:
            try:
                lines = Path(log).read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            best: dict[str, tuple[int, str]] = {}
            for draft in drafts_from_journal(lines):
                body = draft.draft.get("draft_body") or ""
                if draft.thread_id not in best or draft.attempt >= best[draft.thread_id][0]:
                    best[draft.thread_id] = (draft.attempt, body)
            for thread_id, (_, body) in best.items():
                drafted.setdefault(thread_id, []).append((rnd, body))
    return drafted


def written_at(latest: dict[str, tuple[Round, str]], entries: list[dict]) -> dict[str, str]:
    """When each conversation's scored draft was written, from the drafts journal.

    Keyed off the round `latest_drafting_round` kept, so the write time is the one of
    the body being scored. A conversation the journal has no `ok` row for is absent:
    no write time means no pairing (STU-1998).
    """
    from email_classifier import draft_pairing

    by_run = {rnd.run_id: draft_pairing.write_times(entries, rnd.run_id)
              for rnd, _ in latest.values()}
    return {t: by_run[rnd.run_id][t] for t, (rnd, _) in latest.items()
            if t in by_run[rnd.run_id]}
