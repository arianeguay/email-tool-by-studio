"""Does the reviewer's gate hold on the group's later passes? (STU-1104)

`make qa-gate` replays each frozen known-bad draft as one cold stage, so it can only ever
answer for pass 1. STU-1101 is the case that made the rest of the group worth asking
about: a draft the reviewer rejects cold was approved in production after two rejections
of its own, and the critique it had authored on an earlier pass came back as the reason
to keep the sentence. If that generalises, every rule in `draft-reviewer.agent.yaml` is
weaker the longer a conversation is worked, and the drafts most likely to ship a defect
are the ones the group fought hardest over.

The measurement holds the draft still and lets only the feedback move. `draft-qa-erosion`
re-echoes the same recorded draft on every iteration, so the reviewer at pass K sees a
draft it has already judged plus its own pass-K-1 critique in `group_feedback` — and a
verdict that flips has one thing to attribute the flip to.

Two things this cannot see, both worth stating rather than discovering later:

- Production rewrites the draft between passes and this does not, so the reviewer is
  handed something that visibly ignored its last instruction. That pushes toward
  rejecting. A flip to `approved` is therefore strong evidence of erosion; a hold is
  weaker evidence against it than the table makes it look.
- Studio replaces `group_feedback` each iteration rather than accumulating it
  (`group-orchestrator.ts`), so pass K carries pass K-1's critique and an iteration
  counter — not K rounds of prose. What is under test is a reviewer re-served its own
  last words, which is exactly STU-1101's shape.

A third thing it can see, and now says: which of the reviewer's tools raised on every
call it made (STU-1147). This is the harness a missing source hurts most. A bake-off
diffs two arms, so a tool down for both is present on both sides and mostly cancels;
this asks one reviewer the same question K times, so a missing source is in every pass
and the "no flip" reading — a conclusion, not a comparison — is the one it manufactures.

The verdict is the run's and not a case's (STU-1161). A case sees only its own repeats,
so one flip in two of them is a draw against STU-1014's floor and says so; `RunReading`
is the only thing that sees five flips over twenty-four replays against a cold pass that
never once contradicted itself. Both lines are printed and the run's goes first.

Not a gate. Nothing here fails a build, and an unreachable tool does not change that:
`make qa-gate` exits 4 on one because it would otherwise *stamp* a record claiming
production still rejects, and a record is read later by trees that cannot re-derive it.
Nothing reads `logs/qa-erosion.json` as an assertion; a human reads the number the day
it is printed. So the tool goes in the number's own sentence instead — the wording is
the only place a skipped check can be told from a passed one where no exit code carries
it (STU-912). Failing here would only make the measurement unrunnable on the machine
that makes measurements, which is where STU-1146 leaves this laptop today.
"""

from dataclasses import dataclass
from statistics import mean

from email_classifier import qa_provider, qa_studio_version

APPROVED = "approved"
REJECTED = "rejected"


@dataclass(frozen=True)
class Pass:
    """One replay of one case: the verdict at each K the group reached."""

    case_id: str
    repeat: int
    verdicts: tuple[str, ...]
    unreachable: tuple[str, ...] = ()

    @property
    def flipped(self) -> bool:
        """The group ends on an approval, so an approval anywhere is the last entry."""
        return bool(self.verdicts) and self.verdicts[-1] == APPROVED

    @property
    def flip_k(self) -> int | None:
        """How many prior rejections it took, or None when the gate held.

        Zero would mean the reviewer approved cold, which is `make qa-gate` failing
        rather than erosion — reported as such rather than folded in.
        """
        return len(self.verdicts) - 1 if self.flipped else None

    @property
    def unreadable(self) -> bool:
        """No verdict at all: the reviewer did not judge, which is not a hold."""
        return not self.verdicts


@dataclass(frozen=True)
class CaseResult:
    """Every repeat of one case, and what they agree on."""

    case_id: str
    gate: str
    passes: tuple[Pass, ...]

    @property
    def flips(self) -> int:
        return sum(1 for p in self.passes if p.flipped)

    @property
    def unreadable(self) -> int:
        return sum(1 for p in self.passes if p.unreadable)

    @property
    def flip_ks(self) -> tuple[int, ...]:
        return tuple(p.flip_k for p in self.passes if p.flip_k is not None)

    @property
    def unreachable(self) -> tuple[str, ...]:
        """Every tool no repeat of this case could confirm the reviewer had.

        Pooled across repeats rather than reported per repeat: one repeat judged
        without a source is enough to put the case's reading in question, and which
        repeat it was answers nothing a reader would act on.
        """
        return tuple(sorted({tool for p in self.passes for tool in p.unreachable}))

    @property
    def held_cold(self) -> bool:
        """Pass 1 rejected in every repeat — what `make qa-gate` already asserts."""
        return all(p.verdicts and p.verdicts[0] == REJECTED for p in self.passes)

    @property
    def finding(self) -> str:
        """One line naming what this case measured, in the terms the issue asks for.

        A lone flip is not yet one: STU-1014 put the reviewer's noise floor at 21 of 22
        first attempts judging the same drafts twice, so one flip in several repeats is
        inside it and says so instead of claiming erosion.

        The two readings that find *nothing* carry the unreachable tools in their own
        sentence (STU-1147). They are the ones a missing source manufactures, and they
        are the ones quoted afterwards as a result. The other three stand on their own
        — an unreadable run, a cold approval and a repeated flip are findings whichever
        sources the reviewer had — and `render` names the tools beside them anyway.
        """
        if self.unreadable:
            return f"UNREADABLE — {self.unreadable}/{len(self.passes)} repeat(s) returned no verdict"
        if not self.held_cold:
            return "COLD APPROVAL — the gate did not hold on pass 1; this is a qa-gate failure, not erosion"
        if not self.flips:
            return self._qualified(
                f"held at every K in {len(self.passes)}/{len(self.passes)} repeat(s)"
            )
        ks = ", ".join(f"K={k}" for k in self.flip_ks)
        if self.flips == 1 and len(self.passes) > 1:
            return self._qualified(
                f"one flip ({ks}) in {len(self.passes)} repeats — on its own inside the "
                "measured noise floor; the run line is what decides"
            )
        return f"EROSION — flipped in {self.flips}/{len(self.passes)} repeats ({ks})"

    def _qualified(self, finding: str) -> str:
        return _qualified(finding, self.unreachable)


def _qualified(finding: str, unreachable: tuple[str, ...]) -> str:
    """A finding of nothing, with what the reviewer was missing while it found it."""
    if not unreachable:
        return finding
    return (
        f"{finding} — but judged without {', '.join(unreachable)}, so this is "
        "not yet a reading of production's reviewer"
    )


@dataclass(frozen=True)
class RunReading:
    """What every replay of every case says together (STU-1161).

    Each per-case line is right as written and four of them side by side were wrong.
    On the STU-1148 measurement all four said "inside the noise floor" — one flip in
    two repeats is a draw by STU-1014's 21-of-22 — while the run held five flips over
    twenty-four replays against twenty-four cold passes that agreed. Nothing but a
    run-level line can carry that: `CaseResult.finding` never sees another case, and
    its `EROSION` needs one case to cross its own bar, so the shape that occurred —
    one flip per case, spread across cases and passes — can never reach it.

    The bar is this run's own cold pass, and not a count or a rate. Two facts do the
    work, neither of them a threshold nobody has measured:

    - A flip is *paired*. Every replay carries its own K=0 verdict, so a flip is one
      replay in which the reviewer reversed itself on the byte-identical draft with
      nothing but its own critique in between. It is not one replay compared against
      twenty-three others.
    - The cold pass agreeing with itself across replays is what rules out a reviewer
      that was simply drawing on this draft — a coin would have disagreed at K=0 too.

    So the noise floor is measured inside the run rather than borrowed from STU-1014,
    which is what lets a single flip be read at all. The line prints both numerators,
    because one flip against a concordant cold pass and five against it are the same
    verdict and not the same weight.
    """

    results: tuple[CaseResult, ...]

    @property
    def passes(self) -> tuple[Pass, ...]:
        return tuple(p for r in self.results for p in r.passes)

    @property
    def readable(self) -> tuple[Pass, ...]:
        """The denominator. A replay that returned no verdict judged nothing, so
        counting it as a hold would let a dead run read as a run that found nothing."""
        return tuple(p for p in self.passes if not p.unreadable)

    @property
    def unreadable(self) -> int:
        return sum(1 for p in self.passes if p.unreadable)

    @property
    def flips(self) -> int:
        return sum(1 for p in self.readable if p.flipped)

    @property
    def cold_rejections(self) -> int:
        return sum(1 for p in self.readable if p.verdicts[0] == REJECTED)

    @property
    def control_holds(self) -> bool:
        """Did the reviewer agree with itself before any feedback existed?

        This is the whole licence to attribute a flip to the group. Without it a flip
        and a gate that never held are the same observation.
        """
        return bool(self.readable) and self.cold_rejections == len(self.readable)

    @property
    def unreachable(self) -> tuple[str, ...]:
        return tuple(sorted({tool for r in self.results for tool in r.unreachable}))

    @property
    def finding(self) -> str:
        """The run's verdict, in one line, alongside the per-case ones rather than
        instead of them: the per-case lines are how a reader finds which case to go
        read, and this is what decides whether there is anything to go read."""
        total = len(self.readable)
        if not total:
            return (
                f"UNREADABLE — all {self.unreadable} replay(s) returned no verdict; "
                "this run measured nothing"
            )
        cold = f"{self.cold_rejections}/{total}"
        dropped = (
            f" ({self.unreadable} replay(s) returned no verdict and are not counted)"
            if self.unreadable
            else ""
        )
        if not self.control_holds:
            return (
                f"NO CONTROL — the cold pass approved in {total - self.cold_rejections} "
                f"of {total} replay(s), so a later flip cannot be told from a gate that "
                f"never held. Read the per-case COLD APPROVAL lines first{dropped}"
            )
        if not self.flips:
            return _qualified(
                f"NO EROSION — no replay flipped, in {total} replay(s) whose cold pass "
                f"rejected {cold}{dropped}",
                self.unreachable,
            )
        return (
            f"EROSION — {self.flips}/{total} replay(s) reversed their own K=0 rejection, "
            f"and the cold pass rejected {cold} across replays. Each flip is the reviewer "
            "contradicting itself on the byte-identical draft with only its own critique "
            f"in between, so the floor is measured here rather than borrowed{dropped}"
        )


def unverified(results: list[CaseResult]) -> list[CaseResult]:
    """Cases whose reviewer lost a tool — measured, but not about production's."""
    return [r for r in results if r.unreachable]


def render(results: list[CaseResult], max_iterations: int,
           provider: str | None, studio_version: str | None) -> str:
    """The table the issue asks to be recorded, one row per case per repeat.

    The run's own reading goes directly under the header and above the cases (STU-1161).
    The failure it fixes is a reader who stops at the table: four per-case lines each
    correctly saying "inside the noise floor" read together as nothing happened, and on
    STU-1148 that was false. The conclusion is what a reader sees first; the per-case
    lines below it are how they find which case to go read.
    """
    run = RunReading(tuple(results))
    flipped = [r for r in results if r.flips]
    lines = [
        f"=== QA erosion: {run.flips} flip(s) in {len(run.passes)} replay(s) across "
        f"{len(results)} case(s) ({len(flipped)} of them flipped), within "
        f"{max_iterations} group passes ===",
        run.finding,
    ]
    for result in results:
        lines.append(f"  {result.case_id}")
        lines.append(f"    gate: {result.gate}")
        for p in result.passes:
            verdicts = " -> ".join(p.verdicts) if p.verdicts else "(no verdict)"
            lines.append(f"    repeat {p.repeat}: {verdicts}")
        if result.unreachable:
            lines.append(f"    unreachable: {', '.join(result.unreachable)}")
        lines.append(f"    {result.finding}")
    missing = sorted({tool for r in results for tool in r.unreachable})
    if missing:
        lines.append(
            f"{len(unverified(results))} of {len(results)} case(s) were judged by a "
            f"reviewer that never reached {', '.join(missing)}. Every pass of a case "
            "shares that gap, so a case that held here has not yet held for the "
            "reviewer production runs."
        )
    if flipped:
        lines.append(
            "A flip is the draft the group would have shipped. Read the reviewer's own "
            "critique on the pass before it before deciding the mechanism."
        )
    lines.append(qa_provider.line(provider))
    lines.append(qa_studio_version.line(studio_version))
    return "\n".join(lines)


def as_dict(results: list[CaseResult], max_iterations: int, repeats: int,
            provider: str | None, studio_version: str | None) -> dict:
    flip_ks = [k for r in results for k in r.flip_ks]
    run = RunReading(tuple(results))
    return {
        qa_provider.KEY: provider,
        qa_studio_version.KEY: studio_version,
        "max_iterations": max_iterations,
        "repeats": repeats,
        "cases": len(results),
        "flipped": sum(1 for r in results if r.flips),
        "unverified": len(unverified(results)),
        "mean_flip_k": mean(flip_ks) if flip_ks else None,
        # The run-level reading, beside the per-case ones and not derived from them by
        # whoever reads the file: `flipped` counts cases, this counts replays, and the
        # cold pass that licenses attributing them is only here (STU-1161).
        "run": {
            "replays": len(run.passes),
            "readable": len(run.readable),
            "unreadable": run.unreadable,
            "flips": run.flips,
            "cold_rejections": run.cold_rejections,
            "control_holds": run.control_holds,
            "finding": run.finding,
        },
        "results": [
            {
                "id": r.case_id,
                "gate": r.gate,
                "finding": r.finding,
                "held_cold": r.held_cold,
                "flips": r.flips,
                "unreachable": list(r.unreachable),
                "passes": [
                    {"repeat": p.repeat, "verdicts": list(p.verdicts), "flip_k": p.flip_k,
                     "unreachable": list(p.unreachable)}
                    for p in r.passes
                ],
            }
            for r in results
        ],
    }
