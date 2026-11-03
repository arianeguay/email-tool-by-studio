"""Drive `draft-qa-replay` on one already-written draft, and read the verdict back.

Extracted from the bake-off script (STU-1014) when a second caller appeared (STU-1078):
`make qa-bakeoff` scores two judges on drafts a run recorded, `make qa-gate` replays the
committed corpus of drafts the reviewer must keep rejecting. Both ask the pipeline the
same question, so both ask it through here — two callers reading a verdict two different
ways would be measuring two different things. `make qa-erosion` is the third (STU-1104),
and it asks the same question of a group rather than a stage, so the pipeline is an
argument and the reader returns every pass rather than the one.

Live either way. Nothing is drafted and no mailbox is written, but the reviewer's own
tools run, so this needs the credentials a drafting run needs.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from email_classifier import club_memory

PIPELINE = "draft-qa-replay"
EROSION_PIPELINE = "draft-qa-erosion"
STAGE = "draft-qa"
MODEL_ENV = "DRAFT_QA_MODEL"
DRAFT_ENV = "EMAIL_CLASSIFIER_QA_REPLAY_DRAFT"
TIMEOUT = 600

# What a caller records when it could not read a verdict. Not a verdict the reviewer can
# return, so it can never be mistaken for one: the bake-off counts it as a disagreement
# and the gate as a failure, rather than either dropping the draft.
ERROR = "error"

# Where Studio writes a run's journal, relative to the cwd it was launched in. The
# stdout record cannot answer which tools answered — `StageRun` carries no tool calls,
# only a count of the successful ones on each agent run — so the journal is the only
# place a caller can read it back from.
JOURNAL_DIR = ".studio/runs"

# What stands in the unreachable list when the journal that would name this run's tool
# calls is not there. Not a tool name, and not silence: unrecorded is not the same as
# answered, and the caller asking is asking exactly that.
UNRECORDED = "(no run journal)"

# Tools whose raise is explained by the tree the run stood in rather than by a lost
# source (STU-1235): `org_memory-read_memory` reads a name off disk and raises
# `FileNotFoundError` for one that is not there, and when `club-memory/`'s own index is
# empty *no* name is there — the store said so before the call, not after. That is a
# fact about this run's checkout, checked the same way the reviewer's own prompt reads
# it (`club_memory.entries`), not re-derived from anything the run reported.
#
# `org_web-search_site` and `mail_precedents-search_mail` are absent on purpose, for
# the reason `qa_live_sources.SILENCED_TOOLS` gives: both define an empty answer
# (`matches: []`) and never raise on it, so a raise from either is still the STU-1139
# case with nothing left to distinguish.
_EMPTY_STORE_CHECKS: dict[str, Callable[[Path], bool]] = {
    "org_memory-read_memory": lambda cwd: not club_memory.entries(cwd / "club-memory"),
}


def _empty_store(tool: str, cwd: Path) -> bool:
    check = _EMPTY_STORE_CHECKS.get(tool)
    return check is not None and check(cwd)


@dataclass(frozen=True)
class Judgement:
    """What one replay produced: a verdict per pass, and how its tools fared.

    Three facts rather than one, because they are three findings with three repairs. A
    draft that stopped being rejected is about the reviewer's judgement and a human
    reads it. A tool that raised on every call while its backing store held something
    is about the run: the reviewer that judged had one source fewer than production's,
    whatever it went on to conclude, so its verdict answers neither question
    (STU-1139). A tool that raised on every call *because* its store was verifiably
    empty in this tree is neither: nothing was lost, and calling it anyway (or
    declining to, per production's own reviewer) is model behaviour a bake-off exists
    to show (STU-1235) — reported beside the verdict, never folded into `unreachable`.

    One type for both shapes rather than two, so no caller can read a verdict without
    the tools behind it in reach (STU-1147): a single-stage replay lands one verdict
    here and a group lands one per pass, which is the relation `verdict_of` already has
    to `verdicts_of`.
    """

    verdicts: tuple[str, ...]
    unreachable: tuple[str, ...] = ()
    # Every tool the run called at all, answered or not. `unreachable` is a subset of
    # it, and the two say different things: a caller asking whether a source decided a
    # verdict has to know the reviewer reached for it before silencing it means
    # anything (STU-1114).
    called: tuple[str, ...] = ()
    # Tools that raised on every call, excused: the store behind them was verifiably
    # empty in this tree, so nothing was lost (STU-1235). Disjoint from `unreachable`,
    # not a subset of it — a tool is one or the other, never both.
    empty_store: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        """The cold verdict, or `ERROR`. The head either way: a group appends a stage
        run per iteration, so its first entry is the pass a single-stage replay makes."""
        return self.verdicts[0] if self.verdicts else ERROR


def tool_calls(journal: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The tools a run called, and those every one of whose calls raised.

    Both in the order they were first called, and the second a subset of the first.

    Read off `tool_call_complete` and not off the `tool_calls` array that
    `stage_complete` carries: the engine attaches that array only when the stage
    succeeded, so it is absent in exactly the case this exists to catch — a tool that
    raises every time leaves the contract's tool-call floor unmet, since the validator
    counts successful calls only, and takes the stage down with it.

    Unreachable is every call and not any: a tool that answered once was reachable and
    the reviewer had it. A tool nobody called is not unreachable either — declining to
    search is a judgement, not a failure, and production's reviewer declines too. That
    same silence is why the called list is worth returning beside it: silencing a tool
    the reviewer never reached for cannot have moved anything (STU-1114).
    """
    called: list[str] = []
    answered: set[str] = set()
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "tool_call_complete":
            continue
        tool = event.get("tool")
        if not isinstance(tool, str):
            continue
        if tool not in called:
            called.append(tool)
        # A failure is the presence of `error`; the event carries no success flag.
        if "error" not in event:
            answered.add(tool)
    return tuple(called), tuple(tool for tool in called if tool not in answered)


def health_in(
    stdout: str, cwd: Path
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """What this run called, what it lost, and what it asked a store this tree says is
    empty.

    The journal is named by the first 8 characters of the run id, which is the one
    handle the stdout record gives us. No record at all — a timeout, a crash before the
    engine ran — returns nothing called on purpose: the verdict is already `ERROR` and
    the caller fails on that, where a second name for the same failure would only say
    it twice.

    The split against `_EMPTY_STORE_CHECKS` happens here and not in `tool_calls`: a
    journal names which tools raised, `cwd` says whether that raise was expected, and
    only this function has both. `UNRECORDED` is neither lost nor excused — it never
    reaches the check, since there is no journal to have raised anything at all.

    Public because `draft_bakeoff` drives its own pipeline and needs the same lookup
    (STU-1147). Written a second time there it would be a second definition of tool
    health, which is the thing this module exists to stop happening to a verdict. It
    reads whatever agents that pipeline ran, so under `draft-bakeoff` it answers for
    the drafter's tools as well as the reviewer's.
    """
    try:
        run_id = json.loads(stdout).get("id")
    except json.JSONDecodeError:
        return (), (), ()
    if not isinstance(run_id, str):
        return (), (), ()
    journals = sorted((cwd / JOURNAL_DIR).glob(f"*-{run_id[:8]}.jsonl"))
    if len(journals) != 1:
        return (), (UNRECORDED,), ()
    called, raised = tool_calls(journals[0])
    empty = tuple(tool for tool in raised if _empty_store(tool, cwd))
    unreachable = tuple(tool for tool in raised if tool not in empty)
    return called, unreachable, empty


def unreachable_in(stdout: str, cwd: Path) -> tuple[str, ...]:
    """Only the unreachable half, for a caller that asks nothing else of the journal."""
    return health_in(stdout, cwd)[1]


def empty_store_in(stdout: str, cwd: Path) -> tuple[str, ...]:
    """Only the excused half: tools that raised because their store is empty here."""
    return health_in(stdout, cwd)[2]


def verdicts_of(stdout: str) -> list[str]:
    """Every verdict the reviewer returned in a run record, in the order it returned them.

    A rejected draft ends the pipeline as `rejected`; the stage that said so still
    carries its output, and that output is the measurement.

    One entry per stage execution, because that is what a group leaves: the orchestrator
    appends a stage run per iteration rather than overwriting one, so a three-pass group
    lands here as three verdicts. A single-stage replay leaves exactly one, which is why
    `verdict_of` can be this list's head.
    """
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        return []
    verdicts = []
    for stage in record.get("stages") or []:
        if isinstance(stage, dict) and stage.get("stage_name") == STAGE:
            output = stage.get("output")
            if isinstance(output, dict) and isinstance(output.get("verdict"), str):
                verdicts.append(output["verdict"])
    return verdicts


def verdict_of(stdout: str) -> str:
    """The reviewer's verdict in a single-stage run record, or `ERROR`."""
    verdicts = verdicts_of(stdout)
    return verdicts[0] if verdicts else ERROR


def _replay(
    item: dict,
    draft: dict,
    workdir: Path,
    run_id: str,
    cwd: Path,
    model: str | None,
    label: str,
    pipeline: str,
    extra_env: dict[str, str] | None = None,
) -> Judgement:
    """Run one replay and return the reviewer's verdicts, one per pass it made.

    `model` overrides the pin on `draft-reviewer`, which is what the bake-off flips; left
    unset, the reviewer runs on the model production names, which is what the gate and the
    erosion measurement ask about. `pipeline` picks the shape asked of it — one cold stage,
    or the group whose later passes STU-1104 measures.

    The item's own run id is replaced, not dropped: Studio logs the input, so a replay
    carrying it makes its journal one more log naming that report, and `audit_run.py cost`
    then refuses the report for ambiguity. Swapping one opaque id for another leaves the
    reviewer's `input` block the shape production gives it.

    `extra_env` is what a caller changes about the world the reviewer's tools run in —
    STU-1114 points `PYTHON` at a shim so two of them answer with nothing. Passed rather
    than set on this process: a caller that mutated `os.environ` around the call would be
    a second way of building a replay's environment, and only one of them would be here.
    """
    item_path = workdir / "item.yaml"
    draft_path = workdir / "draft.json"
    item_path.write_text(
        yaml.safe_dump({**item, "run_id": run_id}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    draft_path.write_text(json.dumps(draft, ensure_ascii=False), encoding="utf-8")

    env = {**os.environ, DRAFT_ENV: str(draft_path), **(extra_env or {})}
    if model:
        env[MODEL_ENV] = model
    prefix = f"  {label}: " if label else "  "
    try:
        result = subprocess.run(
            ["studio", "run", pipeline, "--input-file", str(item_path), "--json"],
            cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"{prefix}TIMEOUT after {TIMEOUT}s", file=sys.stderr)
        return Judgement(())
    verdicts = verdicts_of(result.stdout)
    if not verdicts:
        # stderr only: stdout is the run record, and it echoes the item back (STU-1010).
        print(f"{prefix}no verdict | rc={result.returncode}", file=sys.stderr)
    called, unreachable, empty_store = health_in(result.stdout, cwd)
    return Judgement(tuple(verdicts), unreachable, called, empty_store)


def judge(
    item: dict,
    draft: dict,
    workdir: Path,
    run_id: str,
    cwd: Path,
    model: str | None = None,
    label: str = "",
    extra_env: dict[str, str] | None = None,
) -> Judgement:
    """Replay one draft past the reviewer once and read back what it said, and had."""
    return _replay(item, draft, workdir, run_id, cwd, model, label, PIPELINE, extra_env)


def judge_passes(
    item: dict,
    draft: dict,
    workdir: Path,
    run_id: str,
    cwd: Path,
    label: str = "",
) -> Judgement:
    """Replay one draft through the group and return its verdict at each pass (STU-1104).

    Shorter than `max_iterations` whenever the reviewer approved: the group ends on an
    approval, so the last entry is the flip and its index is the K it took. No `model`
    argument — the question is whether *production's* reviewer erodes, and a model the
    production pin does not name cannot answer it.
    """
    return _replay(item, draft, workdir, run_id, cwd, None, label, EROSION_PIPELINE)
