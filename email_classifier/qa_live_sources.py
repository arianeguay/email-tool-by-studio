"""Can a live source flip a verdict `make qa-gate` freezes? (STU-1114)

`qa_gate.SOURCES` fingerprints the reviewer's own declarations — its agent YAML, its two
skills, two contracts, the replay pipeline, three tool YAMLs and `invariants.md`. What
`make qa-gate-check` publishes on the strength of that is whether the frozen rejections
still stand. They are not the same question: during a replay the reviewer really calls
its tools, so the club's live pages and the mailbox's own sent mail are inputs to the
text a verdict is reached on, and no leg covers either. A change there leaves the check
green, and STU-1104 had to re-run its measurement after a merge moved `anonymize.py`
precisely because nothing recorded could say whether the reviewer still read the same
thing.

So: replay each frozen case twice over, once with the tools live and once with them
answering nothing, and compare. A case that rejects both ways is judged on its prose,
and CLAUDE.md's sentence — *« il n'y en a pas : la seule entrée se juge sur sa prose »*,
written when the corpus held one entry — holds for it. A case whose verdict moves names
the entry and the source it turns on.

**Answering nothing is not being unreachable**, and keeping the two apart is the whole
design. STU-1139 fails the gate when a tool raised on every call, because a reviewer
missing a source is not the reviewer production ships. This measurement wants that same
reviewer with its sources *present and empty* — `matches: []`, the answer each tool
defines for "the words are not there". So the silenced arm answers through the real
search function with nothing behind it, exits 0, and is recorded as reachable; a tool
that actually raised still shows up as unreachable and disqualifies the run.

Two tools, not three. `org_web-search_site` and `mail_precedents-search_mail` each have
an empty answer they define. `org_memory-read_memory` has none — a name not on disk
raises, which is the unreachable case STU-1139 already asks about — and the corpus does
not exercise it anyway: `memory_index` is empty in all four entries, so the reviewer's
own rule (« when none does, ignore them ») means it is never called.
"""

from dataclasses import dataclass
from pathlib import Path

from email_classifier import precedents, qa_provider, qa_studio_version, websearch
from email_classifier.qa_replay import ERROR

REJECTED = "rejected"

LIVE = "live"
SILENT = "silent"

# What the shim reads to know which modules to answer with nothing, and which real
# interpreter to hand everything else to. Comma-separated, so one arm can silence a
# subset once a case is found that turns on one of them.
SILENCED_ENV = "EMAIL_CLASSIFIER_QA_SILENCED"
REAL_PYTHON_ENV = "EMAIL_CLASSIFIER_QA_REAL_PYTHON"

WEBSEARCH = "email_classifier.websearch"
PRECEDENTS = "email_classifier.precedents"

# The module behind each silenceable tool, so a tool the journal names can be read
# against what the shim answered. `org_memory-read_memory` is deliberately absent, for
# the reason the docstring gives: it has no empty answer to return.
SILENCED_TOOLS = {
    "org_web-search_site": WEBSEARCH,
    "mail_precedents-search_mail": PRECEDENTS,
}
SILENCEABLE = tuple(SILENCED_TOOLS.values())


class _NoMail:
    """A mailbox with nothing in it, for `precedents.search` to find nothing in."""

    def search_messages(self, query: str, limit: int) -> list:
        return []


def empty_answer(module: str, query: str, sources: Path) -> dict:
    """The answer a silenced tool returns: its own shape, with no source behind it.

    Driven through the real search function rather than written out here, so the payload
    cannot drift from the one the reviewer reads on a live run — a hand-copied empty
    shape would go on looking right after the tool gained a field.

    The site is silenced by handing the fetcher empty documents, not by refusing to
    fetch: a refusal lands in `errors`, which the reviewer's prompt reads as a page it
    could not reach, and that is the other question. `pages_searched` stays the real
    allow-list for the same reason — the arm says the pages hold nothing, not that there
    are no pages.
    """
    if module == WEBSEARCH:
        return websearch.search(query, websearch.load_pages(sources), fetcher=lambda url: "")
    if module == PRECEDENTS:
        return precedents.search(query, _NoMail())
    raise ValueError(f"no empty answer defined for {module}")


@dataclass(frozen=True)
class Arm:
    """One case judged `repeats` times under one source condition."""

    name: str
    verdicts: tuple[str, ...]
    called: tuple[str, ...] = ()
    unreachable: tuple[str, ...] = ()

    @property
    def settled(self) -> str | None:
        """The verdict every repeat returned, or None when they disagreed.

        The reviewer is not deterministic — STU-1014 put its floor at 21 of 22 first
        attempts judging the same drafts twice — so an arm that did not agree with
        itself cannot be one side of a comparison. Unsettled says so instead of
        picking a repeat.
        """
        unique = set(self.verdicts)
        return self.verdicts[0] if len(unique) == 1 and self.verdicts else None

    @property
    def unreadable(self) -> bool:
        return not self.verdicts or ERROR in self.verdicts


@dataclass(frozen=True)
class CaseResult:
    """One frozen case under both source conditions."""

    case_id: str
    gate: str
    live: Arm
    silent: Arm

    @property
    def diverged(self) -> bool:
        """Both arms settled, on different verdicts."""
        return (
            self.live.settled is not None
            and self.silent.settled is not None
            and self.live.settled != self.silent.settled
        )

    @property
    def candidates(self) -> tuple[str, ...]:
        """The silenced tools the live arm actually got an answer out of.

        This is what names the source behind a divergence without paying for an arm per
        tool: a case that called one of them has one candidate, and a case that called
        both says so rather than picking. A tool nobody called cannot be what moved a
        verdict — silencing it changed nothing that reached the reviewer.
        """
        return tuple(
            t for t in self.live.called
            if t in SILENCED_TOOLS and t not in self.live.unreachable
        )

    @property
    def silenced_reached(self) -> tuple[str, ...]:
        """Silenceable tools the silent arm called, and so had answered with nothing.

        Reported rather than assumed: the reviewer decides for itself whether to search,
        and an arm where it declined is not an arm where the silencing was exercised. It
        reaches the same conclusion — no live source contributed either way — by a
        different route, and a reader deciding what the number covers needs to know
        which one this was.
        """
        return tuple(t for t in self.silent.called if t in SILENCED_TOOLS)

    @property
    def finding(self) -> str:
        """One line, in the terms the issue asks to record."""
        if self.live.unreachable or self.silent.unreachable:
            missing = sorted(set(self.live.unreachable) | set(self.silent.unreachable))
            return (
                f"UNVERIFIED — a tool raised on every call ({', '.join(missing)}), so "
                "neither arm is the reviewer production runs"
            )
        if self.live.unreadable or self.silent.unreadable:
            return "UNREADABLE — an arm returned no verdict; the reviewer did not judge"
        for arm in (self.live, self.silent):
            if arm.settled is None:
                return (
                    f"UNSETTLED — the {arm.name} arm returned "
                    f"{' and '.join(sorted(set(arm.verdicts)))} across "
                    f"{len(arm.verdicts)} repeats, which is inside the noise floor; "
                    "no comparison"
                )
        if self.diverged:
            source = ", ".join(self.candidates) or "no tool the live arm called"
            return (
                f"LIVE SOURCE — {self.live.settled} live, {self.silent.settled} "
                f"silenced; the live arm was answered by {source}"
            )
        if self.live.settled != REJECTED:
            return (
                f"GATE FAILURE — settled {self.live.settled} both ways; this is "
                "`make qa-gate` failing, not a source dependency"
            )
        if self.silenced_reached:
            how = f"{', '.join(self.silenced_reached)} answered nothing"
        else:
            how = "the silenced arm's reviewer searched nothing at all"
        return f"prose — rejects either way; {how}"

    @property
    def on_prose(self) -> bool:
        """The finding CLAUDE.md's sentence claims: rejected either way, both settled."""
        return (
            not self.diverged
            and self.live.settled == REJECTED
            and self.silent.settled == REJECTED
            and not self.live.unreachable
            and not self.silent.unreachable
        )


def diverged(results: list[CaseResult]) -> list[CaseResult]:
    return [r for r in results if r.diverged]


def render(results: list[CaseResult], repeats: int, provider: str | None,
           studio_version: str | None) -> str:
    """The table the issue asks to be recorded: verdict live, verdict silenced, per case."""
    on_prose = [r for r in results if r.on_prose]
    lines = [
        f"=== QA live sources: {len(on_prose)}/{len(results)} case(s) judged on prose "
        f"alone, {repeats} repeat(s) per arm ==="
    ]
    for result in results:
        lines.append(f"  {result.case_id}")
        lines.append(f"    gate: {result.gate}")
        for arm in (result.live, result.silent):
            verdicts = ", ".join(arm.verdicts) if arm.verdicts else "(no verdict)"
            lines.append(f"    {arm.name:<7} {verdicts}")
            if arm.called:
                lines.append(f"            called: {', '.join(arm.called)}")
            if arm.unreachable:
                lines.append(f"            unreachable: {', '.join(arm.unreachable)}")
        lines.append(f"    {result.finding}")
    if diverged(results):
        lines.append(
            "A divergence is a case whose gate rests on a source no fingerprint covers. "
            "Read it before deciding whether the leg grows or the entry is reworked."
        )
    elif len(on_prose) == len(results):
        lines.append(
            "Every case rejects with its live sources answering nothing, so each is "
            "judged on its prose and the fingerprint covers what decides it."
        )
    unexercised = [r.case_id for r in results if r.on_prose and not r.silenced_reached]
    if unexercised:
        lines.append(
            f"The silenced arm searched nothing at all in {', '.join(unexercised)}: no "
            "live source reached that reviewer either, so the reading holds — but it is "
            "a reviewer that declined to search, not one the shim answered."
        )
    lines.append(qa_provider.line(provider))
    lines.append(qa_studio_version.line(studio_version))
    return "\n".join(lines)


def as_dict(results: list[CaseResult], repeats: int, provider: str | None,
            studio_version: str | None) -> dict:
    return {
        qa_provider.KEY: provider,
        qa_studio_version.KEY: studio_version,
        "repeats": repeats,
        "cases": len(results),
        "diverged": len(diverged(results)),
        "on_prose": sum(1 for r in results if r.on_prose),
        "results": [
            {
                "id": r.case_id,
                "gate": r.gate,
                "finding": r.finding,
                "diverged": r.diverged,
                "on_prose": r.on_prose,
                "candidates": list(r.candidates),
                "silenced_reached": list(r.silenced_reached),
                "arms": {
                    arm.name: {
                        "verdicts": list(arm.verdicts),
                        "settled": arm.settled,
                        "called": list(arm.called),
                        "unreachable": list(arm.unreachable),
                    }
                    for arm in (r.live, r.silent)
                },
            }
            for r in results
        ],
    }
