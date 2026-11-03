"""Score two drafting models on the conversations a finished run drafted (STU-1073).

The drafter is the largest single line of the drafting bill — 64% of it on the three
post-STU-1051 runs of 2026-08-29 — and it was the only leg no measurement had ever
decided. The judge was settled by STU-1014, the classifier by STU-1049, the cache by
STU-1051.

"Is this draft good" is not mechanical, so unlike `classify_bakeoff` there is no golden
to score against. The judge already in the pipeline is the scorer: `draft-qa` on Opus,
whose own noise floor STU-1014 measured at 21 of 22 first attempts when asked to rule on
the same drafts twice. Both arms author over the same conversations and are judged by
that same reviewer, so a gap in what it accepts is a gap between the drafters.

The number that decides is cost per *accepted* draft, never cost per call. STU-1014's
finding runs backwards here: a rejection is a whole group iteration, a `generate-draft`
and a `draft-qa` paid again. A drafter 60% cheaper per call and rejected twice as often
costs more, and a per-call table would have called it a bargain.

Two axes, both scored only where an arm agreed with itself across its repeats:

- `accepted` — the reviewer approved a draft within the group's iterations. The
  headline: a conversation no draft ever satisfies is a parent reading a bad reply.
- `accepted_first` — approved on the first attempt. The cost axis, and the one
  STU-1014's noise floor was measured on.

Both arms author live, so a tool that raised on every call it made is missing from the
*drafter* as well as from the reviewer — the site search, the precedent search, the club
memory. It is named per arm beside the figures and does not change the exit code
(STU-1147), for the reason the QA bake-off does not either: this prints a number a human
reads once, where `make qa-gate` exits 4 because it would otherwise stamp a record other
trees read back as an assertion. Per arm, because a source down for both is on both
sides of the comparison while one down for a single arm is two different pipelines.

Not every raise means a lost source (STU-1235): `org_memory-read_memory` raises for any
name while `club-memory/`'s own index is empty, and calling it anyway, or declining to,
is a real difference between two models — the exact thing this bake-off exists to show,
not a broken host. That raise is reported separately, under a name that says the store
was checked and found empty, and never counts toward `unreachable` — an arm the store
excused this way is not missing anything the other arm has.

`accepted` has now come back at the ceiling for both arms twice in a row — 44/44 and
44/44 on STU-1073, 22/22 and 22/22 on STU-1162 — so the pin was decided by
`accepted_first` alone both times, and the second time `accepted_first` did not separate
the models either (opus 11/17 against sonnet 12/20, under noise floors of 0.77 and 0.91:
not a real difference). Both axes are booleans scored per conversation, and a conversation
either passes the gate or it does not — there is no room in a coin for two models that
both land heads most of the time.

`iterations` (STU-1237) is the axis with room, and it costs nothing new to compute:
`Authored.attempts` already counts the drafting stage's executions for the group
`draft-qa` ruled on, so the number of iterations a conversation took to reach acceptance
was in the trace from the first bake-off onward — `Spend.attempts` already sums it per
arm for the spend line (61 against 62 on the STU-1162 pass). What was missing was scoring
it like `accepted` is scored: per conversation, only where an arm agreed with itself
across repeats, so a mean isn't quietly built from a run that flip-flopped. It is
deliberately not a third boolean — a boolean has the same ceiling problem `accepted_first`
does, and the point is a number with headroom under it.

It answers a narrower question than `accepted` on purpose: how many iterations did
*acceptance* cost, for the conversations where an arm was stably accepted. A conversation
an arm never settles is already counted against it by `accepted`; asking `iterations` to
speak for it too would double a penalty `accepted` already levies and blur the one thing
`accepted` still decides alone — whether a drafter is correct at all. So `accepted` stays
the gate a candidate cannot buy its way out of with fewer iterations, and `iterations` is
read only when `accepted` ties, which is exactly the case that has come up twice.
"""

from dataclasses import dataclass
from typing import Callable

from email_classifier.cost import price

APPROVED = "approved"


@dataclass(frozen=True)
class Authored:
    """What one arm's authoring run over one conversation produced.

    `verdicts` is the reviewer's ruling per group iteration, in order; empty means the
    run reached the judge no times at all, which a dead run does. `attempts` counts the
    drafting stage's executions instead, so an iteration that died before the reviewer
    is still visible as one the arm consumed.
    """

    thread_id: str
    verdicts: tuple[str, ...]
    attempts: int
    by_model: dict[str, dict[str, int]]
    unreachable: tuple[str, ...] = ()
    # A tool that raised on every call because its store is verifiably empty in this
    # tree (STU-1235). Disjoint from `unreachable`: nothing was lost, so this never
    # counts toward it and never makes the arms read as two different pipelines.
    empty_store: tuple[str, ...] = ()

    @property
    def errored(self) -> bool:
        return not self.verdicts

    @property
    def accepted(self) -> bool:
        return self.verdicts[-1:] == (APPROVED,)

    @property
    def accepted_first(self) -> bool:
        return self.verdicts[:1] == (APPROVED,)

    @property
    def usd(self) -> float:
        return sum(price(counts, model) for model, counts in self.by_model.items())


@dataclass(frozen=True)
class Arm:
    model: str
    repeats: tuple[dict[str, Authored], ...]

    @property
    def runs(self) -> tuple[Authored, ...]:
        return tuple(run for repeat in self.repeats for run in repeat.values())

    @property
    def unreachable(self) -> tuple[str, ...]:
        """Every tool no run of this arm could confirm it had.

        Pooled over the arm rather than reported per conversation: a tool that raised
        is down for the machine and the grant, not for one thread, so listing the
        conversations would be a longer way of naming the arm.
        """
        return tuple(sorted({tool for run in self.runs for tool in run.unreachable}))

    @property
    def empty_store(self) -> tuple[str, ...]:
        """Every tool this arm raised on only because its store was empty (STU-1235).

        Pooled the same way `unreachable` is, and reported beside it rather than
        folded in: excusing a raise here is a fact about the tree, not the arm, so it
        does not make one arm read as missing what the other has.
        """
        return tuple(sorted({tool for run in self.runs for tool in run.empty_store}))


@dataclass(frozen=True)
class Axis:
    name: str
    of: Callable[[Authored], bool]


AXES = (
    Axis("accepted", lambda a: a.accepted),
    Axis("accepted_first", lambda a: a.accepted_first),
)

HEADLINE = AXES[0]


@dataclass(frozen=True)
class Score:
    axis: str
    model: str
    conversations: int          # every conversation both arms were asked for — the denominator
    judged: int                 # of those, the ones no repeat errored on
    stable: int                 # of those, the ones every repeat ruled the same way
    held: int                   # of the stable ones, the ones the axis holds for
    unstable: tuple[str, ...]
    errored: tuple[str, ...]
    repeats: int

    @property
    def noise_floor(self) -> float | None:
        """How often the arm agreed with itself. None under a single repeat, where every
        answer is trivially stable and a reported 1.0 would be an artefact of asking once."""
        if self.repeats < 2 or not self.judged:
            return None
        return self.stable / self.judged

    @property
    def rate(self) -> float | None:
        """The axis over what the arm settled. None when it settled nothing — a rate over
        an empty denominator is not a zero."""
        return self.held / self.stable if self.stable else None


@dataclass(frozen=True)
class Spend:
    """What an arm burned, over every run it made — the economics half.

    Counted over all repeats and not only the stable conversations: this is money that
    was actually spent, and a rejection an arm made on one repeat and not the next cost
    a group iteration all the same.
    """

    model: str
    runs: int
    attempts: int
    accepted: int
    tokens: dict[str, int]
    usd: float

    @property
    def usd_per_accepted_draft(self) -> float | None:
        """None when the arm reached no accepted draft: a real bill over a denominator
        the arm never had is a made-up number, the way an unpriced model raises."""
        return self.usd / self.accepted if self.accepted else None


def spend(arm: Arm) -> Spend:
    runs = arm.runs
    tokens: dict[str, int] = {}
    for run in runs:
        for counts in run.by_model.values():
            for field, value in counts.items():
                tokens[field] = tokens.get(field, 0) + value
    return Spend(
        model=arm.model,
        runs=len(runs),
        attempts=sum(run.attempts for run in runs),
        accepted=sum(1 for run in runs if run.accepted),
        tokens=tokens,
        usd=sum(run.usd for run in runs),
    )


def _settled(arm: Arm, thread_id: str, axis: Axis) -> bool | None:
    """The arm's answer on this axis when every repeat gave the same one, else None."""
    seen = {axis.of(repeat[thread_id]) for repeat in arm.repeats if thread_id in repeat}
    return seen.pop() if len(seen) == 1 else None


def score(arm: Arm, thread_ids: list[str], axis: Axis) -> Score:
    judged, stable, held = [], [], 0
    unstable, errored = [], []
    for thread_id in thread_ids:
        runs = [repeat[thread_id] for repeat in arm.repeats if thread_id in repeat]
        if len(runs) < len(arm.repeats) or any(run.errored for run in runs):
            errored.append(thread_id)
            continue
        judged.append(thread_id)
        answer = _settled(arm, thread_id, axis)
        if answer is None:
            unstable.append(thread_id)
            continue
        stable.append(thread_id)
        held += bool(answer)
    return Score(
        axis=axis.name,
        model=arm.model,
        conversations=len(thread_ids),
        judged=len(judged),
        stable=len(stable),
        held=held,
        unstable=tuple(unstable),
        errored=tuple(errored),
        repeats=len(arm.repeats),
    )


@dataclass(frozen=True)
class Iterations:
    """How many group iterations it took to reach acceptance (STU-1237).

    Continuous where `accepted` is binary, and scored on the same denominator and the
    same stability rule: only a conversation where the arm's *accept/reject* verdict
    agreed across every repeat contributes, and only when that agreement was `accepted`
    — a conversation the arm never settled has no acceptance to count iterations to, and
    `accepted` already penalizes it. `mean` averages, per contributing conversation, the
    attempts across that conversation's own repeats first (so a conversation runs count
    once, not once per repeat) and then across conversations.
    """

    model: str
    conversations: int          # every conversation both arms were asked for
    accepted: int               # of those, stably accepted — the ones `mean` is over
    mean: float | None
    unstable: tuple[str, ...]   # accept/reject itself disagreed across repeats
    errored: tuple[str, ...]


def iterations_to_accept(arm: Arm, thread_ids: list[str]) -> Iterations:
    accepted, per_conversation, unstable, errored = [], [], [], []
    for thread_id in thread_ids:
        runs = [repeat[thread_id] for repeat in arm.repeats if thread_id in repeat]
        if len(runs) < len(arm.repeats) or any(run.errored for run in runs):
            errored.append(thread_id)
            continue
        answer = _settled(arm, thread_id, HEADLINE)
        if answer is None:
            unstable.append(thread_id)
            continue
        if not answer:
            continue
        accepted.append(thread_id)
        per_conversation.append(sum(run.attempts for run in runs) / len(runs))
    return Iterations(
        model=arm.model,
        conversations=len(thread_ids),
        accepted=len(accepted),
        mean=sum(per_conversation) / len(per_conversation) if per_conversation else None,
        unstable=tuple(unstable),
        errored=tuple(errored),
    )


@dataclass(frozen=True)
class Bakeoff:
    baseline: str
    candidate: str
    scores: tuple[Score, ...]
    spends: tuple[Spend, ...]
    iterations: tuple[Iterations, ...]
    unreachable: dict[str, tuple[str, ...]]
    empty_store: dict[str, tuple[str, ...]]

    def of(self, model: str, axis: str) -> Score:
        return next(s for s in self.scores if s.model == model and s.axis == axis)

    def spent(self, model: str) -> Spend:
        return next(s for s in self.spends if s.model == model)

    def iterations_of(self, model: str) -> Iterations:
        return next(it for it in self.iterations if it.model == model)

    @property
    def candidate_needs_more_iterations(self) -> bool | None:
        """None when either arm settled no accepted draft to average iterations over —
        the tie-break `iterations` exists for has nothing to break in that case."""
        base, cand = self.iterations_of(self.baseline), self.iterations_of(self.candidate)
        if base.mean is None or cand.mean is None:
            return None
        return cand.mean > base.mean

    @property
    def measured(self) -> bool:
        """Whether the baseline settled anything for the candidate to be scored against.

        A pass where it did not has found nothing rather than a worse candidate: every
        arm erroring or flip-flopping reads through `candidate_is_worse` exactly like a
        bad candidate does, and naming that a verdict on the model is the wrong fact.
        """
        return self.of(self.baseline, HEADLINE.name).rate is not None

    @property
    def candidate_is_worse(self) -> bool:
        """The question this exists to answer: fewer accepted drafts than the baseline.

        Counts and not rates, and over the same denominator for both arms, for
        `classify_bakeoff.candidate_is_worse`'s reason: a rate over what each arm
        happened to settle lets a model that flip-flopped on all but two conversations
        print 100%. A draft a model cannot hold is a draft lost like a rejected one.

        Equal counts are not worse — a cheaper model that ties is the trade this
        licenses, and the spend beside it is what makes the trade worth taking.
        """
        return (
            self.of(self.candidate, HEADLINE.name).held
            < self.of(self.baseline, HEADLINE.name).held
        )


def compare(baseline: Arm, candidate: Arm, thread_ids: list[str]) -> Bakeoff:
    return Bakeoff(
        baseline=baseline.model,
        candidate=candidate.model,
        scores=tuple(
            score(arm, thread_ids, axis) for axis in AXES for arm in (baseline, candidate)
        ),
        spends=tuple(spend(arm) for arm in (baseline, candidate)),
        iterations=tuple(
            iterations_to_accept(arm, thread_ids) for arm in (baseline, candidate)
        ),
        unreachable={arm.model: arm.unreachable for arm in (baseline, candidate)},
        empty_store={arm.model: arm.empty_store for arm in (baseline, candidate)},
    )


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def _usd(amount: float | None) -> str:
    return "n/a" if amount is None else f"${amount:.3f}"


def render(result: Bakeoff) -> str:
    lines = [f"=== Draft bake-off {result.baseline} vs {result.candidate} ==="]
    for axis in AXES:
        lines.append(f"-- {axis.name}")
        for model in (result.baseline, result.candidate):
            s = result.of(model, axis.name)
            lines.append(
                f"  {model}: {s.held}/{s.conversations} | "
                f"rate {_pct(s.rate)} ({s.held}/{s.stable} of what it settled) | "
                f"noise floor {_pct(s.noise_floor)} ({s.stable}/{s.judged} settled over "
                f"{s.repeats} repeat(s))"
            )
            if s.unstable:
                lines.append("    unstable across repeats: " + ", ".join(s.unstable))
            if s.errored:
                lines.append("    no verdict (not scored): " + ", ".join(s.errored))
    lines.append("-- iterations to acceptance (continuous; the tie-break when accepted ties)")
    for model in (result.baseline, result.candidate):
        it = result.iterations_of(model)
        mean_str = "n/a" if it.mean is None else f"{it.mean:.2f}"
        lines.append(
            f"  {model}: {it.accepted}/{it.conversations} accepted | "
            f"mean {mean_str} iteration(s) to acceptance"
        )
        if it.unstable:
            lines.append("    accept/reject unstable across repeats: " + ", ".join(it.unstable))
        if it.errored:
            lines.append("    no verdict (not scored): " + ", ".join(it.errored))
    lines.append("-- spend")
    for model in (result.baseline, result.candidate):
        sp = result.spent(model)
        lines.append(
            f"  {model}: {sp.runs} run(s), {sp.attempts} group iteration(s), "
            f"{sp.accepted} accepted | {sp.tokens.get('total_tokens', 0)} tokens | "
            f"{_usd(sp.usd)} total | {_usd(sp.usd_per_accepted_draft)} per accepted draft"
        )
    missing = {model: tools for model, tools in result.unreachable.items() if tools}
    if missing:
        lines.append("-- unreachable (a tool raised on every call it made)")
        for model, tools in missing.items():
            lines.append(f"  {model}: {', '.join(tools)}")
        lines.append(
            "Both arms lost the same source, so they were compared like with like — but "
            "neither authored or was judged as production does."
            if len(missing) == len(result.unreachable)
            and len({tools for tools in missing.values()}) == 1
            else "Only one arm lost a source, so the arms are two different pipelines "
                 "and the gap between them is not a gap between the two models."
        )
    touched = {model: tools for model, tools in result.empty_store.items() if tools}
    if touched:
        lines.append(
            "-- asked a store this tree's index says is empty (not a lost source)"
        )
        for model, tools in touched.items():
            lines.append(f"  {model}: {', '.join(tools)}")
        if len(touched) == 1:
            (asked,) = touched
            declined = result.candidate if asked == result.baseline else result.baseline
            lines.append(
                f"{asked} called it and {declined} did not, given the same empty "
                "store — a calling-behavior difference between the two models, not "
                "two pipelines."
            )
    if not result.measured:
        lines.append(
            f"NO MEASUREMENT: {result.baseline} settled no draft of its own, so there "
            f"is nothing for {result.candidate} to be scored against"
        )
    else:
        verdict = "WORSE than" if result.candidate_is_worse else "at least as good as"
        lines.append(f"{result.candidate} is {verdict} {result.baseline} on {HEADLINE.name}")
        more = result.candidate_needs_more_iterations
        if more is None:
            lines.append(
                "no iteration tie-break: one arm settled no accepted draft to average over"
            )
        else:
            base_mean = result.iterations_of(result.baseline).mean
            cand_mean = result.iterations_of(result.candidate).mean
            direction = "more" if more else "fewer" if cand_mean < base_mean else "the same"
            lines.append(
                f"{result.candidate} needs {direction} iterations to acceptance than "
                f"{result.baseline} on average ({cand_mean:.2f} vs {base_mean:.2f})"
            )
    return "\n".join(lines)


def as_dict(result: Bakeoff) -> dict:
    return {
        "baseline": result.baseline,
        "candidate": result.candidate,
        "measured": result.measured,
        "candidate_is_worse": result.candidate_is_worse,
        "candidate_needs_more_iterations": result.candidate_needs_more_iterations,
        "unreachable": {model: list(tools) for model, tools in result.unreachable.items()},
        "empty_store": {model: list(tools) for model, tools in result.empty_store.items()},
        "scores": [
            {
                "axis": s.axis,
                "model": s.model,
                "conversations": s.conversations,
                "judged": s.judged,
                "stable": s.stable,
                "held": s.held,
                "rate": s.rate,
                "noise_floor": s.noise_floor,
                "unstable": list(s.unstable),
                "errored": list(s.errored),
            }
            for s in result.scores
        ],
        "iterations": [
            {
                "model": it.model,
                "conversations": it.conversations,
                "accepted": it.accepted,
                "mean": it.mean,
                "unstable": list(it.unstable),
                "errored": list(it.errored),
            }
            for it in result.iterations
        ],
        "spend": [
            {
                "model": sp.model,
                "runs": sp.runs,
                "attempts": sp.attempts,
                "accepted": sp.accepted,
                "tokens": sp.tokens,
                "usd": round(sp.usd, 4),
                "usd_per_accepted_draft": (
                    None if sp.usd_per_accepted_draft is None
                    else round(sp.usd_per_accepted_draft, 4)
                ),
            }
            for sp in result.spends
        ],
    }
