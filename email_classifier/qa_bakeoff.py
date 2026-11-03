"""Score two `draft-qa` models against one corpus of already-written drafts (STU-1014).

The QA pass was a third of a `mailbox-full` run's bill on the pipeline default model,
for a stage that judges a draft rather than writes one. What gates a cheaper judge is
not the config line but evidence that it still rejects what the current one rejects,
and that evidence needs the *same* drafts: re-running the pipeline writes new ones, and
two judges scoring two corpora agree or disagree about nothing.

A finished run's Studio journal already holds them — one `write-draft` output per
attempt, and the fan-out item behind each — so the corpus costs no drafting to assemble
and is the traffic the cost was measured on.

Both arms are then re-judged live rather than one being read off the journal: a verdict
recorded for a group's second attempt was reached on a prompt carrying the feedback the
first one drew, and a single-stage replay has nothing to reconstruct that from. Two arms
through the same replay is the only shape where the two prompts are equal.

What each arm could not reach is reported beside the diff and not folded into it
(STU-1147). A judge missing a source is not a disagreement, and counting it as one
would move the number this exists to publish; a tool that was down for both arms is on
both sides of the diff and mostly cancels, while one down for a single arm makes the
whole comparison two different reviewers. Annotated rather than failed, for the reason
`make qa-gate` is the opposite: the gate exits 4 because it would otherwise stamp a
record other trees read back as an assertion, and this prints a number a human reads
once. Failing would only make comparisons unrunnable on the machine that makes them.
"""

import json
from collections import Counter
from dataclasses import dataclass

_ITEM_STAGE = "drafts-pre"
_DRAFT_STAGE = "write-draft"


class OrphanDraft(Exception):
    """A recorded draft whose fan-out item is not in the journal."""


@dataclass(frozen=True)
class Draft:
    """One recorded draft and the item the drafter wrote it from."""

    thread_id: str
    attempt: int
    item: dict
    draft: dict

    @property
    def key(self) -> str:
        return f"{self.thread_id}#{self.attempt}"


def _completions(lines):
    """Every `stage_complete` in a journal that carries an output, as (stage, output)."""
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "stage_complete":
            continue
        output = event.get("output")
        if isinstance(output, dict):
            yield event.get("stage"), output


def items_from_journal(lines) -> dict[str, dict]:
    """The fan-out items a run's `drafts-pre` built, by thread.

    The corpus both bake-offs draw from: this one hands them to a reviewer beside a
    recorded draft, `draft_bakeoff` hands them to two drafters to author from (STU-1073).
    """
    items: dict[str, dict] = {}
    for stage, output in _completions(lines):
        if stage == _ITEM_STAGE:
            items = {i["thread_id"]: i for i in output.get("pending", [])}
    return items


def drafts_from_journal(lines) -> list[Draft]:
    """Every draft a run wrote, paired with its item.

    Keyed by thread plus attempt rather than by position: the fan-out interleaves, and
    `stage_complete` carries the root run id, so which child a completion belongs to is
    not readable off the ordering. `write-draft`'s own output names its thread.
    """
    items: dict[str, dict] = {}
    outputs: list[dict] = []
    for stage, output in _completions(lines):
        if stage == _ITEM_STAGE:
            items = {i["thread_id"]: i for i in output.get("pending", [])}
        elif stage == _DRAFT_STAGE:
            outputs.append(output)

    attempts: Counter = Counter()
    drafts = []
    for output in outputs:
        thread_id = output["thread_id"]
        if thread_id not in items:
            raise OrphanDraft(
                f"draft on thread {thread_id} has no {_ITEM_STAGE} item in the journal"
            )
        attempts[thread_id] += 1
        drafts.append(Draft(thread_id, attempts[thread_id], items[thread_id], output))
    return drafts


@dataclass(frozen=True)
class Disagreement:
    key: str
    baseline: str
    candidate: str

    @property
    def missed_rejection(self) -> bool:
        """The half that reaches a parent's inbox: what the baseline caught and this
        judge waved through. A rejection the baseline did not make costs a redraft."""
        return self.baseline == "rejected" and self.candidate != "rejected"


@dataclass(frozen=True)
class Comparison:
    judged: int
    disagreements: tuple[Disagreement, ...]

    @property
    def agreed(self) -> int:
        return self.judged - len(self.disagreements)

    @property
    def agreement(self) -> float:
        return self.agreed / self.judged if self.judged else 0.0

    @property
    def missed_rejections(self) -> tuple[Disagreement, ...]:
        return tuple(d for d in self.disagreements if d.missed_rejection)


def compare(baseline: dict[str, str], candidate: dict[str, str]) -> Comparison:
    """Diff two arms' verdicts over the same drafts.

    Both arms must have judged every draft. A key missing on one side is a dead run, and
    dropping it would count a draft nobody judged as one they agreed on — an arm records
    `error` for a run it could not read a verdict from, so a failure surfaces as a
    disagreement rather than as silence.
    """
    if set(baseline) != set(candidate):
        missing = sorted(set(baseline) ^ set(candidate))
        raise ValueError(f"arms judged different drafts: {missing}")
    return Comparison(
        judged=len(baseline),
        disagreements=tuple(
            Disagreement(key, baseline[key], candidate[key])
            for key in sorted(baseline)
            if baseline[key] != candidate[key]
        ),
    )


def unreachable_note(by_arm: dict[str, tuple[str, ...]]) -> list[str]:
    """What the arms could not reach, said in the summary that carries the figures.

    Per arm rather than pooled, because the two cases read differently. The same tool
    down for both arms leaves the agreement figure comparing like with like — what it
    invalidates is the verdicts themselves, which are then about a reviewer nobody
    ships. A tool down for one arm only leaves nothing standing: the arms are two
    different reviewers, and their agreeing or disagreeing says nothing about the two
    models the pass named.
    """
    missing = {model: tools for model, tools in by_arm.items() if tools}
    if not missing:
        return []
    lines = [
        f"  {model}: never reached {', '.join(tools)}" for model, tools in missing.items()
    ]
    if len(missing) == len(by_arm) and len({tuple(t) for t in missing.values()}) == 1:
        lines.append(
            "Both arms lost the same source, so they were compared like with like — but "
            "neither judged as production's reviewer does."
        )
    else:
        lines.append(
            "Only one arm lost a source, so the two arms are two different reviewers and "
            "their agreement is not a comparison of the two models."
        )
    return ["A reviewer tool raised on every call it made:"] + lines
