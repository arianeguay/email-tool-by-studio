"""Which conversation ran which stage, in a journal the fan-out wrote at once (STU-1112).

A drafting run's Studio journal is a single file every conversation writes into
concurrently, all under the **root** run id. `stage_complete` carries `stage`, `status`,
`output` and `depth` and nothing that names a conversation, so reading the file top to
bottom gives an order that is true of the file and false of any one thread. A table
traced that way put four `draft-qa` passes on one conversation whose group is capped at
three, and the miscount was a premise in STU-1104 before anyone caught it.

The attribution is in the journal, in the two fields nothing had read together:
`stage_context` carries the run id of the **child** run whose stage it is, and
`map_item_complete` carries that same id beside the item's `map` and `index`. A child's
stages are strictly sequential, so its execution of a stage opens at its `stage_context`
and closes at the next event that child emits — its next `stage_context`, or its
`pipeline_complete`.

One case is left that file order does not decide: a `stage_complete` for a stage two
children both have open. It is settled by watching which child moves on. A candidate
whose own next event falls before the *next* completion of that stage cannot be waiting
on that later one, so the completion in hand is its. Exactly one candidate forced that
way is the answer; anything else is reported unattributed rather than guessed. A
mis-paired verdict attributes one conversation's approval to another's body and the table
reads exactly as right as a correct one, which is the whole reason this exists — and
STU-912's rule applies: a reading that could not be made must not look like one that was.

Measured 2026-09-01 on the eight multi-conversation journals this repo and the staging
host hold: of 112 `generate-draft` and `write-draft` completions, the 110 whose fan-out
item completed attribute to the child whose item names the thread their own output names
— no mismatch. Those two stages name their thread and so can be checked against;
`draft-qa` names nothing, which is why the rule has to exist at all. The naive rule —
take the child named by the next id-bearing event — gets 109 of those 110, and is wrong
on the one where a neighbour's verdict lands between a completion and the next context of
its own child.

Pure: the journal is the caller's.
"""

from dataclasses import dataclass

# The `email-draft` group, in the order one iteration runs them. `generate-draft` heads a
# pass, so counting it counts the group's iterations without attributing `group_iteration`
# events, which carry the root run id like everything else.
DRAFT_STAGES = ("generate-draft", "write-draft", "draft-qa")
_ITEM_STAGE = "drafts-pre"

# The events that name a child run: a stage opening inside it, and its own end.
_CHILD_EVENTS = ("stage_context", "pipeline_complete")


@dataclass(frozen=True)
class Execution:
    """One stage execution, and the child run it belongs to when that is decidable."""

    position: int
    ts: str
    stage: str
    status: str
    output: dict
    child: str | None

    @property
    def verdict(self) -> str:
        """What the stage decided, which for `draft-qa` is not its status.

        A rejected review completes with `status: rejected`, but an approval and a read
        the contract could not parse both complete `success` — the verdict is in the
        output.
        """
        return str(self.output.get("verdict") or self.status)


@dataclass(frozen=True)
class Conversation:
    """One fan-out item's whole drafting sequence."""

    child: str
    item: tuple[str, int] | None
    thread_id: str
    executions: tuple[Execution, ...]

    @property
    def passes(self) -> tuple[tuple[Execution, ...], ...]:
        """The group's iterations, each the stages that ran under one `generate-draft`.

        A run cut off mid-stage leaves a pass short of its verdict, and a journal whose
        first drafting event is not a `generate-draft` — a replay, a resumed run — leaves
        a headless first pass. Both are kept as they are rather than padded: the point of
        the sequence is what the conversation actually ran.
        """
        out: list[list[Execution]] = []
        for execution in self.executions:
            if execution.stage == DRAFT_STAGES[0] or not out:
                out.append([])
            out[-1].append(execution)
        return tuple(tuple(p) for p in out)


def attribute(events: list[dict]) -> list[Execution]:
    """Every `stage_complete` in the journal, each tied to the child run that ran it.

    General over stages, not just the drafting ones: `classify` fans out the same way and
    the rule does not know the difference. `child` is None where the journal does not
    decide it.
    """
    positions: dict[str, list[int]] = {}
    for i, event in enumerate(events):
        if event.get("event") == "stage_complete":
            positions.setdefault(event.get("stage"), []).append(i)
    # Where the next completion of this same stage falls: the deadline a candidate has to
    # beat to be the one this completion belongs to.
    next_same: dict[int, int] = {}
    for stage_positions in positions.values():
        for here, after in zip(stage_positions, stage_positions[1:] + [len(events)]):
            next_same[here] = after

    open_stage: dict[str, str] = {}
    executions: list[Execution] = []
    for i, event in enumerate(events):
        kind = event.get("event")
        if kind == "stage_context":
            open_stage[event.get("run_id")] = event.get("stage")
        elif kind == "pipeline_complete":
            open_stage.pop(event.get("run_id"), None)
        elif kind == "stage_complete":
            stage = event.get("stage")
            candidates = [c for c, s in open_stage.items() if s == stage]
            if len(candidates) > 1:
                candidates = [c for c in candidates
                              if _next_own_event(events, i, c) < next_same[i]]
            child = candidates[0] if len(candidates) == 1 else None
            open_stage.pop(child, None)
            output = event.get("output")
            executions.append(Execution(
                position=i,
                ts=str(event.get("ts") or ""),
                stage=str(stage or ""),
                status=str(event.get("status") or ""),
                output=output if isinstance(output, dict) else {},
                child=child,
            ))
    return executions


def _next_own_event(events: list[dict], after: int, child: str) -> int:
    for j in range(after + 1, len(events)):
        event = events[j]
        if event.get("event") in _CHILD_EVENTS and event.get("run_id") == child:
            return j
    return len(events)


def _items(events: list[dict]) -> tuple[dict[str, tuple[str, int]], dict[int, str]]:
    """Child run id -> the fan-out item it ran, and item index -> the thread it carries.

    The item is keyed on `(map, index)` and never on the index alone: a `mailbox-full`
    runs two fan-outs, and index 0 of `classify` is not index 0 of `drafts`.
    """
    child_item: dict[str, tuple[str, int]] = {}
    threads: dict[int, str] = {}
    for event in events:
        if event.get("event") == "map_item_complete":
            child_item[event.get("run_id")] = (event.get("map"), event.get("index"))
        elif (event.get("event") == "stage_complete"
              and event.get("stage") == _ITEM_STAGE
              and isinstance(event.get("output"), dict)):
            threads = {i: p.get("thread_id", "")
                       for i, p in enumerate(event["output"].get("pending", []))}
    return child_item, threads


def conversations(events: list[dict]) -> tuple[list[Conversation], list[Execution]]:
    """The drafting sequence of each conversation, and what could not be placed.

    A conversation is one child run. Its thread comes from the stages that name it —
    `generate-draft` and `write-draft` both do — and falls back to the thread its fan-out
    item carries, which is the only source a run whose drafting died before its first
    completion has.
    """
    child_item, threads = _items(events)
    executions = [e for e in attribute(events) if e.stage in DRAFT_STAGES]
    unattributed = [e for e in executions if e.child is None]

    grouped: dict[str, list[Execution]] = {}
    for execution in executions:
        if execution.child is not None:
            grouped.setdefault(execution.child, []).append(execution)

    out = []
    for child, own in grouped.items():
        item = child_item.get(child)
        named = next((e.output["thread_id"] for e in own if e.output.get("thread_id")), "")
        fallback = threads.get(item[1], "") if item else ""
        out.append(Conversation(child, item, named or fallback, tuple(own)))
    return out, unattributed


def render(found: list[Conversation], unattributed: list[Execution]) -> str:
    """The table a person reads: one block per conversation, its passes in order."""
    lines = [f"=== {len(found)} conversation(s), "
             f"{sum(len(c.executions) for c in found)} attributed stage execution(s) ==="]
    for conversation in found:
        item = (f"{conversation.item[0]} item {conversation.item[1]}"
                if conversation.item else "no fan-out item")
        lines.append(f"  {conversation.thread_id or '(thread unnamed)'}  "
                     f"({item}, run {conversation.child})")
        for number, group in enumerate(conversation.passes, start=1):
            stages = " | ".join(f"{e.stage} {e.verdict}" for e in group)
            lines.append(f"    pass {number}  {group[0].ts}  {stages}")
    if unattributed:
        lines.append(f"UNATTRIBUTED — {len(unattributed)} drafting stage execution(s) "
                     "the journal does not tie to a conversation; the sequences above "
                     "are short by that much:")
        for execution in unattributed:
            lines.append(f"    line {execution.position}  {execution.ts}  "
                         f"{execution.stage} {execution.verdict}")
    return "\n".join(lines)

