"""The drafts `draft-reviewer` must keep rejecting (STU-1078).

STU-1070 fixed a reviewer that approved a draft claiming « j'ai transféré votre message »
when nothing forwards anything, and verified the fix by replaying that exact draft through
`draft-qa-replay`. The verification was not repeatable: the run journal holding the draft
was on the staging host, the source thread came out of a gitignored dump on a 30-day
shelf, and the input lived in a scratchpad. A gate whose regression case exists in one
terminal is a gate that can silently stop firing — which is, one level up, the failure
STU-1070 was filed for.

So the case is frozen instead. `eval/draft-qa-rejections.json` carries, per entry, the
fan-out item, the draft, the verdict expected of it, and the gate it exercises; `make
qa-gate` replays them all and fails on anything that is not `rejected`. Committable for
the reason `eval/claiming-corpus.json` is: the mail is de-identified and a draft the club
never sent names nobody.

Not a golden. `make eval-spans-seed` re-freezes what the detector returns, because there
the current answer is the specification. Here an entry states that a draft is bad, so a
run that starts approving one is the finding — there is nothing to re-seed, and a target
that could would be the mechanism for erasing the evidence.

That the gate can fail was measured, not assumed, on 2026-08-29: the reviewer of
`b5e324b^` — its state the night it approved this draft — approves it again here, and the
one on `main` rejects it. Which half of STU-1070's fix that answers for is worth writing
down, because the issue guessed the other one: removing `club-voice` from the reviewer's
skills alone still rejects. What holds is the prose gate the same commit rewrote, which
names the past-tense claim outright. The skill is what the reviewer measures voice
against; the sentence is what rejects this draft.

The corpus grows by the rule that made the first entry necessary: a gate's case is added
the day that gate is written or repaired.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from email_classifier import prompt_sources, qa_provider, qa_studio_version
from email_classifier.qa_replay import ERROR
from email_classifier.stage1.policy import load_policy

REJECTED = "rejected"


@dataclass(frozen=True)
class Case:
    """One known-bad draft, and the reviewer rule it is here to exercise."""

    id: str
    gate: str
    source: str
    item: dict
    draft: dict


def load_corpus(path: Path, required_draft_fields: Iterable[str]) -> list[Case]:
    """The frozen cases, refused rather than trusted when malformed.

    Checked before the first call, the way `classify_bakeoff.coverage` is: every entry
    costs a live reviewer pass, so an entry the harness cannot drive should say so for
    free rather than after the bill.

    `expected_verdict` may only say `rejected`. The field is there because an entry
    should state its own expectation rather than inherit it from the file's name, but a
    corpus of drafts that must be *approved* is a different artifact with a different
    failure mode — it would re-freeze an approval, which is the one thing this must not
    become.
    """
    entries = json.loads(path.read_text(encoding="utf-8"))
    cases = [
        Case(
            id=entry["id"],
            gate=entry["gate"],
            source=entry["source"],
            item=entry["item"],
            draft=entry["draft"],
        )
        for entry in entries
    ]

    problems = []
    seen: set[str] = set()
    for entry, case in zip(entries, cases):
        if case.id in seen:
            problems.append(f"{case.id}: duplicate id")
        seen.add(case.id)
        if entry["expected_verdict"] != REJECTED:
            problems.append(
                f"{case.id}: expected_verdict is {entry['expected_verdict']!r}, and this "
                f"corpus only holds drafts a gate must reject"
            )
        if not case.gate.strip():
            problems.append(f"{case.id}: no gate named")
        missing = [f for f in required_draft_fields if f not in case.draft]
        if missing:
            problems.append(f"{case.id}: draft is missing {', '.join(missing)}")
    if problems:
        raise ValueError("corpus entries the harness cannot drive: " + "; ".join(problems))
    return cases


@dataclass(frozen=True)
class Outcome:
    case: Case
    verdict: str
    unreachable: tuple[str, ...] = ()
    # A tool that raised on every call because its store is verifiably empty in this
    # tree (STU-1235): excused, never folded into `unreachable`, and never disqualifies
    # `verified` — nothing was lost, so there is nothing here for a gate to refuse on.
    empty_store: tuple[str, ...] = ()

    @property
    def held(self) -> bool:
        """`ERROR` is a failure, not a skip: a gate that could not be asked did not fire,
        and counting it as held is exactly the silence this corpus exists against."""
        return self.verdict == REJECTED

    @property
    def verified(self) -> bool:
        """Whether the reviewer that judged is the one production runs (STU-1139).

        Held and verified are two statements and only both together are a pass. What
        the record claims is that *production* still rejects these; production's
        reviewer has three tools, and a run where one of them raised on every call had
        two. Its `rejected` is then a verdict about a reviewer nobody ships, which is
        STU-912's rule one stage over — a check family that did not run must not read
        as a check family that passed.
        """
        return not self.unreachable


def failures(outcomes: Iterable[Outcome]) -> list[Outcome]:
    return [o for o in outcomes if not o.held]


def unverified(outcomes: Iterable[Outcome]) -> list[Outcome]:
    return [o for o in outcomes if not o.verified]


def render(outcomes: list[Outcome], provider: str | None,
           studio_version: str | None) -> str:
    failed = failures(outcomes)
    lines = [
        f"=== QA gate: {len(outcomes) - len(failed)}/{len(outcomes)} still rejected ==="
    ]
    for outcome in outcomes:
        mark = "ok" if outcome.held else "HELD NO LONGER"
        lines.append(f"  [{mark}] {outcome.case.id}: {outcome.verdict}")
        if not outcome.held:
            lines.append(f"      gate: {outcome.case.gate}")
            lines.append(f"      from: {outcome.case.source}")
        if outcome.unreachable:
            lines.append(f"      unreachable: {', '.join(outcome.unreachable)}")
        if outcome.empty_store:
            lines.append(
                f"      asked an empty store: {', '.join(outcome.empty_store)} "
                "(not a lost source)"
            )
    if any(o.verdict == ERROR for o in failed):
        lines.append("A verdict could not be read — the reviewer did not judge, "
                     "which is not the same as approving.")
    missing = sorted({tool for o in unverified(outcomes) for tool in o.unreachable})
    if missing:
        lines.append(
            f"{len(unverified(outcomes))} of {len(outcomes)} case(s) were judged by a "
            f"reviewer that never reached {', '.join(missing)}. A verdict from a "
            "reviewer production does not have is not a pass, whichever way it went."
        )
    lines.append(qa_provider.line(provider))
    lines.append(qa_studio_version.line(studio_version))
    return "\n".join(lines)


def as_dict(outcomes: list[Outcome], provider: str | None,
            studio_version: str | None) -> dict:
    return {
        qa_provider.KEY: provider,
        qa_studio_version.KEY: studio_version,
        "cases": len(outcomes),
        "failures": len(failures(outcomes)),
        "unverified": len(unverified(outcomes)),
        "outcomes": [
            {"id": o.case.id, "gate": o.case.gate, "verdict": o.verdict, "held": o.held,
             "unreachable": list(o.unreachable), "empty_store": list(o.empty_store)}
            for o in outcomes
        ],
    }


_REVIEWER_AGENT = "draft-reviewer"

# What the reviewer reads when this corpus is replayed, besides its own declared skills
# and tools — read off `_REVIEWER_AGENT`'s YAML rather than re-listed here (STU-1458).
# Not `drafts._PROMPT_SOURCES`, which is wrong here in both directions: it carries the
# drafter, whose files the replay never loads, so editing them would bill a live Opus
# call to re-answer a question that did not move; and it misses the replay pipeline and
# the recorded draft's contract, which the replay does run.
_EXPLICIT_SOURCES = {
    "agent": f".studio/agents/{_REVIEWER_AGENT}.agent.yaml",
    "contract:draft-qa": ".studio/contracts/draft-qa.contract.yaml",
    "contract:qa-replay-draft": ".studio/contracts/qa-replay-draft.contract.yaml",
    "pipeline": ".studio/pipelines/draft-qa-replay.pipeline.yaml",
    "invariants": ".studio/invariants.md",
}


def sources(repo_root: Path) -> dict[str, str]:
    """Role -> path, relative to `repo_root`, of everything a replay reads.

    Skills and tools are read off the reviewer's own declaration rather than re-listed
    (STU-1458): `tests/test_qa_gate.py::test_the_reviewers_agent_skills_and_tools_are_all_fingerprinted`
    guards that a skill or tool it gains is covered here without a Python edit.
    """
    studio_dir = repo_root / ".studio"
    declared = prompt_sources.skill_paths(studio_dir, _REVIEWER_AGENT) \
        | prompt_sources.tool_paths(studio_dir, _REVIEWER_AGENT)
    return _EXPLICIT_SOURCES | {
        role: f".studio/{relative}" for role, relative in declared.items()
    }

# The corpus is a leg like the rest, but named rather than pathed: `--corpus` can point
# anywhere, and a leg keyed by where the file sat would read as a different question every
# time somebody passed an absolute path to the same corpus.
CORPUS_LEG = "corpus"

DIGEST_KEY = "digest"

RECORD_UNRECORDED = 2
RECORD_MOVED = 3
# A record answers for its own provenance, and one a `git merge` assembled has none
# (STU-1153). Two branches editing different reviewer sources each re-stamp on their own
# tree; git then merges the two `sources` maps cleanly, and every leg of the result
# matches the merged tree — so the check agrees, about a reviewer no run ever replayed.
RECORD_ASSEMBLED = 6
# A code of its own, by the rule that gave each of the others one: a draft the gate must
# reject coming back approved and a reviewer that lost a tool are two findings with two
# repairs, and folding the second into the first is how it goes unnoticed behind it.
TOOL_UNREACHABLE = 4

# Printed by the red check, not only by the run that hits it: `make qa-gate` is the sole
# repair for a moved leg, and a machine without the grant answers it with exit 4 and no
# stamp — so the constraint has to be readable where the push goes red, one edit before
# anyone spends a reviewer pass finding out (STU-1146).
# What it names is how the precedent search reaches the mailbox, so it follows the
# deployment's `email.provider` (STU-2094): a Graph grant names nothing on IMAP.
_MAILBOX_ACCESS = {
    "outlook": "an app-only Graph grant (OUTLOOK_TENANT_ID naming the tenant and "
               "OUTLOOK_CLIENT_SECRET set)",
    "imap": "the IMAP login (IMAP_HOST, IMAP_USER and IMAP_PASSWORD set)",
}


def _email_provider(policy_path: Path | None) -> str:
    return load_policy(policy_path).email_provider if policy_path else "outlook"


def repair_needs(email_provider: str) -> str:
    access = _MAILBOX_ACCESS.get(email_provider, f"read access to the {email_provider} mailbox")
    return (
        f"`make qa-gate` needs {access} and the `ner` extra: the reviewer's "
        "`mail_precedents-search_mail` reads the mailbox, and without it the gate exits 4 "
        "and stamps nothing."
    )


# The corpus leg has a second repair, and only it does (STU-1166). `make qa-gate` asks
# every entry whether the reviewer still rejects it; it never asks the question
# `make qa-live-sources` asks — whether that rejection turns on the reviewer's prose or
# on a live source. So an entry added to the corpus widens the second measurement's
# claim on a tree the first turns green again, and the reader is at this message when
# they add it. Named here rather than held by a record of its own: STU-1114 declined to
# freeze "nothing diverged" under digests precisely because a green record reads as a
# reviewer somebody replayed.
CORPUS_GREW = (
    "The corpus leg moved: an entry added here has not been asked whether its rejection "
    "turns on a live source. Run `make qa-live-sources` too — `make qa-gate` does not "
    "ask that, and CLAUDE.md's answer names the entries it was measured on."
)


def fingerprint(repo_root: Path, corpus: Path) -> dict[str, str]:
    """A digest per file, keyed by its path — not one digest over all of them.

    Which file moved is the whole of what a reader needs: an edit to the reviewer's
    prose and an entry added to the corpus both mean "replay it", but only the first
    means the reviewer's judgement is what changed. A missing file digests as absent
    rather than raising, so a source deleted out from under the gate reads as a leg that
    moved instead of a crash with nothing recorded.
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
    of which ones count — and a merge stitching `held` from one run onto `sources` from
    another fabricates just as much. Keys are sorted, because re-ordering the file asserts
    nothing; content is the claim.
    """
    body = {name: value for name, value in record.items() if name != DIGEST_KEY}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _measured_on(recorded: dict) -> str:
    """How the check names the provider of the run it is reading back (STU-1175).

    Reported, never compared. The record says which provider produced the verdicts, and
    a reader chasing a contaminated measurement needs that; but the check answers
    whether the tree moved, and a provider that differs from this host's is not a tree
    that moved — there would be nothing here to repair it with.

    A record stamped before STU-1175 carries no provider, and says so rather than
    reading as one taken on this host: re-stamping one by hand is what the digest above
    refuses.

    The Studio version rides beside it for the same reason (STU-1241): reported here,
    never compared — a version older than this host's is not a tree that moved, and
    there would be nothing to repair it with.
    """
    provider = recorded.get(qa_provider.KEY)
    provider_part = f"measured on `{provider}`" if provider else "provider unrecorded"
    version = recorded.get(qa_studio_version.KEY)
    version_part = f"studio `{version}`" if version else "studio version unrecorded"
    return f"{provider_part}, {version_part}"


def check(today: dict[str, str], record_path: Path, policy_path: Path | None = None) -> int:
    """Whether the last recorded pass was measured on the reviewer there is now.

    A missing record is not agreement, so it has its own code: a file written before any
    of this existed is indistinguishable from one that still holds.

    The policy is read only to name the repair, so a tree with no record needs none.
    """
    recorded = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {}
    previous = recorded.get("sources")
    if not previous:
        print(f"no sources recorded in {record_path}; nothing says what the gate last held against")
        return RECORD_UNRECORDED
    stamped = recorded.get(DIGEST_KEY)
    if stamped != self_digest(recorded):
        # Which of the two forms was seen, the way STU-1080's cache violation names
        # which zero-hit it measured: an absent digest and a broken one are the same
        # verdict reached from opposite directions, and a reader chasing the wrong one
        # looks for the wrong edit.
        print(f"{record_path}: " + (
            "carries no digest, so it cannot answer for where it came from"
            if stamped is None
            else "does not match its own digest; this record was assembled, not measured"
        ))
        print("A record whose fields come from two runs describes the reviewer neither "
              "of them replayed. Run `make qa-gate` — resolving it by hand cannot "
              "make it true.")
        print(repair_needs(_email_provider(policy_path)))
        return RECORD_ASSEMBLED
    moved = sorted(
        leg for leg in set(previous) | set(today)
        if previous.get(leg, "absent") != today.get(leg, "absent")
    )
    if not moved:
        print(f"{record_path}: reviewer unchanged since the gate of "
              f"{recorded.get('date')}, {_measured_on(recorded)}")
        return 0
    print(f"{record_path}: reviewer moved since the gate of "
          f"{recorded.get('date')}, {_measured_on(recorded)}:")
    for leg in moved:
        print(f"  {leg}: recorded {previous.get(leg, 'absent')}, today {today.get(leg, 'absent')}")
    print(f"{recorded.get('cases')} case(s) last held; run `make qa-gate` — "
          "nothing else says they still do")
    if CORPUS_LEG in moved:
        print(CORPUS_GREW)
    print(repair_needs(_email_provider(policy_path)))
    return RECORD_MOVED


def as_record(outcomes: list[Outcome], sources: dict[str, str], run_id: str, date: str,
              provider: str | None, studio_version: str | None) -> dict:
    """What a passing gate leaves for every other tree to check itself against.

    Written only on a pass. A record of a run that found a draft approved would state
    that these sources were verified, which is the one thing it must never say — and
    leaving the stale record in place is what keeps `make qa-gate-check` failing until
    somebody actually replays them.

    The provider joins the body the digest covers (STU-1175), so a merge cannot stitch
    one run's provider onto another run's sources. It is deliberately not a `sources`
    leg: the legs are what a tree can rebuild and disagree about, and the provider is a
    property of the run — as a leg, every host difference would be a red push whose
    repair no tree can perform. The Studio version joins it for the same reason
    (STU-1241): a property of the run's host, not something a bare tree rebuilds.
    """
    record = {
        "date": date,
        "run_id": run_id,
        qa_provider.KEY: provider,
        qa_studio_version.KEY: studio_version,
        "cases": len(outcomes),
        "held": [o.case.id for o in outcomes],
        "sources": sources,
    }
    return record | {DIGEST_KEY: self_digest(record)}
