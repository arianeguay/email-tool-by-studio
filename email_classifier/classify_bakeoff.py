"""Score two classifier models on the escalated club subset (STU-1045).

Nothing in the repo could answer "is this model good enough to classify with". `eval run`
replays stage 1 and never calls the LLM, so it reports the same 35/7/0 under any model,
and `eval/club-golden.json` holds `bucket: null, action: null` for every fixture that
escalates — the golden a stage-1 replay seeds cannot carry a stage-2 verdict. So the only
evidence that had ever decided a classifier model was two live rounds on three messages
(STU-1044), and reverting to the older model was the safe answer rather than a measured
one.

This is the classification half of what `make qa-bakeoff` did for `draft-qa`: one fixed
corpus, one model per arm, agreement counted against a noise floor. Unlike the QA
bake-off it needs no recorded traffic — a fixture is a whole input, so both arms run
directly from `fixtures-club/`.

Two axes, scored the same way:

- `verdict` — (bucket, suggested_action), the headline.
- `needs_reply` — whether the action is `needs_reply` at all. Scored apart because that
  is the axis a miss costs a parent a reply on, and the axis STU-1044's regression landed
  on. It fails in both directions since STU-1048: the corpus carries two bucket-A messages
  that owe no reply, so a model that answers `needs_reply` to everything now scores below
  one that does not, where before it tied. Still lopsided — 25 of the 28 fixtures are
  someone asking something, which is the mailbox — so a gap of one message is a gap of one
  message, not a rate.

A model is scored only where it agreed with *itself* across its repeats. The LLM is not
deterministic, so a single round cannot tell a wrong verdict from a coin flip; the share
of messages an arm settled is the noise floor, and it is what says whether a gap between
two models is a gap at all.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from email_classifier.core.contract import Taxonomy
from email_classifier.eval.ab import Arm, Verdict

_NEEDS_REPLY = "needs_reply"


@dataclass(frozen=True)
class GoldenLabel:
    """The reviewed verdict for one escalating fixture.

    Field names match `Verdict`'s on purpose: an axis then projects a golden label and a
    model's answer through the same function, so the two can never be read differently.
    """

    bucket: str
    action: str


@dataclass(frozen=True)
class Axis:
    name: str
    of: Callable[[GoldenLabel | Verdict], object]


AXES = (
    Axis("verdict", lambda v: (v.bucket, v.action)),
    Axis(_NEEDS_REPLY, lambda v: v.action == _NEEDS_REPLY),
)

HEADLINE = AXES[0]


def load_golden(path: Path, taxonomy: Taxonomy) -> dict[str, GoldenLabel]:
    """The reviewed stage-2 verdicts, refused rather than trusted when out of taxonomy.

    A golden row naming a bucket/action pair the contract forbids would mark every model
    wrong on that message forever, and no run could ever produce the answer it wants.
    `taxonomy` is this deployment's own bucket shape (STU-1461), required and never
    defaulted — the same reason `load_golden` refuses a row outside it.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    golden = {
        message_id: GoldenLabel(bucket=row["bucket"], action=row["suggested_action"])
        for message_id, row in data.items()
    }
    invalid = [
        f"{message_id}: {label.bucket}/{label.action}"
        for message_id, label in sorted(golden.items())
        if label.bucket not in taxonomy.buckets
        or label.action not in taxonomy.allowed_actions[label.bucket]
    ]
    if invalid:
        raise ValueError(f"golden rows outside the taxonomy: {', '.join(invalid)}")
    return golden


def coverage(message_ids: list[str], golden: dict[str, GoldenLabel]) -> tuple[list[str], list[str]]:
    """What the golden does not answer for, and what it answers for that no longer escalates.

    Checked before the first call, not after: a bake-off silently scored on a subset of
    the corpus is a wrong number, and the arms cost a stage-2 pass each to produce it.
    """
    escalating = set(message_ids)
    return (
        sorted(mid for mid in escalating if mid not in golden),
        sorted(mid for mid in golden if mid not in escalating),
    )


@dataclass(frozen=True)
class Miss:
    message_id: str
    expected: str
    actual: str


@dataclass(frozen=True)
class Score:
    axis: str
    model: str
    covered: int                   # golden rows the arm was asked about — the shared denominator
    judged: int                    # golden-covered messages the arm answered without erroring
    stable: int                    # of those, the ones every repeat agreed on
    correct: int                   # of the stable ones, the ones matching the golden
    unstable: tuple[str, ...]
    errored: tuple[str, ...]
    misses: tuple[Miss, ...]
    repeats: int

    @property
    def noise_floor(self) -> float | None:
        """How often the model agreed with itself. None under a single repeat, where
        every answer is trivially stable and a reported 1.0 would be an artefact."""
        if self.repeats < 2 or not self.judged:
            return None
        return self.stable / self.judged

    @property
    def agreement(self) -> float | None:
        """Agreement with the golden over what the model settled. None when it settled
        nothing — a rate over an empty denominator is not a zero."""
        return self.correct / self.stable if self.stable else None


def _settled(arm: Arm, message_id: str, axis: Axis) -> object | None:
    """The arm's answer on this axis when every repeat gave the same one, else None."""
    seen = {axis.of(round_[message_id]) for round_ in arm.repeats if message_id in round_}
    return seen.pop() if len(seen) == 1 else None


def _errored(arm: Arm, message_id: str) -> bool:
    return any(round_.get(message_id, Verdict(None, None)).error for round_ in arm.repeats)


def score(arm: Arm, golden: dict[str, GoldenLabel], axis: Axis) -> Score:
    judged, stable, correct = [], [], 0
    unstable, errored, misses = [], [], []
    for message_id, label in sorted(golden.items()):
        if _errored(arm, message_id):
            errored.append(message_id)
            continue
        judged.append(message_id)
        answer = _settled(arm, message_id, axis)
        if answer is None:
            unstable.append(message_id)
            continue
        stable.append(message_id)
        expected = axis.of(label)
        if answer == expected:
            correct += 1
        else:
            misses.append(Miss(message_id, str(expected), str(answer)))
    return Score(
        axis=axis.name,
        model=arm.name,
        covered=len(golden),
        judged=len(judged),
        stable=len(stable),
        correct=correct,
        unstable=tuple(unstable),
        errored=tuple(errored),
        misses=tuple(misses),
        repeats=len(arm.repeats),
    )


@dataclass(frozen=True)
class Bakeoff:
    baseline: str
    candidate: str
    scores: tuple[Score, ...]

    def of(self, model: str, axis: str) -> Score:
        return next(s for s in self.scores if s.model == model and s.axis == axis)

    @property
    def measured(self) -> bool:
        """Whether the baseline settled anything to be compared against.

        A pass where it did not has found nothing, not a worse candidate — every arm
        erroring or flip-flopping reads the same way through `candidate_is_worse`, and
        naming that a verdict on the model would be the wrong fact entirely.
        """
        return self.of(self.baseline, HEADLINE.name).agreement is not None

    @property
    def candidate_is_worse(self) -> bool:
        """The one question the bake-off exists to answer, on the headline axis.

        Counts of correct verdicts, not the two arms' agreement rates: those are rates
        over what each arm happened to settle, and comparing them lets a candidate that
        answered four messages consistently and flip-flopped on the other twenty-two
        print 100% and outscore a baseline that settled all twenty-six. In production a
        verdict a model cannot hold is a verdict lost exactly like a wrong one, so both
        arms are scored over the same denominator — every golden row — and a message an
        arm errored on or could not settle simply is not correct on it.

        Equal counts are not worse: a cheaper model that ties is the trade this licenses.
        """
        return (
            self.of(self.candidate, HEADLINE.name).correct
            < self.of(self.baseline, HEADLINE.name).correct
        )


def compare(baseline: Arm, candidate: Arm, golden: dict[str, GoldenLabel]) -> Bakeoff:
    return Bakeoff(
        baseline=baseline.name,
        candidate=candidate.name,
        scores=tuple(
            score(arm, golden, axis) for axis in AXES for arm in (baseline, candidate)
        ),
    )


def _pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.0%}"


def render(result: Bakeoff) -> str:
    lines = [f"=== Classify bake-off {result.baseline} vs {result.candidate} ==="]
    for axis in AXES:
        lines.append(f"-- {axis.name}")
        for model in (result.baseline, result.candidate):
            s = result.of(model, axis.name)
            lines.append(
                f"  {model}: {s.correct}/{s.covered} correct | "
                f"agreement {_pct(s.agreement)} ({s.correct}/{s.stable} of what it settled) | "
                f"noise floor {_pct(s.noise_floor)} ({s.stable}/{s.judged} settled over "
                f"{s.repeats} repeat(s))"
            )
            lines += [
                f"    MISS {m.message_id}: golden {m.expected} -> {m.actual}"
                for m in s.misses
            ]
            if s.unstable:
                lines.append("    unstable across repeats: " + ", ".join(s.unstable))
            if s.errored:
                lines.append("    stage-2 error (not scored): " + ", ".join(s.errored))
    if not result.measured:
        lines.append(
            f"NO MEASUREMENT: {result.baseline} settled no verdict of its own, so there "
            f"is nothing for {result.candidate} to be scored against"
        )
    else:
        verdict = "WORSE than" if result.candidate_is_worse else "at least as good as"
        lines.append(f"{result.candidate} is {verdict} {result.baseline} on {HEADLINE.name}")
    return "\n".join(lines)


def as_dict(result: Bakeoff) -> dict:
    return {
        "baseline": result.baseline,
        "candidate": result.candidate,
        "measured": result.measured,
        "candidate_is_worse": result.candidate_is_worse,
        "scores": [
            {
                "axis": s.axis,
                "model": s.model,
                "covered": s.covered,
                "judged": s.judged,
                "stable": s.stable,
                "correct": s.correct,
                "agreement": s.agreement,
                "noise_floor": s.noise_floor,
                "unstable": list(s.unstable),
                "errored": list(s.errored),
                "misses": [
                    {"message_id": m.message_id, "expected": m.expected, "actual": m.actual}
                    for m in s.misses
                ],
            }
            for s in result.scores
        ],
    }
