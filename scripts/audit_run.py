#!/usr/bin/env python
"""CLI for auditing a classification run (used by /validate-classify-run).

    python scripts/audit_run.py metrics [--report reports/<id>.json] [--log logs/frontier.jsonl]
    python scripts/audit_run.py check   [--report ...] [--log ...] [--dump dumps/gmail-dump.json]
        (--dump defaults to the input the report recorded, STU-859)
    python scripts/audit_run.py rules   [--report ...] [--rules ...] [--corpus fixtures ...]
        (--rules defaults to the rule set the run pinned, recovered from git, STU-913)
    python scripts/audit_run.py cost    [--report ...] [--drafts-log logs/drafts/stages.jsonl]
        [--studio-log .studio/runs/<log>.jsonl]  (the fallback for a pre-STU-1012 run)
    python scripts/audit_run.py recall  [--report ...] [--keymap logs/anonymization/keymap.json]
        [--studio-log .studio/runs/<log>.jsonl ...]  (the fallback for a pre-STU-1036 run)
    python scripts/audit_run.py drafts  [--run <run-id>] [--draft-journal logs/drafts/journal.jsonl]
        (--run defaults to the run the report names)
    python scripts/audit_run.py call-floor [--report ...] [--studio-log .studio/runs/<log>.jsonl]
        [--drafts-log logs/drafts/stages.jsonl]
    python scripts/audit_run.py reviewed [--report ...] [--studio-log .studio/runs/<log>.jsonl ...]
        [--draft-journal logs/drafts/journal.jsonl] [--facts]
        (reads the mailbox: what the reviewer sent against what the drafter wrote)
    python scripts/audit_run.py coverage [--reports reports] [--audit-log audits/audit_log.json]
    python scripts/audit_run.py skip --run <run-id> --reason "..." [--skipped audits/skipped.json]
    python scripts/audit_run.py file [--reports reports] [--audit-log audits/audit_log.json]
    python scripts/audit_run.py log-check [--audit-log audits/audit_log.json]
    python scripts/audit_run.py sequence --studio-log .studio/runs/<log>.jsonl

`check` exits 1 on any violation and 2 when a whole check family could not run (an
absent frontier log, STU-912; an escalating run that priced nothing, STU-1057) — a
skipped family is not a pass. It and `reviewed` refuse outright, before reading
anything, when the report names a mailbox and this host is configured for a different
one (STU-1183): two mailboxes have been served, and a report from either read
identically — an audit that took one's ids to the other's store got nothing back and
read that as deleted mail. `coverage` exits 1 on
any report missing from the audit log, and `skip` marks a run that does not earn an
audit at all (STU-900). `file` writes the entry a push run's own artifacts already
determine, leaving `verdict` visibly unanswered (STU-1084); `coverage` prints those as
`UNREVIEWED` and still exits 0, that count being the review a person still owes. `cost` prices the run off the artifacts the run itself owns —
the report's `cost` block and the drafts stage log's `tokens` (STU-1012) — and falls
back to the Studio log for a run written before those existed (STU-1011), exiting 2 when
neither is there: a run whose worktree took its log has no cost, which is not the same
as a free one. `drafts` reads the drafting half off `logs/drafts/journal.jsonl` —
drafts created, drafts updated in place, and every non-`ok` row named with its draft id
(STU-1029) — and off the stage log for the one thing that journal cannot say: why a
conversation the report owed a reply was not drafted (STU-1186). Per execution, and an
execution recording no breakdown is reported `unrecorded` rather than as one that held
nothing back. `call-floor` measures a leg's own call count against the attempts the
run's Studio journal recorded (STU-1058), and names the retries beside the gap rather
than subtracting them from a count that now contains them (STU-1210). One row per leg
since STU-1246: both classify orchestrations now report attempts read off a child's own
per-stage record, so the classify leg makes the same claim the drafting leg does and
nothing was checking it. It exits 2 when no journal for the run survives or the run
recorded no leg at all to measure — a floor with nothing to measure it against, or
nothing to measure, is no measurement (STU-1092) — and 3 when a count it reports exceeds
the journal's, a floor above its own ceiling being wrong rather than low. A run that
measured one leg and not the other exits on the one it measured, with the other's row
null and named on stderr. Both it and `cost` fold every execution of the
drafting leg a run id carries rather than reading its last (STU-1111): a run killed
mid-fan-out resumes under the same id, and taking the last line published the resume's
spend, calls and draft count as the run's. An execution that left no terminal line is
counted and named, and a run carrying one reports no per-conversation figure at all.
`reviewed` also reports the three figures STU-1124's voice rules were meant to move —
the median of the answering part, the share under 200 characters and the semicolon —
each beside the archive count it faces and beside round `fb7889ca`, the round before
those rules landed (STU-1130). The archive figures are re-derived by the same extractor
that cuts the round, so both sides are cut the same way; `club-voice.skill.md` publishes
a hand read that lands ten characters higher, and both are printed.
`sequence` reads a drafting journal back as one ordered sequence per conversation —
group iterations, the stages each one ran and the verdict each pass drew (STU-1112).
The file's own order is not that: the fan-out writes into it concurrently under the root
run id, and a table traced top to bottom put four `draft-qa` passes on a group capped at
three. It exits 2 on a journal it cannot read and names, rather than drops, any execution
the journal does not tie to a conversation.
`recall` settles anonymization recall on the payloads the run recorded sending stage 2
(STU-1036), exiting 1 on any residual PII and 2 when the run kept no
record and no Studio child log stands in for it: a scan that read nothing is not a
clean scan. `log-check` exits 1 on any
audit entry whose key set or types drifted, or whose `schema_note` pins a sha that does
not bear it out
(STU-914). `--report` defaults to the newest file in reports/, and `--run` is
refused by every subcommand that would have ignored it (STU-1067).
"""

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

KERNEL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KERNEL_ROOT))

from email_classifier import cost, journal_sequence
from email_classifier.audit import (
    UNKNOWN_SELECTION,
    check_audit_entries,
    check_window_verdicts,
    check_cache_hits,
    check_dump_consistency,
    check_log_consistency,
    check_report,
    check_schema_note_pins,
    draft_activity,
    draft_selection,
    metrics,
    recall,
    rule_stats,
    stage2_calls,
    studio_payloads,
    studio_thread_id,
    unaudited_runs,
    unrecorded_drafting_note,
    unrecorded_selection_note,
    unreviewed_runs,
)
from email_classifier.audit_filing import file_unaudited
from email_classifier.core.contract import load_taxonomy
from email_classifier.anonymize import load_keymap, load_sent, sent_path
from email_classifier.orchestrate.provenance import LIVE_INPUT
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def _resolve_dump(explicit: Path | None, report: dict) -> Path | None:
    """The dump this run consumed — read from the report, never guessed (STU-859).

    Cross-checking against the wrong dump yields a wall of spurious violations that
    read as a pipeline bug, so a recorded input that is no longer on disk aborts the
    audit instead of falling back to a guess.
    """
    recorded = (report.get("provenance") or {}).get("input_path")
    if explicit is not None:
        if recorded and str(explicit) != recorded:
            print(f"warning: --dump {explicit} overrides recorded input {recorded}",
                  file=sys.stderr)
        return explicit
    if recorded == LIVE_INPUT:
        print(f"note: run consumed {LIVE_INPUT} — no dump exists to cross-check",
              file=sys.stderr)
        return None
    if not recorded:
        print("warning: report records no input source (written before STU-859) — "
              "pass --dump to cross-check the input", file=sys.stderr)
        return None
    path = Path(recorded)
    if not path.exists():
        sys.exit(f"recorded input {recorded} is not on disk — pass --dump explicitly")
    return path


def _assert_named_mailbox(report: dict) -> str | None:
    """Refuse to read a report against a mailbox other than the one it names (STU-1183).

    Same argument as `_resolve_dump` one field over: an audit should not guess what a
    run consumed. It matters most where a report's ids are taken to a store — Graph
    answers a foreign id with nothing, and nothing reads as deleted mail rather than as
    the wrong mailbox, which is three probes spent confirming a migration that never
    happened.

    Only a disagreement between two named mailboxes refuses. Either side unnamed is not
    a contradiction and cannot found one: a report written before this field, a
    delegated grant that addresses `/me`, or Gmail, which names no mailbox at all. The
    returned name is what the caller reports it read against.
    """
    from email_classifier.providers import configured_mailbox
    from email_classifier.stage1.policy import load_policy

    named = (report.get("provenance") or {}).get("mailbox")
    policy_path = Path("rules/policy.yaml")
    try:
        here = configured_mailbox(load_policy(policy_path).email_provider)
    except (OSError, ValueError, KeyError) as exc:
        # A refusal that cannot be founded is not a refusal (the shape `unusable_provider`
        # already takes): with no readable policy there is no mailbox on this side to
        # disagree with, and crashing an audit that never touches a mailbox on the way
        # to saying so is worse than saying it.
        print(f"warning: cannot tell which mailbox this host addresses ({policy_path}: "
              f"{exc}) — the report's mailbox was not cross-checked", file=sys.stderr)
        return named
    if named and here and named.casefold() != here.casefold():
        sys.exit(f"report was produced against {named} and this host is configured for "
                 f"{here} — refusing to cross-check one mailbox's run against another")
    return named


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _file_at_commit(commit: str, rel: str, repo: Path = REPO_ROOT) -> bytes | None:
    """A tracked file as of a commit, or None when git cannot produce it.

    Both things this CLI refuses to take on trust read a file out of history: the rule
    set a run pinned (STU-913) and the sha a `schema_note` pins (STU-914). `git show`
    fails identically for a commit this clone does not have and for a path that commit
    does not carry — the callers each say what that unverifiability means for them.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{rel}"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _resolve_rules(explicit: Path | None, report: dict, workdir: Path) -> tuple[Path | None, str]:
    """The rule set the run pinned, not the one on disk today (STU-913).

    `never_fires` drives rule removal, and it is a statement about the rule set the run
    classified with — the report records that set as `provenance.rules_sha256` (STU-819).
    Replaying against a tree that has moved since calls a rule added after the run dead
    and drops a rule the run exercised off the list entirely, with nothing in the output
    saying so: eight entries in `audits/audit_log.json` carry a hand-written caveat that
    is exactly this default going uncaught.

    Returns the rules file to replay and the provenance line that describes it. A null
    path means the pinned set is gone and the staleness split is not computed at all —
    a list describing a different rule set is worse than no list.
    """
    default = Path("rules/rules.yaml")
    recorded = (report.get("provenance") or {}).get("rules_sha256")
    if explicit is not None:
        if recorded and _sha256(explicit) != recorded:
            print(f"warning: --rules {explicit} is not the rule set the run pinned",
                  file=sys.stderr)
        return explicit, f"{explicit} (named on the command line)"
    if not recorded:
        print("warning: report records no rules_sha256 (written before STU-819) — "
              f"staleness is computed against {default} as it stands today", file=sys.stderr)
        return default, f"{default} as it stands today — the run pinned no rule set (pre-STU-819)"
    if _sha256(default) == recorded:
        return default, f"{default} (matches the rules_sha256 the run pinned)"

    commit = (report.get("provenance") or {}).get("git_commit")
    blob = _file_at_commit(commit, str(default)) if commit else None
    if blob is not None and hashlib.sha256(blob).hexdigest() == recorded:
        recovered = workdir / "rules.yaml"
        recovered.write_bytes(blob)
        print(f"note: {default} has moved since the run — replaying against "
              f"{default} at {commit[:12]}", file=sys.stderr)
        return recovered, f"{default} at {commit[:12]} — the rule set the run pinned"

    why = "the run recorded no commit" if not commit else (
        f"{default} at {commit[:12]} does not hash to it"
        if blob is not None else f"commit {commit[:12]} is not reachable"
    )
    print(f"warning: the run pinned rules_sha256 {recorded[:12]} and {why} — "
          "never_fires and unexercised_by_this_run not computed", file=sys.stderr)
    return None, (f"unrecoverable: the run pinned rules_sha256 {recorded[:12]} and {why}; "
                  "the staleness split is not computed")


def _dump_hash_violations(report: dict, dump_path: Path) -> list[str]:
    recorded = (report.get("provenance") or {}).get("input_sha256")
    if not recorded:
        return []
    actual = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    if actual == recorded:
        return []
    return [f"{dump_path} sha256 {actual[:12]} != recorded {recorded[:12]} "
            f"(input rewritten since the run)"]


def _pinned_file_sha256(repo: Path):
    """`schema_note` pin resolver (STU-914) — the check's only I/O, kept out of `audit.py`.

    None means the claim could not be re-derived at all, which the caller reports as a
    violation: an unverifiable pin is as good as none.
    """
    def resolve(sha: str, path: str) -> str | None:
        blob = _file_at_commit(sha, path, repo)
        return None if blob is None else hashlib.sha256(blob).hexdigest()
    return resolve


def _is_shallow(repo: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
                          capture_output=True, text=True)
    return proc.stdout.strip() == "true"


def _reports(reports_dir: Path) -> list[Path]:
    reports = sorted(reports_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not reports:
        sys.exit(f"no report found in {reports_dir} — run `make run` first")
    return reports


def _latest_report(reports_dir: Path) -> Path:
    return _reports(reports_dir)[-1]


def _load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_input(path: Path) -> list[dict]:
    """A run's input as message records — a JSON dump, or a fixture corpus directory."""
    if path.is_dir():
        from email_classifier.eval.harness import load_corpus

        return [{"message_id": e.message_id} for e in load_corpus(path)]
    return _load_json(path)


def _load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _load_entries(path: Path) -> list[dict]:
    return _load_json(path) if path.exists() else []


def _skip(args) -> int:
    """Mark a run as not worth auditing (STU-900).

    Three refusals, each the marker's point: a run id no report bears is a typo, an
    already-audited run is a contradiction, and a reason-less entry excuses nothing
    downstream anyway.
    """
    if not args.run:
        sys.exit("skip needs --run <run-id>")
    reason = (args.reason or "").strip()
    if not reason:
        sys.exit("skip needs --reason — a marker naming no reason is not a decision")
    report = args.reports / f"{args.run}.json"
    if not report.exists():
        sys.exit(f"no report at {report} — nothing to skip")
    if any(e.get("run_id") == args.run for e in _load_entries(args.audit_log)):
        sys.exit(f"{args.run} is already audited in {args.audit_log}")
    entries = _load_entries(args.skipped)
    if any(e.get("run_id") == args.run for e in entries):
        sys.exit(f"{args.run} is already marked in {args.skipped}")
    entries.append({"run_id": args.run,
                    "date": datetime.date.today().isoformat(),
                    "reason": reason})
    args.skipped.parent.mkdir(parents=True, exist_ok=True)
    args.skipped.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    print(f"marked {args.run} not worth auditing in {args.skipped}")
    return 0


def _drafts(args, run_id: str) -> int:
    """What one run's drafting did (STU-1029), and why it drafted no more (STU-1186).

    Two files, because the per-draft journal cannot answer the second question: a
    conversation `drafts-pre` held back never reaches it, so nothing owed and a reply
    lost leave it looking identical. The stage log is where that decision was written.
    """
    if not args.draft_journal.exists():
        sys.exit(f"no drafts journal at {args.draft_journal} — nothing else records "
                 "which drafts a run put in the mailbox")
    activity = draft_activity(_load_jsonl(args.draft_journal), run_id)
    if not (activity["created"] or activity["updated"] or activity["problems"]):
        # A stage that drafted nothing and a stage that never ran leave the same
        # absence here; only its own record tells them apart (STU-917).
        print(f"note: no row in {args.draft_journal} names {run_id} — whether the stage "
              f"drafted nothing or never ran is answered by {args.drafts_log}",
              file=sys.stderr)
    closed, unrecorded = cost.drafts_executions(args.drafts_log, run_id)
    executions = draft_selection(closed)
    if any(e["selection"] == UNKNOWN_SELECTION for e in executions):
        print(f"note: {unrecorded_selection_note(args.drafts_log)}", file=sys.stderr)
    if unrecorded:
        # The same silence `cost` names: an execution killed before `drafts-post`
        # recorded neither what it drafted nor what it held, so the executions below
        # are not all of them (STU-1111).
        print(f"note: {unrecorded_drafting_note(unrecorded, run_id, args.drafts_log)}",
              file=sys.stderr)
    print(json.dumps({"journal": str(args.draft_journal), **activity,
                      "stage_log": str(args.drafts_log), "executions": executions,
                      "unrecorded_drafting_executions": unrecorded},
                     indent=2, ensure_ascii=False))
    return 0



def _counted_unit(recorded: dict | None) -> str:
    """Which unit the leg's `llm_calls` is in (STU-1210).

    `unrecorded_items` only exists on a leg written since attempts went into the count,
    so its presence dates the leg, and its value says how much of that leg fell back to
    the old unit anyway — a host below Studio 0.18.0 reports no per-stage record, so
    every item prices as one call whatever it made. Without this the same `undercount`
    means two opposite things and the payload cannot say which.

    One reading for both legs, because both go through `cost.leg` and it writes the
    field either way. Which change put the attempts there differs — STU-1210 for
    drafting and `classify-batch`, STU-1244 for the classify subprocess path — and the
    sentence names both rather than the leg, the field dating a leg without needing to
    know whose it is.
    """
    if recorded is None or "unrecorded_items" not in recorded:
        return ("stage executions — the leg predates the change that put attempts in "
                "its count (STU-1210 for drafting and `classify-batch`, STU-1244 for "
                "the classify subprocess path), so the engine's retries are on top of "
                "it and make up part of the gap")
    fell_back = recorded["unrecorded_items"]
    if fell_back:
        return (f"attempts, except for {fell_back} item(s) whose child reported no "
                "per-stage record and priced as one call each — a Studio below 0.18.0, "
                "so that part of the count is in the older stage-execution unit")
    return ("attempts — the retries are inside the leg's own count, so what is left of "
            "the gap is an execution nothing recorded")


def _reported_drafts(drafts_log: Path, recorded: dict | None,
                     run_id: str) -> tuple[int | None, str]:
    """The drafts leg's own call count, and where it was read.

    Only the recorded leg answers, and a run that recorded none is reported unmeasured
    (STU-1092). Recomputing it was the other branch, and it counted mailbox writes as
    calls: a run old enough to have recorded no leg predates STU-1056, so it also
    predates STU-1030, when the drafting agent still held the write tool and re-wrote on
    every revision. Run 86130fae left 81 `ok` rows for 14 conversations, which the rule
    doubled into a floor of 162 against the 48 calls its Studio journal shows. A floor
    above its own ceiling is worse than no floor, and the recompute could only ever be a
    guess about code that is gone.
    """
    if recorded and recorded.get("llm_calls") is not None:
        return recorded["llm_calls"], str(drafts_log)
    return None, f"unmeasured — {drafts_log} records no leg for {run_id}"


def _reported_classify(report_path: Path, recorded: dict | None,
                       run_id: str) -> tuple[int | None, str]:
    """The classify leg's own call count, and where it was read (STU-1246).

    A second source rather than a second call to the same one: the drafting leg is
    folded over the rows one stage log holds per execution, the classify leg is a
    single block the run wrote into its own report. Read off `cost.classify` directly
    and not through `cost.report_leg`, which answers None for a leg that priced
    nothing — a leg whose every call died still reported how many it made, and that
    count is the whole of what is measured here.

    Zero is a measurement and not an absence: a fan-out every item resumed from cache
    made no calls, recorded none, and agrees with a journal that holds none.
    """
    if recorded and recorded.get("llm_calls") is not None:
        return recorded["llm_calls"], str(report_path)
    return None, f"unmeasured — {report_path} records no classify leg for {run_id}"


def _journal_calls(logs, stages) -> dict[str, dict[str, int]]:
    """The attempts every Studio journal naming the run recorded, for `stages`
    (STU-1111).

    `stages` is which stages the leg being measured runs, so each leg is measured
    against the attempts of the very stages its own count came from (STU-1246).

    Every journal and not one: a run resumed under its own id wrote a second one, and
    together they are the run. Summed per log rather than over the concatenation,
    because `journal_calls` counts a stage at the deepest depth its name appears at —
    one log nesting the fan-out a level deeper than another would drop the shallower
    log's stages entirely.
    """
    total: dict[str, dict[str, int]] = {}
    for path in logs:
        for stage, counted in cost.journal_calls(cost.load_events(Path(path)),
                                                 stages).items():
            running = total.setdefault(stage, {"executions": 0, "retries": 0, "calls": 0})
            for key, value in counted.items():
                running[key] += value
    return total


def _leg_floor(name: str, stages, reported: int | None, source: str,
               recorded: dict | None, logs, extra: dict) -> dict:
    """One leg's own count beside the attempts the journal recorded for its stages."""
    by_stage = _journal_calls(logs, stages)
    # No stage of the leg in any journal is a journal of another leg, not a leg that
    # made no call: a push run's classify children each wrote a journal that names no
    # run of ours, so the only one found is its drafting journal (STU-2059).
    made = sum(s["calls"] for s in by_stage.values()) if by_stage else None
    return {
        "leg": name,
        **extra,
        "reported_calls": reported,
        "reported_from": source,
        "journal_calls": made,
        "undercount": (made - reported
                       if reported is not None and made is not None else None),
        # No unit for a leg nothing measured: the pre-STU-1210 sentence would read as a
        # fact about a leg that does not exist, now that an unmeasured row is printed
        # beside a measured one rather than being the whole payload.
        "reported_unit": _counted_unit(recorded) if reported is not None else None,
        "ralph_retries": sum(s["retries"] for s in by_stage.values()),
        "by_stage": by_stage,
    }


def _call_floor(args, report_path: Path, report: dict) -> int:
    """What each leg reported spending calls on, next to what it spent (STU-1058).

    Both legs and not one (STU-1246). While the classify leg counted one call per
    escalated item, its floor was a stated shape with an obvious size — one per
    escalation, every retry invisible — and only the drafting leg's claim was worth
    measuring. STU-1210 and STU-1244 made the two claims the same claim: both classify
    orchestrations now report `llm_calls` as the RALPH attempts read off a child's own
    per-stage record, which is exactly what this command exists to check against the
    journal that recorded those attempts independently. The subprocess path is the more
    worth measuring of the two, being the one where this repo *translates* the record
    rather than reading it at the spawner boundary.

    One command and not a `--leg` flag: a `mailbox-full` run has both legs, and an
    auditor reading it wants both numbers side by side. The journal side needs no new
    rule — each leg names the stages its child runs, and `journal_calls` filters by
    them, the same pairing on both sides that STU-1210 set up for drafting.

    A leg written since STU-1210 counts a child stage's RALPH attempts, so the retries
    the journal records are inside its own number and the difference that remains is
    calls from an execution the leg never recorded at all. That is why the difference is
    no longer broken out by cause: the split was additive only while `leg` counted stage
    executions, and subtracting the retries from a count that now contains them yields a
    negative cause. `ralph_retries` stays beside it as the journal's own observation —
    for a leg written before STU-1210, it is still the share of the undercount the
    engine's retries account for. Which of the two a payload shows is `reported_unit`,
    read off the leg itself rather than guessed: without it the same `undercount` reads
    two opposite ways and nothing in the artifact says which.

    Every journal naming the run is read, not one (STU-1111). A run resumed under its
    own id left two, and comparing the leg's one count against either alone compares a
    number to half its own ceiling — which is how `fb7889ca` reported 20 calls against
    the 14 its resume's journal holds and exited 3. `unrecorded_leg_executions` is the
    other half of the same resume, and it is why the undercount can be large without the
    leg's own count being wrong. It stays on the drafting row alone: a resume re-opens
    that stage log, while the classify leg is one block a run wrote into its own report.

    Exit 2 is now no leg measured rather than one leg missing, and the cost of that is
    named: a `mailbox-full` run whose drafting leg went unrecorded exits 0 on its
    classify row where it used to exit 2. Any leg is a legitimate whole run — a
    classify-only `run-batch` has no drafting to measure, and a rule that exited 2 there
    forever would make this the check nobody reads on the shape it reads most. What
    keeps that from reading as a pass over an unmeasured leg is the row itself:
    `reported_calls` is null, `reported_from` says unmeasured, and stderr names it.
    """
    run_id = report.get("run_id")
    logs = args.studio_log or cost.find_logs(run_id, args.runs_dir)
    if not logs:
        print(f"no Studio journal under {', '.join(str(d) for d in args.runs_dir)} names "
              f"{run_id} — the retries a run made are recorded nowhere else, so its floor "
              "has nothing to be measured against. Report it as unmeasured, never as "
              "exact.", file=sys.stderr)
        return 2

    classify = (report.get("cost") or {}).get(cost.CLASSIFY_LEG)
    drafts = cost.drafts_leg(args.drafts_log, run_id)
    _, unrecorded = cost.drafts_executions(args.drafts_log, run_id)
    rows = [
        _leg_floor(cost.CLASSIFY_LEG, cost.CLASSIFY_AGENT_STAGES,
                   *_reported_classify(report_path, classify, run_id),
                   classify, logs, {}),
        _leg_floor(cost.DRAFTS_LEG, cost.DRAFTING_AGENT_STAGES,
                   *_reported_drafts(args.drafts_log, drafts, run_id),
                   drafts, logs, {"unrecorded_leg_executions": unrecorded}),
    ]
    print(json.dumps({
        "report": str(report_path),
        "studio_logs": [str(p) for p in logs],
        "run_id": run_id,
        "legs": rows,
    }, indent=2, ensure_ascii=False))

    over = [row for row in rows if (row["undercount"] or 0) < 0]
    if over:
        for row in over:
            print(f"{row['leg']}: reported_calls {row['reported_calls']} exceeds the "
                  f"{row['journal_calls']} calls "
                  f"{', '.join(str(p) for p in logs)} record: a floor above its own "
                  "ceiling is not a floor, so one of the two is wrong. Since STU-1210 "
                  "the leg counts the same attempts the journal does, so the two are "
                  "expected to be equal and a journal that aged off the 30-day shelf "
                  "produces this as readily as a bad count — read `studio_logs` before "
                  "the leg.", file=sys.stderr)
        return 3
    def why(row):
        if row["reported_calls"] is None:
            return row["reported_from"]
        return (f"unmeasured: no journal of {', '.join(str(p) for p in logs)} runs a "
                f"{row['leg']} stage, so the {row['reported_calls']} call(s) the leg "
                "claims have no ceiling here")

    unmeasured = [row for row in rows
                  if row["reported_calls"] is None or row["journal_calls"] is None]
    if len(unmeasured) == len(rows):
        for row in unmeasured:
            print(f"{row['leg']}: {why(row)}", file=sys.stderr)
        print("no leg is measured, which is not a gap of zero.", file=sys.stderr)
        return 2
    for row in unmeasured:
        print(f"{row['leg']}: {why(row)} — that leg is unmeasured, which is "
              "not a gap of zero; the exit code answers for the leg(s) that were.",
              file=sys.stderr)
    return 0


def _fallback_payloads(args, report: dict, run_id: str) -> tuple[list[dict], list[str], str | None]:
    """The Studio child logs standing in for a run that kept no record of its own.

    Two ways in, and they are not equally strong. A log that names the run id is that
    run's beyond doubt — the drafts fan-out carries it in every item. A classify child
    log carries none: the push path spawns one `studio run classify` per conversation
    and the item is the mail, so the only handle is the conversation it opens on. Every
    log opening on a thread this report escalated is therefore read, and the caveat is
    reported rather than resolved: two runs over the same thread produce two
    indistinguishable logs, so the set can include a sibling run's send of the same
    conversation. It over-includes, never under-includes, which is the direction that
    finds a leak rather than missing one.
    """
    if args.studio_log:
        payloads = [payload for path in args.studio_log
                    for payload in studio_payloads(_events(path) or [])]
        return payloads, [str(p) for p in args.studio_log], None

    named = cost.find_logs(run_id, args.runs_dir)
    threads = {v.get("thread_id") for v in report.get("verdicts", [])
               if v.get("path") == "llm" and v.get("thread_id")}
    seen = {p.resolve() for p in named}
    matched = {}
    for path in _candidate_logs(args.runs_dir):
        if path.resolve() in seen:
            continue
        events = _events(path)
        if events is not None and studio_thread_id(events) in threads:
            seen.add(path.resolve())
            matched[path] = events

    logs = named + list(matched)
    payloads = [payload for path in logs
                for payload in studio_payloads(matched.get(path) or _events(path) or [])]
    caveat = matched and (
        f"{len(matched)} of {len(logs)} log(s) were matched on the conversation they "
        "open on, not on the run id — a sibling run's send of the same conversation "
        "would be read here too")
    return payloads, [str(p) for p in logs], caveat or None


def _candidate_logs(runs_dirs) -> list[Path]:
    """Every Studio log on the shelves, each real file once.

    Deduped on the resolved path because a worktree's `.studio/runs` is a symlink to
    the main checkout's, and the shipped `--runs-dir` default names both: reading a log
    twice doubles `payloads` and the `occurrences` an auditor triages a hit on.
    """
    return sorted(p for d in runs_dirs if Path(d).is_dir() for p in Path(d).glob("*.jsonl"))


def _events(path: Path) -> list[dict] | None:
    """One child log, or None when it cannot be read.

    Tolerant where `cost.load_events` is not, and deliberately so: pricing reads the
    one log a run names, while this sweeps a whole directory, where a log Studio is
    still writing has a half-flushed last line. Letting that raise would exit 1 — the
    code reserved for a leak — on a neighbour that has nothing to do with this run.
    """
    try:
        return cost.load_events(path)
    except (OSError, ValueError):
        print(f"warning: skipping unreadable {path}", file=sys.stderr)
        return None


def _recall(args, report_path: Path, report: dict) -> int:
    run_id = report.get("run_id")
    fields = (report.get("provenance") or {}).get("anonymized_fields") or []
    if not fields:
        # No scope, so nothing claimed to be tokenized and there is no gate to fail —
        # and no record either, which is the point: writing what a cleartext run sent
        # would leave a run-named mail corpus on a push path whose whole premise is
        # that the mail never touched disk (STU-859).
        print(f"{run_id} recorded no anonymized_fields — stage 2 saw the mail in clear "
              "by policy, so it has no recall to settle.", file=sys.stderr)
        return 0

    recorded = sent_path(args.keymap, run_id)
    caveat = None
    if recorded.exists() and not args.studio_log:
        payloads, source = load_sent(recorded), {"sent_log": str(recorded)}
    else:
        payloads, logs, caveat = _fallback_payloads(args, report, run_id)
        source = {"studio_logs": logs}
        if not payloads:
            print(f"{run_id} kept no record of what it sent stage 2, and no Studio child "
                  f"log under {', '.join(str(d) for d in args.runs_dir)} stands in for "
                  "one. A run written before STU-1036 whose logs have since been pruned "
                  "cannot have its recall settled — report it as unknown, never as "
                  "clean.", file=sys.stderr)
            return 2

    result = recall(payloads, load_keymap(args.keymap))
    if caveat:
        result["attribution_caveat"] = caveat
    print(json.dumps({"report": str(report_path), "run_id": run_id,
                      "anonymized_fields": fields, **source, **result},
                     indent=2, ensure_ascii=False))
    return 1 if result["leaks"] else 0


def _own_legs(args, report: dict, run_id: str) -> dict[str, dict]:
    """What the run's own artifacts say it spent (STU-1012), leg by leg."""
    legs = {}
    if (classify := cost.report_leg(report)) is not None:
        legs[cost.CLASSIFY_LEG] = classify
    if (drafting := cost.drafts_leg(args.drafts_log, run_id)) is not None:
        legs[cost.DRAFTS_LEG] = drafting
    return legs


UNMATCHED_ITEM = "unmatched item"
NO_WRITE_TIME = "no write time"


def _reviewed(args, report_path: Path, report: dict) -> int:
    """What the reviewer did to each draft before sending it (STU-1100).

    Both halves are read where they survive the send: our draft from the Studio
    journal's `generate-draft` outputs, their reply from Sent Items. The draft itself
    is gone by then — sending it moves it out of Drafts — so this is the only window
    in which the comparison exists at all.

    Every log naming the run is read, not one. A run resumed under its own id has two,
    and taking the last would score the resume's conversations as though they were the
    run's — the accounting defect STU-1111 describes one command over.

    The `voice` block is the honest test of STU-1124's three rules (STU-1130) and, since
    STU-1239, STU-1129's three comma rules too: they are counts over the archive, so
    nothing in them says our drafts moved. Read it against `round_before`, which is what
    the drafter wrote before the rules existed. A figure that did not move is the
    finding, not something to explain away.
    """
    from email_classifier import draft_delta, draft_pairing
    from email_classifier.placeholder import flag_sent
    from email_classifier.providers import get_drafter, get_thread_reader
    from email_classifier.qa_bakeoff import drafts_from_journal
    from email_classifier.stage1.policy import load_policy

    # Before a single id leaves for Graph: this is the command the mailbox field was
    # written for (STU-1183). Every row unjoined is what a foreign store looks like
    # from here, and it is indistinguishable from a round nobody sent.
    mailbox = _assert_named_mailbox(report)
    run_id = report.get("run_id")
    if not args.draft_journal.exists():
        sys.exit(f"no drafts journal at {args.draft_journal}: it is the only record of "
                 "when each draft was written, and a draft is paired with the first "
                 "send after that instant")
    logs = args.studio_log or cost.find_logs(run_id, args.runs_dir)
    if not logs:
        sys.exit(f"no Studio journal under {', '.join(str(d) for d in args.runs_dir)} "
                 f"names {run_id} — nothing else records what the drafter wrote, and "
                 "the draft left the mailbox when it was sent")

    latest: dict[str, tuple[int, str]] = {}
    # A draft whose thread no drafts-pre item carries (STU-2061): the drafter echoes the
    # thread id and can mis-copy it, so the id names no conversation Graph could join.
    # It is still a draft the reviewer saw, so it is a row, not a traceback.
    unmatched: set[str] = set()
    for path in logs:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        for draft in drafts_from_journal(lines, strict=False):
            if draft.item is None:
                unmatched.add(draft.thread_id)
                continue
            body = draft.draft.get("draft_body") or ""
            if draft.thread_id not in latest or draft.attempt >= latest[draft.thread_id][0]:
                latest[draft.thread_id] = (draft.attempt, body)
    written = draft_pairing.write_times(_load_jsonl(args.draft_journal), run_id)
    # A draft the journal never saw written has no instant to pair from: guessing one
    # is how the newest send got compared, so it is a row and not a score.
    untimed = {t for t in latest if t not in written}
    drafted = {thread_id: body for thread_id, (_, body) in latest.items()
               if thread_id not in untimed}
    if not drafted and not unmatched and not untimed:
        sys.exit(f"no generate-draft output in {', '.join(str(p) for p in logs)}")

    provider = load_policy(Path("rules/policy.yaml")).email_provider
    reader = get_thread_reader(provider)
    replies = reader.sent_replies({t: written[t] for t in sorted(drafted)})
    # A draft still in the folder has not been judged; one that is gone with nothing
    # sent was judged and dropped. Merging them makes a review still under way read
    # as a round of bad drafts.
    drafter = get_drafter(provider)
    still = {t for t in drafted if drafter.find_draft(t)}
    sent_bodies = {t: r["body"] for t, r in replies.items()}
    rows = draft_delta.compare(drafted, sent_bodies, still)
    sent_with_placeholder = flag_sent(sent_bodies)
    unjoined = sum(1 for r in rows if r["similarity"] is None)
    summary = draft_delta.summarise(rows, list(drafted.values()), list(sent_bodies.values()))
    if args.facts:
        from email_classifier import fact_read
        # Lexical bands stay in `by_outcome`; this is the reading an audit quotes.
        summary["by_facts"] = fact_read.annotate(rows, drafted, sent_bodies, Path.cwd(),
                                                  args.provider or fact_read.DEFAULT_PROVIDER)
    rows += [{"thread_id": t, "outcome": UNMATCHED_ITEM} for t in sorted(unmatched)]
    rows += [{"thread_id": t, "outcome": NO_WRITE_TIME} for t in sorted(untimed)]

    print(json.dumps({
        "report": str(report_path),
        "run_id": run_id,
        "studio_logs": [str(p) for p in logs],
        # Named rather than assumed: the join is what this whole reading rests on, and
        # on this mailbox `conversationId` is exactly the key that has failed before.
        "join_key": "conversationId",
        "join_rule": "first send after the draft's last write in the drafts journal",
        # Which store that key was resolved in, or null where the report predates the
        # field: an unjoined row means something different in each case.
        "mailbox": mailbox,
        "unjoined": unjoined,
        "unmatched_items": len(unmatched),
        "no_write_time": len(untimed),
        # A parent received the drafter's `[À COMPLÉTER` marker (STU-2049).
        "sent_with_placeholder": sent_with_placeholder,
        **summary,
        "rows": rows,
    }, indent=2, ensure_ascii=False))
    return 0


def _studio_log(args, run_id: str) -> Path | None:
    """The Studio log of a run written before its artifacts carried their own counts."""
    if args.studio_log is not None:
        # `recall` reads several at once, so the flag is a list; a cost is one run's.
        if len(args.studio_log) > 1:
            sys.exit("cost prices one run: pass a single --studio-log")
        return args.studio_log[0]
    found = cost.find_logs(run_id, args.runs_dir)
    if not found:
        return None
    if len(found) > 1:
        sys.exit(f"{len(found)} logs name {run_id}: "
                 f"{', '.join(str(p) for p in found)} — pass --studio-log")
    return found[0]


def _cost(args, report_path: Path, report: dict) -> int:
    """What the run spent, over the artifacts it owns — or the Studio log fallback.

    A drafting leg that ran more than once under one run id is summed, not taken from
    its last pass (STU-1111); an execution that left no terminal line makes every
    figure over it a floor, and the note says which.
    """
    run_id = report.get("run_id")
    # An explicitly named log wins over the run's own legs: naming one is how an
    # operator picks between the several that can name a run, and silently reading
    # something else would answer a question nobody asked.
    legs = {} if args.studio_log is not None else _own_legs(args, report, run_id)
    if legs:
        drafted = cost.drafts_denominator(args.drafts_log, run_id)
        result, source = cost.combine(legs, drafted), {"legs": sorted(legs)}
        _, unrecorded = cost.drafts_executions(args.drafts_log, run_id)
        if unrecorded:
            result["unrecorded_drafting_executions"] = unrecorded
            result["schema_note"] = unrecorded_drafting_note(
                unrecorded, run_id, args.drafts_log)
    else:
        log = _studio_log(args, run_id)
        if log is None:
            print(f"{run_id} recorded no cost of its own, and no Studio run log names "
                  f"it under {', '.join(str(d) for d in args.runs_dir)} — a run "
                  "predating STU-1012 left its log beside the cwd it ran from, and a "
                  "worktree since removed took it along. Report the cost as unknown, "
                  "never as zero.", file=sys.stderr)
            return 2
        result, source = cost.run_cost(cost.load_events(log)), {"studio_log": str(log)}

    emails = report.get("totals", {}).get("emails") or 0
    escalated = report.get("totals", {}).get("llm") or 0
    print(json.dumps({
        "report": str(report_path),
        **source,
        **result,
        "usd_per_email": round(result["usd"] / emails, 3) if emails else None,
        "usd_per_escalation": round(result["usd"] / escalated, 3) if escalated else None,
    }, indent=2, ensure_ascii=False))
    return 0


def _sequence(args) -> int:
    """Read one drafting journal back as a sequence per conversation (STU-1112)."""
    if args.studio_log is None or len(args.studio_log) != 1:
        sys.exit("sequence reads one journal: pass a single --studio-log")
    path = args.studio_log[0]
    try:
        events = cost.load_events(path)
    except (OSError, ValueError) as exc:
        print(f"cannot read {path}: {exc}", file=sys.stderr)
        return 2
    found, unattributed = journal_sequence.conversations(events)
    print(journal_sequence.render(found, unattributed))
    return 0


_RUN_AWARE = {"drafts", "skip"}
_REPORT_AWARE = {"metrics", "check", "rules", "cost", "recall", "call-floor", "reviewed"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=["metrics", "check", "rules", "cost", "coverage", "skip",
                                 "file", "log-check", "drafts", "recall", "call-floor",
                                 "reviewed", "sequence"])
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--log", type=Path, default=Path("logs/frontier.jsonl"))
    parser.add_argument("--dump", type=Path, default=None)
    # Defaulted late: with no --rules the run's own pinned set wins over the tree (STU-913).
    parser.add_argument("--rules", type=Path, default=None)
    # A rule is dead only if no corpus fires it: a deployment with a second corpus passes
    # every one, since a rule its first corpus never exercises is not thereby dead.
    parser.add_argument("--corpus", type=Path, nargs="+",
                        default=[REPO_ROOT / "fixtures"])
    parser.add_argument("--reports", type=Path, default=Path("reports"))
    parser.add_argument("--audit-log", type=Path, default=Path("audits/audit_log.json"))
    parser.add_argument("--skipped", type=Path, default=Path("audits/skipped.json"))
    parser.add_argument("--drafts-log", type=Path, default=Path("logs/drafts/stages.jsonl"),
                        help="where the drafts stage recorded what its fan-out spent")
    parser.add_argument("--draft-journal", type=Path,
                        default=Path("logs/drafts/journal.jsonl"),
                        help="the shared per-draft journal every run appends to")
    parser.add_argument("--studio-log", type=Path, nargs="+", default=None,
                        help="the run's .studio/runs/*.jsonl (default: the one naming its run id)")
    parser.add_argument("--keymap", type=Path,
                        default=Path("logs/anonymization/keymap.json"),
                        help="the shared keymap; the run's own record sits beside it")
    parser.add_argument("--runs-dir", type=Path, nargs="+",
                        default=[Path(".studio/runs"),
                                 *Path(".claude/worktrees").glob("*/.studio/runs")],
                        help="where to look for that log — a run launched from a worktree "
                             "left its log there")
    parser.add_argument("--run", default=None,
                        help="the run to read: one to mark not worth auditing (skip), "
                             "or whose drafting to report (drafts)")
    parser.add_argument("--reason", default=None, help="why that run does not earn an audit")
    parser.add_argument("--facts", action="store_true",
                        help="reviewed: ask the draft-facts reader whether each sent reply "
                             "kept, extended or contradicted the draft (live LLM calls)")
    # The reader reads real mail, so it runs where the host runs (STU-1128, STU-2088).
    parser.add_argument("--provider", default=os.environ.get("STUDIO_PROVIDER") or None,
                        help="reviewed --facts: the reader's provider "
                             "(default: STUDIO_PROVIDER, else the reader's own default)")
    args = parser.parse_args()

    # `--run` is top-level and only two subcommands read it, so anywhere else it was
    # accepted and ignored — the answer came from the newest report on disk, under the
    # run id the caller named (STU-1067).
    if args.run is not None and args.command not in _RUN_AWARE:
        hint = (f" — name it with --report {args.reports}/{args.run}.json"
                if args.command in _REPORT_AWARE else "")
        sys.exit(f"{args.command} does not read --run{hint}")

    if args.command == "log-check":
        if not args.audit_log.exists():
            sys.exit(f"no audit log at {args.audit_log}")
        entries = _load_json(args.audit_log)
        repo = args.audit_log.resolve().parent
        drift = check_audit_entries(entries)
        pins = check_schema_note_pins(entries, _pinned_file_sha256(repo))
        pins += check_window_verdicts(entries, repo.parent)
        for d in drift + pins:
            print(f"DRIFT: {d}")
        if pins and _is_shallow(repo):
            print("warning: this clone is shallow — run `git fetch --unshallow` before "
                  "reading an unverifiable pin as a bad sha", file=sys.stderr)
        total = len(drift) + len(pins)
        print(f"{total} violation(s) across {len(entries)} entries in {args.audit_log}")
        return 1 if total else 0

    if args.command == "sequence":
        return _sequence(args)

    if args.command == "skip":
        return _skip(args)

    if args.command == "file":
        filed = file_unaudited(args.reports, args.audit_log, args.skipped,
                               args.log, args.drafts_log,
                               load_taxonomy(Path("rules/taxonomy.yaml")))
        for rid in filed:
            print(f"FILED: {rid}")
        print(f"{len(filed)} push run(s) filed into {args.audit_log} — "
              "each awaits a verdict")
        return 0

    if args.command == "coverage":
        report_paths = _reports(args.reports)
        if not args.audit_log.exists():
            sys.exit(f"no audit log at {args.audit_log} — every run is unaudited")
        entries = _load_json(args.audit_log)
        skipped = _load_entries(args.skipped)
        run_ids = [_load_json(p).get("run_id") for p in report_paths]
        missing = unaudited_runs(run_ids, entries, skipped)
        audited = {e.get("run_id") for e in entries}
        excused = {e.get("run_id"): (e.get("reason") or "").strip()
                   for e in skipped if (e.get("reason") or "").strip()}
        for rid in run_ids:
            if rid not in audited and rid in excused:
                print(f"SKIPPED: {rid} — {excused[rid]}")
        for rid in missing:
            print(f"UNAUDITED: {rid}")
        # The debt filing creates (STU-1084). The gate no longer holds for these, so
        # this count is the only thing left saying a person still owes them a reading;
        # printing nothing is the silence `audit-skip` refuses. Read off the log and
        # not off what `reports/` still holds: the entry is the thing owed a verdict,
        # it outlives the report by design (`retention` stops protecting a run once
        # filing covers it), and intersecting would let a prune quietly pay the debt.
        unreviewed = unreviewed_runs(entries)
        for rid in unreviewed:
            print(f"UNREVIEWED: {rid}")
        print(f"{len(missing)} of {len(run_ids)} run(s) missing from {args.audit_log}")
        if unreviewed:
            print(f"{len(unreviewed)} filed run(s) carry no verdict — machine-filed, "
                  "unread. Audit them with /validate-classify-run and replace the "
                  "`verdict` field.")
        if missing:
            print("audit each one with /validate-classify-run, or mark it not worth "
                  'auditing: make audit-skip RUN_ID=<id> REASON="..."')
        return 1 if missing else 0

    if args.command == "drafts":
        run_id = args.run or _load_json(args.report or _latest_report(args.reports)).get("run_id")
        return _drafts(args, run_id)

    report_path = args.report or _latest_report(args.reports)
    report = _load_json(report_path)
    log_entries = _load_jsonl(args.log) if args.log.exists() else None

    if args.command == "metrics":
        print(json.dumps(
            {"report": str(report_path), **metrics(report, log_entries, str(args.log))},
            indent=2, ensure_ascii=False))
        return 0

    if args.command == "cost":
        return _cost(args, report_path, report)

    if args.command == "call-floor":
        return _call_floor(args, report_path, report)

    if args.command == "recall":
        return _recall(args, report_path, report)

    if args.command == "reviewed":
        return _reviewed(args, report_path, report)

    if args.command == "check":
        _assert_named_mailbox(report)
        violations = check_report(report, load_taxonomy(Path("rules/taxonomy.yaml")))
        # A check family that never ran is its own outcome, not a silent pass (STU-912):
        # `check` is the audit's integrity gate, and exiting 0 having skipped the whole
        # log ⇔ report cross-check reads as one that held.
        incomplete: list[str] = []
        if log_entries is not None:
            violations += check_log_consistency(report, log_entries)
        else:
            incomplete.append(f"no frontier log at {args.log} — the log ⇔ report message "
                              "set and escalation set were not cross-checked")
        dump_path = _resolve_dump(args.dump, report)
        if dump_path:
            violations += check_dump_consistency(report, _load_input(dump_path))
            violations += _dump_hash_violations(report, dump_path)
        legs = _own_legs(args, report, report.get("run_id"))
        # A leg that structurally cannot reuse a prefix is a note, not a violation
        # (STU-1145) — nothing in this repo repairs it, and exit 1 on every run is
        # how a check stops being read.
        cache_violations, cache_notes = check_cache_hits(cost.hit_ratios(legs))
        violations += cache_violations
        # A run that escalated and recorded no classify spend leaves the family nothing
        # to read, which is its own outcome (STU-1057). Two silences are not that: a
        # leg under the call floor is complete evidence a first call cannot carry a
        # ratio for, and an absent drafts leg is indistinguishable from a run that
        # never drafted — the report does not say whether a drafts stage ran.
        if cost.CLASSIFY_LEG not in legs and stage2_calls(report.get("verdicts", [])):
            incomplete.append(f"{report_path} {cost.unpriced_classify(report)}")
        for v in violations:
            print(f"VIOLATION: {v}")
        for n in cache_notes:
            print(f"NOTE: {n}")
        for i in incomplete:
            print(f"INCOMPLETE: {i}")
        skipped = f", {len(incomplete)} check family skipped" if incomplete else ""
        print(f"{len(violations)} violation(s) in {report_path}{skipped}")
        if violations:
            return 1
        return 2 if incomplete else 0

    from email_classifier.eval.harness import load_corpus
    from email_classifier.stage1.rule_engine import apply, load_rules

    with tempfile.TemporaryDirectory() as tmp:
        rules_path, rules_source = _resolve_rules(args.rules, report, Path(tmp))
        rule_ids = corpus_fired = None
        if rules_path is not None:
            rules = load_rules(rules_path)
            for corpus in args.corpus:
                if not corpus.exists():
                    sys.exit(
                        f"no fixture corpus at {corpus} — a rule cannot be called dead without it"
                    )
            rule_ids = [r.id for r in rules]
            corpus_fired = {
                apply(email, rules).rule_id
                for corpus in args.corpus
                for email in load_corpus(corpus)
            }
    print(json.dumps({"rules_source": rules_source,
                      **rule_stats(report, rule_ids, corpus_fired)},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
