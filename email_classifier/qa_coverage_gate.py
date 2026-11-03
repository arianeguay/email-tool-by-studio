"""The reads `coverage-reader` must keep returning (STU-1151).

STU-1140's whole claim is a pair: the defect run flags, the post-fix control on the same
conversation does not. Both halves were Studio journals under `.studio/runs/`, which is
gitignored and pruned at 30 days with no protection reaching it — the shelf is age-only,
its names carrying Studio's run id rather than ours. So the evidence behind
`make qa-coverage` would have left the only machine that had it around 2026-09-29, and
"does the coverage read still separate these two runs" would have become unaskable on
every checkout. That is STU-1078's failure one harness over, so this is STU-1078's answer
one harness over.

`eval/draft-coverage-cases.json` carries, per entry, the sender's message, the passes the
group made, the verdict expected of the shipped body and the reader rule it exercises;
`make qa-coverage-gate` replays them all and fails on anything that comes back otherwise.
Committable for the reason `eval/draft-qa-rejections.json` is: the mail is de-identified
and a draft the club never sent names nobody.

Not a golden, and there is no re-freeze target. An entry states what the read must say,
so a run that starts saying something else is the finding — a target that could re-freeze
would be the mechanism for erasing it.

The control entry carries as much as the defect one, and is why `expected_verdict` may say
either here where `qa_gate`'s may only say `rejected`. A corpus of flagging cases alone
passes trivially under a reader that flags everything, and this reader is one rule away
from being that: whether a plain « l'information n'est pas publiée » *addresses* a request
is the whole of what keeps an honest club reply from flagging, and nothing but the control
entry asks about it.

That the gate can fail was measured, not assumed, on 2026-08-31 — and the measurement
corrected which edit the entry catches. Deleting the prompt's « not published addresses
it » bullet leaves the control reading `covered` 2/2: the bullet illustrates a rule stated
above it (« addressed when the reply says something about it », « unaddressed when the
reply never mentions it at all »), and this body mentions the request outright, so the
general rule carries it alone. Replacing the bullet with its negation flips the control to
`incomplete` 2/2 and the run exits 1. So the entry guards the rule the issue named, and
deleting one sentence is not how that rule leaves the prompt — the same shape STU-1078
measured for the reviewer, where removing a skill left the rejection standing and the
prose gate was what held it.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from email_classifier import prompt_sources, qa_provider, qa_studio_version
from email_classifier.qa_coverage import COVERED, INCOMPLETE, Group, Pass, Read

EXPECTED = (COVERED, INCOMPLETE)


@dataclass(frozen=True)
class Case:
    """One frozen group, and the reader rule its shipped body is here to exercise."""

    id: str
    gate: str
    source: str
    expected: str
    group: Group


def load_corpus(path: Path) -> list[Case]:
    """The frozen cases, refused rather than trusted when malformed.

    Checked before the first call, the way `qa_gate.load_corpus` is: every entry costs a
    live reader pass per repeat, so an entry the harness cannot drive should say so for
    free rather than after the bill.

    The entry's id stands in for the thread id the journal carried. Both journals are the
    same conversation, so the real handle would name two entries the same in every table
    and record this harness prints — and the reader never reads the field.
    """
    entries = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        Case(
            id=entry["id"],
            gate=entry["gate"],
            source=entry["source"],
            expected=entry["expected_verdict"],
            group=Group(
                entry["id"],
                {"body_text": entry["body_text"]},
                tuple(
                    Pass(p["draft_body"], p["verdict"], p.get("critique", ""),
                         tuple(p.get("issues") or ()))
                    for p in entry["passes"]
                ),
            ),
        )
        for entry in entries
    ]

    problems = []
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            problems.append(f"{case.id}: duplicate id")
        seen.add(case.id)
        if case.expected not in EXPECTED:
            problems.append(
                f"{case.id}: expected_verdict is {case.expected!r}, and a read can only "
                f"return {' or '.join(EXPECTED)}"
            )
        if not case.gate.strip():
            problems.append(f"{case.id}: no gate named")
        if not case.group.body_text.strip():
            problems.append(f"{case.id}: no sender message, which reads back covered")
        if not case.group.approved:
            last = case.group.passes[-1].verdict if case.group.passes else "(no pass)"
            problems.append(
                f"{case.id}: the last pass is {last!r}, so this entry froze a group that "
                f"shipped no body"
            )
    if problems:
        raise ValueError("corpus entries the harness cannot drive: " + "; ".join(problems))
    return cases


@dataclass(frozen=True)
class Outcome:
    """One case, and every read taken of the body it froze."""

    case: Case
    reads: tuple[Read, ...]

    @property
    def verdicts(self) -> tuple[str, ...]:
        return tuple(r.verdict for r in self.reads)

    @property
    def held(self) -> bool:
        """Every read returned what the entry expects.

        Every, not most: a read that came back the other way is the finding whichever
        side of it the majority fell on, and a gate that takes the vote is a gate that
        can be half off without saying so. A non-verdict — a dead provider, a reader that
        contradicted itself — is a failure and not a skip, for the reason `qa_gate` counts
        one: a gate that could not be asked did not fire (STU-912).
        """
        return bool(self.reads) and all(v == self.case.expected for v in self.verdicts)

    @property
    def unreadable(self) -> bool:
        """No read of this case landed at all — every one came back `error` or
        `incoherent` (STU-1254).

        Distinct from `held` being false: a case that came back the other way is a real
        regression, but a case nothing ever answered is not one — there is no read to
        have moved. Read off `Read.unreadable`, the same distinction `qa_coverage`
        already draws, rather than restated against the raw verdict strings.
        """
        return bool(self.reads) and all(r.unreadable for r in self.reads)

    @property
    def unaddressed(self) -> tuple[str, ...]:
        """What the first read that flagged called unaddressed, for the human reading a
        failure — on a held defect entry it is the request the corpus is about, and on a
        control entry that stopped holding it is the sentence the reader stopped counting."""
        for read in self.reads:
            if read.unaddressed:
                return read.unaddressed
        return ()


def failures(outcomes: Iterable[Outcome]) -> list[Outcome]:
    return [o for o in outcomes if not o.held]


def unreadable(outcomes: Iterable[Outcome]) -> list[Outcome]:
    return [o for o in outcomes if o.unreadable]


def render(outcomes: list[Outcome], provider: str | None,
           studio_version: str | None) -> str:
    failed = failures(outcomes)
    dead = unreadable(outcomes)
    if dead and len(dead) == len(outcomes):
        # Nothing here read the way it was frozen because nothing was read: the header
        # `qa_gate` and this module otherwise print is an assertion about the reader, and
        # that assertion is not true of a run every one of whose calls came back
        # unreadable (STU-1254).
        lines = [
            f"=== Coverage gate: no read landed for any of the {len(outcomes)} case(s) "
            "— check the provider before reading this as a moved verdict ==="
        ]
    else:
        lines = [
            f"=== Coverage gate: {len(outcomes) - len(failed)}/{len(outcomes)} still "
            "read the way they were frozen ==="
        ]
    for outcome in outcomes:
        case = outcome.case
        if outcome.unreadable:
            mark = "UNREADABLE"
        elif outcome.held:
            mark = "ok"
        else:
            mark = "READS NO LONGER"
        lines.append(
            f"  [{mark}] {case.id}: expected {case.expected}, got "
            f"{', '.join(outcome.verdicts) or '(no read)'}"
        )
        if outcome.unaddressed:
            lines.append(f"      unaddressed: {'; '.join(outcome.unaddressed)}")
        if not outcome.held:
            lines.append(f"      gate: {case.gate}")
            lines.append(f"      from: {case.source}")
            chars = " -> ".join(str(c) for c in case.group.answer_chars)
            removal = "  (converged by removal)" if case.group.converged_by_removal else ""
            lines.append(f"      passes: {len(case.group.passes)} | "
                         f"answer chars: {chars}{removal}")
    moved = [o for o in failed if not o.unreadable]
    if moved:
        lines.append(
            "A frozen read that moved is the finding. Read the entry's gate before "
            "deciding whether the reader or the corpus is what changed."
        )
    if dead:
        lines.append(
            f"{len(dead)} of {len(outcomes)} case(s) got no read at all: every call "
            "came back unreadable. That is not a frozen read that moved — nothing asked "
            "the reader anything, so check the provider (the line below names it) "
            "before spending a replay on the corpus."
        )
    lines.append(qa_provider.line(provider))
    lines.append(qa_studio_version.line(studio_version))
    return "\n".join(lines)


def as_dict(outcomes: list[Outcome], repeats: int, provider: str | None,
            studio_version: str | None) -> dict:
    return {
        qa_provider.KEY: provider,
        qa_studio_version.KEY: studio_version,
        "repeats": repeats,
        "cases": len(outcomes),
        "failures": len(failures(outcomes)),
        "unreadable": len(unreadable(outcomes)),
        "outcomes": [
            {
                "id": o.case.id,
                "gate": o.case.gate,
                "expected": o.case.expected,
                "verdicts": list(o.verdicts),
                "held": o.held,
                "unreadable": o.unreadable,
                "unaddressed": list(o.unaddressed),
            }
            for o in outcomes
        ],
    }


_READER_AGENT = "coverage-reader"

# What the reader reads when this corpus is replayed. Short because the agent declares
# no skills and no tools, which is deliberate (STU-1140: grounding is
# `draft-reviewer`'s job) — so there is no skill file and no tool YAML today, and a leg
# gained is read straight off the agent's own declaration (STU-1458) rather than a
# silent guard someone would have to remember to update.
_EXPLICIT_SOURCES = {
    "agent": f".studio/agents/{_READER_AGENT}.agent.yaml",
    "contract:draft-coverage": ".studio/contracts/draft-coverage.contract.yaml",
    "pipeline": ".studio/pipelines/draft-coverage.pipeline.yaml",
    "invariants": ".studio/invariants.md",
}


def sources(repo_root: Path) -> dict[str, str]:
    """Role -> path, relative to `repo_root`, of everything a replay reads."""
    studio_dir = repo_root / ".studio"
    declared = prompt_sources.skill_paths(studio_dir, _READER_AGENT) \
        | prompt_sources.tool_paths(studio_dir, _READER_AGENT)
    return _EXPLICIT_SOURCES | {
        role: f".studio/{relative}" for role, relative in declared.items()
    }

# The corpus is a leg like the rest, but named rather than pathed, for `qa_gate`'s
# reason: `--corpus` can point anywhere, and a leg keyed by where the file sat would read
# as a different question every time somebody passed an absolute path to the same corpus.
CORPUS_LEG = "corpus"

DIGEST_KEY = "digest"

RECORD_UNRECORDED = 2
RECORD_MOVED = 3
# A code of its own, by the rule that gave each of the others one: a run where every
# case's every read came back unreadable did not read a frozen case the other way, and
# folding that into the generic non-zero exit is how a dead provider gets misread as the
# regression this gate exists to catch (STU-1254). Checked before the generic failure
# count in `scripts/qa_coverage_gate.py`'s live run, the same order `qa_gate.py` checks
# `TOOL_UNREACHABLE` in — a run that answered nothing is disqualified before its
# per-case verdicts are read at all. Only fires when *every* case went unread: a single
# case's calls all dying beside others that answered is a real per-case finding, not the
# whole-run condition this names.
NOTHING_READ = 4
# A record answers for its own provenance, and one a `git merge` assembled has none —
# STU-1153's finding, which reaches this record the same way it reached the reviewer's.
# Two branches editing different reader sources each re-stamp on their own tree; git then
# merges the two `sources` maps cleanly, and every leg of the result matches the merged
# tree, so the check agrees about a reader no run ever replayed. The conflicting form is
# the lucky one: it stops a human.
RECORD_ASSEMBLED = 6


def fingerprint(repo_root: Path, corpus: Path) -> dict[str, str]:
    """A digest per file, keyed by its path — not one digest over all of them.

    Which file moved is the whole of what a reader needs: an entry added to the corpus
    and an edit to the reader's prompt both mean "replay it", but only the second means
    the reader's judgement is what changed. A missing file digests as absent rather than
    raising, so a source deleted out from under the gate reads as a leg that moved
    instead of a crash with nothing recorded.
    """
    legs = {name: repo_root / relative for name, relative in sources(repo_root).items()} \
        | {CORPUS_LEG: corpus}
    return {
        name: (
            hashlib.sha256(path.read_bytes()).hexdigest()[:12]
            if path.exists()
            else "absent"
        )
        for name, path in legs.items()
    }


def self_digest(record: dict) -> str:
    """A digest binding the whole record body, so a merge cannot restate part of it.

    Everything but the digest, rather than `sources` and `run_id` alone: every field is
    written by the same run, so binding them all is both stronger and shorter than a list
    of which ones count — and a merge stitching `read` from one run onto `sources` from
    another fabricates just as much. Keys are sorted, because re-ordering the file asserts
    nothing; content is the claim.
    """
    body = {name: value for name, value in record.items() if name != DIGEST_KEY}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _measured_on(recorded: dict) -> str:
    """How the check names the provider of the run it is reading back (STU-1175).

    Reported, never compared, for the reason `qa_gate._measured_on` is: the check
    answers whether the tree moved, and a provider that differs from this host's is not
    a tree that moved. A record stamped before STU-1175 carries none and says so.

    The Studio version rides beside it for the same reason (STU-1241): reported, never
    compared, and a record stamped before the field existed says so rather than reading
    as measured here.
    """
    provider = recorded.get(qa_provider.KEY)
    provider_part = f"measured on `{provider}`" if provider else "provider unrecorded"
    version = recorded.get(qa_studio_version.KEY)
    version_part = f"studio `{version}`" if version else "studio version unrecorded"
    return f"{provider_part}, {version_part}"


def check(today: dict[str, str], record_path: Path) -> int:
    """Whether the last recorded pass was measured on the reader there is now.

    The half of this gate a push can run (STU-1156). The gate itself is `claude:local` —
    one live call per entry per repeat — so nothing at push time can replay it; what a
    push can ask is whether it still needs replaying. A missing record is not agreement,
    so it has its own code: a tree that never recorded a pass is indistinguishable from
    one whose record still holds.
    """
    recorded = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    previous = recorded.get("sources")
    if not previous:
        print(f"no sources recorded in {record_path}; nothing says what the gate last read against")
        return RECORD_UNRECORDED
    stamped = recorded.get(DIGEST_KEY)
    if stamped != self_digest(recorded):
        # Which of the two forms was seen, the way STU-1080's cache violation names which
        # zero-hit it measured: an absent digest and a broken one are the same verdict
        # reached from opposite directions, and a reader chasing the wrong one looks for
        # the wrong edit.
        print(f"{record_path}: " + (
            "carries no digest, so it cannot answer for where it came from"
            if stamped is None
            else "does not match its own digest; this record was assembled, not measured"
        ))
        print("A record whose fields come from two runs describes the reader neither of "
              "them replayed. Run `make qa-coverage-gate` — resolving it by hand cannot "
              "make it true.")
        return RECORD_ASSEMBLED
    moved = sorted(
        leg for leg in set(previous) | set(today)
        if previous.get(leg, "absent") != today.get(leg, "absent")
    )
    if not moved:
        print(f"{record_path}: reader unchanged since the gate of "
              f"{recorded.get('date')}, {_measured_on(recorded)}")
        return 0
    print(f"{record_path}: reader moved since the gate of "
          f"{recorded.get('date')}, {_measured_on(recorded)}:")
    for leg in moved:
        print(f"  {leg}: recorded {previous.get(leg, 'absent')}, today {today.get(leg, 'absent')}")
    print(f"{recorded.get('cases')} case(s) last read the way they were frozen; run "
          "`make qa-coverage-gate` — nothing else says they still do")
    return RECORD_MOVED


def as_record(outcomes: list[Outcome], sources: dict[str, str], run_id: str,
              date: str, provider: str | None, studio_version: str | None) -> dict:
    """What a passing gate leaves for every other tree to check itself against.

    Written only on a pass, by STU-1078's rule. A record of a run that found a frozen
    read moved would state that these sources were verified, which is the one thing it
    must never say — and leaving the stale record in place is what keeps
    `make qa-coverage-gate-check` failing until somebody actually replays them.

    The provider joins the body the digest covers, for the reason `qa_gate.as_record`
    carries it (STU-1175): a merge cannot then stitch one run's provider onto another
    run's sources. Not a `sources` leg, by the same argument — the legs are what a tree
    rebuilds, and the provider is a property of the run, so as a leg every host
    difference would be a red push no tree could repair. The Studio version joins it
    for the same reason (STU-1241).
    """
    record = {
        "date": date,
        "run_id": run_id,
        qa_provider.KEY: provider,
        qa_studio_version.KEY: studio_version,
        "cases": len(outcomes),
        "read": [o.case.id for o in outcomes],
        "sources": sources,
    }
    return record | {DIGEST_KEY: self_digest(record)}
