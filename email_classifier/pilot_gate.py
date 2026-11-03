"""Reading the V1 exit condition off artifacts instead of off a feeling (STU-1242).

The prod-pilot milestone states what closes V1: *a week of live
rounds running unattended, no PII leak, no message moved that shouldn't have been, and a
drafts-accepted ratio the audit records.* Nothing owned checking it. A milestone
description is prose in a milestone, which is the same thing as a paragraph in a skill
file, and this repo has watched that shape fail twice — STU-1125 recorded a question
under "Unanswered questions" and closed, STU-1121 closed pointing at that paragraph as
though it were ownership, and the question sat unowned until STU-1155 was filed to own
the asking.

The three conditions do not come off one artifact, which is why this is a module and not
a subcommand of `audit_run.py`: that family prices one run, and every question here spans
a window of them.

Three verdicts and not two, the rule the audit family already follows (STU-912): a
condition nobody could measure must not read as one that came back clean. `NOT_MEASURABLE`
is the answer whenever the artifact that would settle a condition is absent, thin, or was
never consulted — and on the drafting ratio it is the expected answer, not a defect.
STU-1182 measured three conversations drafted by a commit carrying the voice rules, and
three drafts do not give a ratio a denominator.

Nothing here is a pass/fail gate on the pilot's *quality*. A condition the window fails is
a result: it is what decides whether the window extends or the criterion changes, and that
decision belongs to a human looking at a number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from email_classifier.orchestrate.provenance import LIVE_INPUT

MET = "met"
NOT_MET = "not met"
NOT_MEASURABLE = "not measurable"

# A week, because that is the word the milestone uses. Counted in distinct UTC days that
# carry a live round rather than in elapsed hours: a host that ran on day 1 and day 8 and
# nothing between spans a week of clock and no week of operation.
WEEK_DAYS = 7

# The hourly probe's codes. 0 is the only one criterion 5 admits, and the reason is not
# uniform across the rest: 3 says the serving commit is not the one `main` holds, so a
# report in the window cannot be read against the repo at all, while 8, 9 and 10 exist
# precisely so a silent window cannot pass as a quiet one.
HEALTHY_EXIT = 0
CRON = "cron"


@dataclass(frozen=True)
class Window:
    """The stretch being read, as epoch seconds. Half-open: `since <= t < until`."""

    since: float
    until: float

    def holds(self, at: float) -> bool:
        return self.since <= at < self.until

    def describe(self) -> dict:
        return {"since": _iso(self.since), "until": _iso(self.until),
                "days": round((self.until - self.since) / 86400, 2)}


@dataclass
class Condition:
    """One of the milestone's three, with the artifact it was read from.

    `artifacts` is not decoration. A verdict whose source cannot be named is the prose
    this issue exists to replace, and `NOT_MEASURABLE` in particular is only actionable
    when it says which file was missing.
    """

    name: str
    verdict: str
    summary: str
    artifacts: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def as_record(self) -> dict:
        return {"condition": self.name, "verdict": self.verdict, "summary": self.summary,
                "artifacts": self.artifacts, "detail": self.detail}


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).isoformat()


def _day(at: float) -> str:
    return datetime.fromtimestamp(at, tz=timezone.utc).date().isoformat()


def health_entries(journal: Path) -> list[dict] | None:
    """The probe's runs, oldest first, or None when the journal cannot be read.

    None and not an empty list, and the distance between them is the whole point: an
    empty journal is a host that never ran the probe, an unreadable one is a reading
    nobody took. Both leave criterion 5 unsettled, but only the first is a fact about
    the pilot.
    """
    try:
        lines = journal.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _at_of(entry: dict) -> float | None:
    try:
        return datetime.fromisoformat(entry["at"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def unattended_week(live_rounds: list, entries: list[dict] | None, window: Window,
                    journal_path: Path, report_dirs: list[str]) -> Condition:
    """A week of live rounds, and an hourly probe that saw nothing wrong while they ran.

    Two artifacts because the condition has two halves and neither implies the other.
    `reports/` says rounds happened; it cannot say the host was reachable in the hours
    between them, and a subscription that lapses produces no report and no error. The
    probe's journal says the pipe held; it says nothing about whether any mail arrived.

    Only `source == "cron"` lines count, which is criterion 5's own filter (STU-1082):
    the post-restart check inside `make deploy-webhook` and anyone typing
    `make check-webhook` write the same shape, so the unfiltered file answers a question
    about hourly runs with every run it has. A line with no `source` predates that flag
    and belongs to nobody — it is evidence in neither direction.

    A window with no cron line in it is not a quiet window, it is an unmeasured one, and
    that is criterion 5's rule stated in the doc: *a window whose hours are not covered
    by cron lines is a window that cannot settle it.*
    """
    artifacts = report_dirs + [str(journal_path)]
    days = sorted({_day(r.at) for r in live_rounds})
    detail = {
        "live_rounds": len(live_rounds),
        "days_with_a_live_round": len(days),
        "days": days,
        "week_days": WEEK_DAYS,
    }

    if entries is None:
        detail["cron_runs"] = None
        return Condition(
            "a week of live rounds running unattended", NOT_MEASURABLE,
            f"{len(live_rounds)} live round(s) over {len(days)} day(s), but "
            f"{journal_path} could not be read — the hourly probe's verdicts are what "
            "say the pipe held between rounds, and no reading of them was taken.",
            artifacts, detail)

    cron = [e for e in entries if e.get("source") == CRON and
            (at := _at_of(e)) is not None and window.holds(at)]
    exits: dict[str, int] = {}
    for entry in cron:
        exits[str(entry.get("exit"))] = exits.get(str(entry.get("exit")), 0) + 1
    detail["cron_runs"] = len(cron)
    detail["cron_exits"] = exits
    detail["unattributed_lines"] = sum(
        1 for e in entries
        if e.get("source") is None and (at := _at_of(e)) is not None and window.holds(at))

    if not cron:
        return Condition(
            "a week of live rounds running unattended", NOT_MEASURABLE,
            f"{len(live_rounds)} live round(s) over {len(days)} day(s), and no "
            f"`source: cron` line in {journal_path} falls inside the window. A window "
            "whose hours are not covered by hourly runs cannot settle whether it was "
            "unattended — quiet and unwatched are the same silence.",
            artifacts, detail)

    faults = {code: n for code, n in exits.items() if code != str(HEALTHY_EXIT)}
    if faults:
        worst = ", ".join(f"exit {c} on {n} run(s)" for c, n in sorted(faults.items()))
        return Condition(
            "a week of live rounds running unattended", NOT_MET,
            f"{len(days)} day(s) carried a live round, but the hourly probe reported "
            f"{worst} over {len(cron)} cron run(s). The window ran; it did not run "
            "unattended-and-well.",
            artifacts, detail)

    if len(days) < WEEK_DAYS:
        return Condition(
            "a week of live rounds running unattended", NOT_MET,
            f"{len(days)} day(s) carried a live round, short of {WEEK_DAYS}. "
            f"{len(cron)} cron run(s) all exited {HEALTHY_EXIT}, so the pipe held — the "
            "window is simply not a week long yet.",
            artifacts, detail)

    return Condition(
        "a week of live rounds running unattended", MET,
        f"{len(live_rounds)} live round(s) across {len(days)} day(s), and all "
        f"{len(cron)} hourly cron run(s) exited {HEALTHY_EXIT}.",
        artifacts, detail)


# The open defects in front of the recall reading, all of which over-report. Named rather
# than compensated for: subtracting known noise here is how a reader learns to wave the
# list through, which is how a real leak gets missed.
#
# STU-1227 left this list when it shipped — measured on this laptop's five settled runs,
# the whole-word rule took the sweep from 18 surviving values to 6, and all 6 are the
# entry below. A name here is a claim that the reading over-reports today, so it is
# removed the day the defect is, not left as history.
RECALL_NOISE = ("STU-1228 (non-person keymap entries left on the host)",)


def no_pii_leak(readings: list[dict], artifacts: list[str]) -> Condition:
    """What survived tokenization in what the window's runs actually sent stage 2.

    One reading per run, each already produced by `audit.recall` over that run's
    `logs/anonymization/<run-id>-sent.jsonl` — the payload as assembled, not the
    tokenizer's output, because a field outside the tokenizer's scope is exactly where a
    leak sits (STU-1036).

    Three populations, and collapsing any two of them is how this condition lies. A run
    that recorded no `anonymized_fields` sent the mail in clear *by policy* and has no
    gate to fail; a run with a scope but no surviving sent log cannot be settled in
    either direction; only the rest carry a verdict. So a window whose runs are all of
    the first two kinds is `NOT_MEASURABLE`, never clean.

    Unsettled runs downgrade a clean sweep rather than being ignored. "No PII leak over
    the window" is a claim about the window, and a sweep that skipped part of it has not
    made that claim — it has made a smaller one.
    """
    settled = [r for r in readings if r["state"] == "settled"]
    unsettled = [r for r in readings if r["state"] == "unsettled"]
    no_scope = [r for r in readings if r["state"] == "no scope"]
    leaking = [r for r in settled if r["leaks"]]
    detail = {
        "runs_settled": len(settled),
        "runs_unsettled": len(unsettled),
        "runs_without_scope": len(no_scope),
        "unsettled_run_ids": [r["run_id"] for r in unsettled],
        "without_scope_run_ids": [r["run_id"] for r in no_scope],
        "readings": readings,
        "known_over_reporting": list(RECALL_NOISE),
    }

    if leaking:
        total = sum(r["leaks"] for r in leaking)
        return Condition(
            "no PII leak", NOT_MET,
            f"{total} surviving value(s) across {len(leaking)} of {len(settled)} settled "
            f"run(s). Every one needs reading before it is dismissed: two open defects "
            f"over-report here — {', '.join(RECALL_NOISE)} — and neither is a leak, but a "
            "reader who learns to wave the list through is how a real one gets missed.",
            artifacts, detail)

    if not settled:
        return Condition(
            "no PII leak", NOT_MEASURABLE,
            f"No run in the window could be settled: {len(unsettled)} kept no record of "
            f"what it sent stage 2, {len(no_scope)} sent the mail in clear by policy and "
            "so has no gate to fail. Nothing here says the window was clean.",
            artifacts, detail)

    if unsettled:
        return Condition(
            "no PII leak", NOT_MEASURABLE,
            f"{len(settled)} run(s) settled clean, but {len(unsettled)} kept no record of "
            "what they sent stage 2. A sweep that skipped part of the window has not made "
            "a claim about the window.",
            artifacts, detail)

    return Condition(
        "no PII leak", MET,
        f"{len(settled)} run(s) settled, no value survived tokenization in any of them"
        + (f" ({len(no_scope)} further run(s) had no scope and so no gate to fail)."
           if no_scope else "."),
        artifacts, detail)


def drafts_accepted_ratio(carrying: int, floor: int, artifacts: list[str],
                          table: dict | None, offline_note: str | None) -> Condition:
    """The ratio the milestone asks the audit to record, or why there is not one.

    `sent unchanged` over the conversations a human actually judged. Two absences are
    excluded from the denominator and neither is a failure: a draft still sitting in the
    folder has not been judged yet, and a draft that vanished with no reply sent may have
    been perfect — on round fb7889ca the abandoned one was abandoned because the parent
    reached the reviewer on Facebook (STU-1100).

    Only drafts written by a commit carrying the voice rules count, which is STU-1182's
    finding applied rather than restated: reading a pre-rules draft as evidence about the
    rules would be reading a rule that never ran.

    `MET` here means the audit can state a ratio, not that the ratio is good. The
    milestone sets no bar, deliberately — the number is for a human to read, and a bar
    invented in this file would be a threshold nobody measured.
    """
    detail = {"carrying_conversations": carrying, "floor": floor}
    if offline_note:
        detail["offline"] = offline_note

    if carrying < floor:
        return Condition(
            "a drafts-accepted ratio the audit records", NOT_MEASURABLE,
            f"{carrying} conversation(s) drafted by a commit carrying the voice rules, "
            f"under the floor of {floor}. A ratio needs a denominator, and this is the "
            "finding rather than a defect in the reading: it is what decides whether the "
            "window extends or the criterion changes.",
            artifacts, detail)

    if table is None:
        return Condition(
            "a drafts-accepted ratio the audit records", NOT_MEASURABLE,
            f"{carrying} conversation(s) clear the floor of {floor}, but what was sent on "
            f"them was never read. {offline_note or 'No comparison was run.'}",
            artifacts, detail)

    judged = table["judged"]
    detail.update(table)
    if not judged:
        return Condition(
            "a drafts-accepted ratio the audit records", NOT_MEASURABLE,
            f"{carrying} conversation(s) drafted, none of them judged yet — every draft is "
            "either still awaiting review or was abandoned without a reply. Neither "
            "absence carries a verdict, so there is nothing to take a ratio over.",
            artifacts, detail)

    return Condition(
        "a drafts-accepted ratio the audit records", MET,
        f"{table['unchanged']} of {judged} judged conversation(s) were sent unchanged — "
        f"{table['ratio']:.0%}, over {carrying} drafted on a carrying commit. The bar is a "
        "human's to set; this states the number.",
        artifacts, detail)


def accepted_table(rows: list[dict], unchanged_label: str) -> dict:
    """`sent unchanged` over the conversations that were judged at all."""
    judged = [r for r in rows if r.get("similarity") is not None]
    unchanged = sum(1 for r in judged if r["outcome"] == unchanged_label)
    return {
        "conversations": len(rows),
        "judged": len(judged),
        "unchanged": unchanged,
        "ratio": round(unchanged / len(judged), 3) if judged else None,
        "by_outcome": {label: sum(1 for r in rows if r["outcome"] == label)
                       for label in sorted({r["outcome"] for r in rows})},
    }


# `not met` outranks `not measurable`, and both outrank silence. The family's shape:
# `audit_run.py recall` exits 1 on a leak and 2 on a run it could not settle, and the
# reason the numbers go that way round is that a found fault is actionable today while an
# unmeasured one is a trip back to the host.
EXIT_MET = 0
EXIT_NOT_MET = 1
EXIT_NOT_MEASURABLE = 2
# Nothing to read at all. Its own code rather than a `not measurable` verdict, because it
# is not a fact about the pilot: it is a reader pointed at the wrong directory, the refusal
# STU-1087 and STU-1109 both spell — never invent an answer for a question nobody asked of
# a real artifact.
EXIT_NOTHING_TO_READ = 5


def write_record(out: Path, payload: str) -> str | None:
    """Drop the record, or say why not. Never raises, and never decides the verdict.

    On the serving host `logs/` belongs to the service account STU-1116 created and the
    operator typing `make pilot-gate` does not, so the documented usage on the documented
    host cannot write here. Letting that propagate replaced the verdict with `make`'s
    own exit 2 — which is a *legitimate* answer in this command's contract, so a crash
    arrived wearing "a condition could not be measured" while all three had been. That is
    STU-1122's shape one command over: an exit code the host's file ownership can forge.

    The printed conditions are what an operator acts on; this file is a convenience, and
    a convenience must not be able to overwrite the answer. Swallowed the way
    `audit_filing` swallows a filing that fails and `alerts.notify` swallows a dead
    channel — in both cases because the thing that mattered is already delivered.
    """
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
    except OSError as exc:
        return f"could not write {out}: {exc}"
    return None


def verdict_exit(conditions: list[Condition]) -> int:
    if any(c.verdict == NOT_MET for c in conditions):
        return EXIT_NOT_MET
    if any(c.verdict == NOT_MEASURABLE for c in conditions):
        return EXIT_NOT_MEASURABLE
    return EXIT_MET
