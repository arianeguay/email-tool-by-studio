"""What a run cost (STU-1011), read off the artifacts the run itself owns (STU-1012).

The report says what a run decided; the token counts say what it spent. They are
split the way billing splits them: fresh input, cache writes, cache reads and
output are four different prices, and a run whose bill is mostly cache writes is
a different problem from one that is mostly output.

Where those counts live is the STU-1012 correction. Studio writes
`.studio/runs/<log>.jsonl` beside the process's cwd, so a run launched from a
worktree left its only record there and a worktree since removed took it along —
seven of the eight most recent reports had no cost at all. Every LLM call a run
makes is now counted where the call was made and written into the artifact that
call belongs to:

- **classify** — the stage-2 calls, into `reports/<run-id>.json` under `cost`;
- **drafts** — the drafting fan-out, onto the `completed` line of
  `logs/drafts/stages.jsonl` (STU-917).

Two legs and not one, because each writer records only what it observed. A
mailbox-full run spends most of its tokens drafting, so a report that also
claimed the drafting leg would be a number no one wrote; and a `drafts` stage
that died still owes an account of what it burned before it did.

`load_events`/`run_cost` stay as the fallback for a run written before this
change, and read the Studio log the old way.
"""

from collections import Counter
from pathlib import Path
from typing import Iterable
import json
import re

from email_classifier.apply.draft_journal import COMPLETED, FAILED, STARTED

# USD per million tokens, Anthropic list price (input, output). A model absent
# here is not priced at zero — `price` raises, because a run silently costed at
# nothing reads as a cheap run.
PRICES_USD_PER_MTOK = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    # Ollama on Athena bills no tokens. Listed rather than defaulted, for the reason above.
    "athena-gemma4": (0.0, 0.0),
    "athena-qwen38": (0.0, 0.0),
    "athena-gptoss": (0.0, 0.0),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

_FIELDS = ("prompt_tokens", "cache_creation_tokens", "cached_input_tokens",
           "completion_tokens", "total_tokens")

# The leg names a run's own artifacts carry. `by_stage` is keyed by these on the
# artifact path and by Studio's stage names on the log fallback — the same
# question asked of two sources, so the same key answers it.
CLASSIFY_LEG = "classify"
DRAFTS_LEG = "drafts"


class UnknownModel(Exception):
    """A model the price table does not carry."""


# The API reports `claude-haiku-4-5-20251001` where a config names the alias
# `claude-haiku-4-5`. The suffix is a release date, so a row per dated release
# would go stale the next time one ships, without a line of this repo changing.
_RELEASE_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _rates(model: str) -> tuple[float, float]:
    """The table's (input, output) rates for a model id, dated or not."""
    try:
        return PRICES_USD_PER_MTOK[_RELEASE_DATE_SUFFIX.sub("", model)]
    except KeyError:
        raise UnknownModel(model) from None


def load_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def find_logs(run_id: str, runs_dirs) -> list[Path]:
    """Every Studio log naming `run_id`.

    The log is written beside the process's cwd, so a run launched from a
    worktree left its log there and nowhere else — and a worktree since removed
    took the only record of its cost with it.
    """
    return sorted(p for d in runs_dirs if Path(d).is_dir()
                  for p in Path(d).glob("*.jsonl") if run_id in p.read_text(errors="ignore"))


def price(counts, model: str) -> float:
    """USD for one model's share of a run."""
    rate_in, rate_out = _rates(model)
    return (counts.get("prompt_tokens", 0) * rate_in
            + counts.get("cache_creation_tokens", 0) * rate_in * CACHE_WRITE_MULTIPLIER
            + counts.get("cached_input_tokens", 0) * rate_in * CACHE_READ_MULTIPLIER
            + counts.get("completion_tokens", 0) * rate_out) / 1e6


def leaf_calls(events) -> list[dict]:
    """The `stage_complete` events that each stand for one LLM call.

    A `call:` or `map:` stage re-reports every token its children spent, so the
    same call appears once per level of nesting; only the deepest level counts
    each call once. `rollup_mismatch` cross-checks that choice against the total
    the root pipeline reported, which is what makes it safe to hold.
    """
    billed = [e for e in events if e["event"] == "stage_complete" and e.get("tokens")]
    if not billed:
        return []
    deepest = max(e.get("depth", 0) for e in billed)
    return [e for e in billed if e.get("depth", 0) == deepest]


# The agent stages one drafting attempt executes, under the names the Studio journal
# gives them — the pair on its own, the only keying a journal has.
DRAFTING_AGENT_STAGES = ("generate-draft", "draft-qa")

# The same for a `classify` child: one agent stage, and the pipeline has no other.
CLASSIFY_AGENT_STAGES = ("classify",)

# The stages a draft costs. Cost per *email* moves with how many conversations earned
# a draft, which is a property of the mailbox; this is the half the pipeline controls,
# and it is what the audit skill's 1.00 ⚠️ threshold is set against (STU-1014).
# Three names for two sources: the Studio log splits drafting into the group's own two
# stages, a run's own artifacts report the pair as the one `drafts` leg. Disjoint, so
# one tuple answers both keyings without either double-counting.
DRAFTING_STAGES = (*DRAFTING_AGENT_STAGES, DRAFTS_LEG)


def journal_calls(events, stages=DRAFTING_AGENT_STAGES) -> dict[str, dict[str, int]]:
    """The LLM calls a Studio journal accounts for, per agent stage (STU-1058).

    A call here is one *attempt*, which is what `leg` cannot count: a run's own
    artifacts see stage executions, and a RALPH retry inside a stage is another call
    nothing outside the engine records. The journal records both halves. `stage_start`
    opens an execution and `stage_retry` fires between attempts and never after the
    last one, so a stage's attempts are its starts plus its retries — which holds for
    an execution the run never finished, where `stage_complete.attempts` is missing
    entirely.

    A skipped stage started without calling anything, so it comes back out.

    Counted at the deepest depth the name appears at that is not the fan-out's own,
    for the reason `leaf_calls` is: a `call:`/`map:` parent re-reports its children
    under its own stage name. Depth alone was the whole rule while the only stages
    measured were drafting's, whose `map:` stage is named `drafts` and never collides
    with `generate-draft` or `draft-qa`. The classify fan-out is a `map:` stage named
    `classify` over a child stage named `classify`, so depth drops the parent only
    while some child ran — and a run whose items all came from the fan-out's resume
    cache leaves the parent's row alone at the deepest depth, counting one call nobody
    made (STU-1246).

    `map_start` names that parent outright — the stage it maps and the depth it runs
    at — so the level comes out by what the journal says it is rather than by a shape
    read off it. A journal too old to carry the event keeps the depth-only rule: the
    reading it loses is a gap that is not there, where a rule that guessed the parent
    from the absence of something would lower the ceiling under a real count and
    report the leg as wrong rather than low.
    """
    rows = [e for e in events if e.get("stage") in stages
            and e["event"] in ("stage_start", "stage_retry", "stage_complete")]
    fanned_out = {(e.get("map"), e.get("depth") or 0) for e in events
                  if e["event"] in ("map_start", "map_complete")}
    counted: dict[str, dict[str, int]] = {}
    for stage in stages:
        mine = [e for e in rows if e["stage"] == stage]
        if not mine:
            continue
        levels = {e.get("depth") or 0 for e in mine} - {d for s, d in fanned_out
                                                        if s == stage}
        if not levels:
            # The fan-out stage ran and no child did: every item came from its resume
            # cache. Zeros rather than no row, because "the stage ran and called
            # nothing" is not "this journal has no such stage".
            counted[stage] = {"executions": 0, "retries": 0, "calls": 0}
            continue
        deepest = max(levels)
        mine = [e for e in mine if (e.get("depth") or 0) == deepest]
        skipped = sum(1 for e in mine
                      if e["event"] == "stage_complete" and e.get("status") == "skipped")
        executions = sum(1 for e in mine if e["event"] == "stage_start") - skipped
        retries = sum(1 for e in mine if e["event"] == "stage_retry")
        counted[stage] = {"executions": executions, "retries": retries,
                          "calls": executions + retries}
    return counted


def drafted(events) -> int | None:
    """Conversations the run left a draft on, off `drafts-post`'s own count.

    None when the run drafted nothing to count — a classify-only run, or one whose
    drafting stage died before reporting. Not zero: a run with no drafts has no cost
    per draft, and a zero here would divide a real bill by a made-up denominator.
    """
    for e in reversed(list(events)):
        if e["event"] == "stage_complete" and e.get("stage") == "drafts-post":
            count = (e.get("output") or {}).get("drafted")
            return count if isinstance(count, int) else None
    return None


def _counts(tokens) -> Counter:
    return Counter({k: v for k, v in tokens.items() if k in _FIELDS})


def _hit_ratio(counts) -> float | None:
    """Share of input tokens served from cache rather than paid at write price."""
    read = counts.get("cached_input_tokens", 0)
    served = read + counts.get("cache_creation_tokens", 0) + counts.get("prompt_tokens", 0)
    return round(read / served, 3) if served else None


def _rollup_mismatch(events, leaf_total: int) -> str | None:
    """Whether the leaf sum disagrees with the run's own reported total.

    A disagreement means the leaf rule counted a call twice or missed one — the
    number is wrong, and saying so beats publishing it.
    """
    roots = [e.get("total_tokens") or 0 for e in events
             if e["event"] == "pipeline_complete" and not e.get("depth")]
    root = max(roots, default=0)
    if not root:
        return "the root pipeline reported no total — leaf sum not cross-checked"
    return None if leaf_total == root else f"leaf sum {leaf_total} != root total {root}"


def _summarize(per_stage: dict[str, dict[str, Counter]], calls: Counter,
               rollup_mismatch: str | None, unpriced: int = 0,
               drafted: int | None = None) -> dict:
    """Tokens, dollars, and where the dollars went, from per-stage per-model counts."""
    totals, by_model = Counter(), {}
    for models in per_stage.values():
        for model, counts in models.items():
            totals.update(counts)
            by_model.setdefault(model, Counter()).update(counts)

    usd = sum(price(counts, model) for model, counts in by_model.items())
    drafting_usd = sum(price(c, m) for stage in DRAFTING_STAGES
                       for m, c in per_stage.get(stage, {}).items())
    cache_write_usd = sum(counts["cache_creation_tokens"] * _rates(model)[0]
                          * CACHE_WRITE_MULTIPLIER / 1e6
                          for model, counts in by_model.items())
    return {
        "models": sorted(by_model),
        "llm_calls": sum(calls.values()),
        # Calls this run made that reported no counts. Non-zero makes `usd` a floor,
        # not the bill — quote it as one or not at all.
        "unpriced_calls": unpriced,
        "tokens": dict(totals),
        "usd": round(usd, 2),
        "by_stage": {
            stage: {
                "calls": calls[stage],
                "total_tokens": sum(c["total_tokens"] for c in models.values()),
                "usd": round(sum(price(c, m) for m, c in models.items()), 2),
                "cache_hit_ratio": _hit_ratio(sum(models.values(), Counter())),
            }
            for stage, models in sorted(per_stage.items())
        },
        "cache_hit_ratio": _hit_ratio(totals),
        "cache_write_share_of_usd": round(cache_write_usd / usd, 3) if usd else None,
        "drafted_conversations": drafted,
        "drafting_usd": round(drafting_usd, 2),
        "usd_per_drafted_conversation": (
            round(drafting_usd / drafted, 3) if drafted else None
        ),
        "rollup_mismatch": rollup_mismatch,
    }


def run_cost(events) -> dict:
    """What a run spent, off its Studio log — the fallback for a pre-STU-1012 run."""
    events = list(events)
    per_stage: dict[str, dict[str, Counter]] = {}
    calls: Counter = Counter()
    for e in leaf_calls(events):
        stage = e.get("stage", "?")
        calls[stage] += 1
        models = per_stage.setdefault(stage, {})
        for model, counts in (e["tokens"].get("by_model") or {}).items():
            models.setdefault(model, Counter()).update(_counts(counts))

    total = sum(sum(c["total_tokens"] for c in models.values())
                for models in per_stage.values())
    return _summarize(per_stage, calls, _rollup_mismatch(events, total),
                      drafted=drafted(events))


def leg(items: Iterable[dict], provider: str | None = None) -> dict:
    """One leg's own spend, in the shape its artifact stores (STU-1012).

    Each entry is a fan-out item as Studio handed it back: the `stages` record its
    child run carried — one entry per child stage, with the `attempts` that stage
    made and the `token_usage` it accumulated — beside the flat sum of the same
    (STU-1064).

    The record is read and the flat sum is not, because the flat sum *is* the
    stages added up and reading both would price the same tokens twice. What the
    record buys is the two numbers a flat sum cannot carry:

    - **a call is an attempt**, so a `generate-draft` that burned three of them
      counts three. The caller no longer supplies a count rebuilt from its own
      journal (STU-1056), and no longer misses the retries that count could not
      see — STU-1058 measured a drafting run that made 32 calls and reported 13;
    - **a failed item still has a record**, so what it burned is priced instead of
      landing whole in `unpriced_calls`. That field says how far below the bill
      `usd` sits, and standing in for every failure is not a measure of anything.

    Every recorded stage is summed, unnamed: a stage that ran no agent reports 0
    attempts (Studio 0.20.2, STU-1245), so a script stage such as `write-draft` buys
    no call. A host below 0.20.2 reports 1 for it, and this leg then counts it as a
    call. Usage is added from every stage too, because a nested `call`/`map` stage
    carries what its own children spent there and nowhere else.

    `call-floor` measures this count against the journal's attempts for the stages
    `journal_calls` names, since a journal line carries no `attempts`.

    An item with no record at all counts as one call — the floor from before
    STU-1064, unpriced when the item reported no flat usage either. Two things
    produce it, a Studio too old to report the record and a throw from before the
    child run started, and nothing here tells them apart: the second really made no
    calls, but a zero would let the first read as a leg that cost nothing, which is
    the reading `unpriced_calls` exists to stop.

    `unrecorded_items` counts them, because one call per item is a count in a
    *different unit* and nothing else in the leg would say so. A drafting item is
    two agent stages per attempt, so a leg from a host below Studio 0.18.0 reports
    roughly half the calls it made with `unpriced_calls` at zero — the old floor
    reading as a measurement, which is the direction STU-912 forbids. Its presence
    is also what dates a leg: absent, the leg predates STU-1210 and counts stage
    executions; present, it counts attempts, and `call-floor` reads it that way.

    An item the resume cache served is not a call this run made and does not belong
    here at all; the caller drops it, where `cached` is.

    Per model and not summed, because the price table is per model and a run that
    escalated under two of them cannot be priced back apart afterwards.

    `provider` is which one the leg's calls actually ran on, recorded here because
    the audit's cache check now reads it (STU-1145) and nothing else in the artifact
    carries it per leg. Recorded by the run rather than re-derived at audit time: the
    answer is a CLI override over an agent field over `defaults.provider`, so a tree
    read weeks later answers for the tree and not for the run. None is a leg written
    before this, and the check treats it as it always did.
    """
    by_model: dict[str, Counter] = {}
    calls = unpriced = unrecorded = 0
    for item in items:
        stages = item.get("stages")
        if isinstance(stages, list) and stages:
            priced = [(stage.get("attempts") or 0, stage.get("token_usage"))
                      for stage in stages]
        else:
            priced = [(1, item.get("token_usage"))]
            unrecorded += 1
        for attempts, usage in priced:
            counts_by_model = (usage or {}).get("by_model") or {}
            calls += attempts
            if attempts and not counts_by_model:
                unpriced += attempts
            for model, counts in counts_by_model.items():
                by_model.setdefault(model, Counter()).update(_counts(counts))
    return {"llm_calls": calls,
            "unpriced_calls": unpriced,
            "unrecorded_items": unrecorded,
            "provider": provider,
            "by_model": {model: dict(counts) for model, counts in sorted(by_model.items())}}


def hit_ratios(
    legs: dict[str, dict],
) -> dict[str, tuple[float | None, int, int, str | None]]:
    """Each leg's cache hit ratio, its calls, what it wrote, and where it ran (STU-1057).

    The audit's cache check wants the ratio alone, and `combine` cannot serve it:
    pricing raises on a model the table does not carry, so a run that escalated
    under an unpriced one would make the check crash instead of reading a ratio
    that is right there in the counts.

    The cache writes travel with it because a ratio of 0.0 has two shapes and the
    ratio alone cannot tell them apart (STU-1080): a prefix written and never read
    back, and a prefix never written at all — which is what a model whose minimum
    cacheable prefix is above this leg's does. Only the first is the leg paying
    cache-write for nothing, and an audit told the wrong one hunts the wrong defect.

    The provider travels with them for a third distinction (STU-1145): under one that
    spawns a process per call, a written prefix cannot be read back by any of them,
    which no pin, contract or model reachable from this repo changes.
    """
    ratios = {}
    for name, spend in legs.items():
        counts = Counter()
        for model_counts in (spend.get("by_model") or {}).values():
            counts.update(_counts(model_counts))
        ratios[name] = (_hit_ratio(counts), spend.get("llm_calls", 0),
                        counts.get("cache_creation_tokens", 0), spend.get("provider"))
    return ratios


def report_leg(report: dict) -> dict | None:
    """The classify leg a report recorded, or None for one written before STU-1012.

    A leg nothing priced is None too: `combine` would answer $0 to a question about
    a run whose calls all died unreported, and the fallback below can still price it
    off the Studio log if that run's log is still around.
    """
    spend = (report.get("cost") or {}).get(CLASSIFY_LEG)
    return spend if spend and spend.get("by_model") else None


def unpriced_classify(report: dict) -> str:
    """Why the classify leg priced nothing — three states, not one line (STU-1167).

    `report_leg` answers None for all three, so the audit printed the same sentence
    for a run whose only call died and a run whose fan-out resumed entirely from
    cache. The first is the failure `unpriced_calls` exists to surface; the second
    costs nothing, reports nothing and is exactly right. Reading run `22e73bc3` took
    opening `.studio/runs/*.jsonl` by hand to tell which had happened.

    The separation is in the leg the run already wrote, because `classify-post` drops
    a resumed item rather than counting it unpriced: a leg every item resumed records
    no calls at all, and a leg whose calls died records them with nothing priced.
    Same shape as STU-1080, which split zero cache hits into its two causes instead
    of printing one sentence for both.
    """
    spend = (report.get("cost") or {}).get(CLASSIFY_LEG)
    if not spend:
        return ("records no classify leg at all — the run predates STU-1012, so no "
                "cache hit ratio was read for it; `cost` falls back to its Studio log")
    if not spend.get("llm_calls"):
        return ("recorded a classify leg that made no calls of its own — every "
                "escalated item was served by the fan-out's resume cache, so there "
                "was nothing to price and no cache hit ratio to read")
    return (f"recorded {spend['llm_calls']} classify call(s) and priced none of them "
            "— they died before reporting usage, so no cache hit ratio was read for "
            "them; `cost` falls back to its Studio log")


def drafts_executions(stage_log: Path, run_id: str) -> tuple[list[dict], int]:
    """The drafting leg's closed executions under `run_id`, and how many closed on
    nothing (STU-1111).

    One run id can carry several executions of the leg, and it must: a run killed
    mid-fan-out resumes with `studio run drafts` under the same id, because the
    per-draft journal scopes by it (STU-1003) and a resume has to keep finding its
    own rows. Taking the last `completed` line therefore reported the resume's
    spend, calls and draft count as the run's, with nothing saying the two
    disagreed — run `fb7889ca` published the 4 conversations its resume drafted as
    the 17 the run did.

    `started` opens an execution and `completed`/`failed` closes it (STU-917), so a
    `started` with no terminal line after it is an execution killed before
    `drafts-post`. What it spent and what it drafted went unrecorded — the
    documented behaviour, not a gap to fill with a number — so it is counted here
    and named by the readers, rather than dropped from the sum in silence.
    """
    if not stage_log.is_file():
        return [], 0
    closed, open_executions = [], 0
    for line in stage_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("run_id") != run_id:
            continue
        if entry.get("status") == STARTED:
            open_executions += 1
        elif entry.get("status") in (COMPLETED, FAILED):
            closed.append(entry)
            open_executions = max(open_executions - 1, 0)
    return closed, open_executions


def drafts_leg(stage_log: Path, run_id: str) -> dict | None:
    """The drafting leg, folded over every execution the run recorded, or None.

    A floor whenever an execution went unrecorded: `drafts_executions` names how
    many, and the readers say so beside the number.
    """
    closed, _ = drafts_executions(stage_log, run_id)
    priced = [e["tokens"] for e in closed if (e.get("tokens") or {}).get("by_model")]
    if not priced:
        return None
    by_model: dict[str, Counter] = {}
    for spend in priced:
        for model, counts in spend["by_model"].items():
            by_model.setdefault(model, Counter()).update(_counts(counts))
    recorded = [spend for spend in priced if "unrecorded_items" in spend]
    return {"llm_calls": sum(spend.get("llm_calls", 0) for spend in priced),
            "unpriced_calls": sum(spend.get("unpriced_calls", 0) for spend in priced),
            # Only when every execution carried it: absent on one is a leg counting
            # stage executions, and a total that hid it would date the run wrong.
            **({"unrecorded_items": sum(spend["unrecorded_items"] for spend in recorded)}
               if len(recorded) == len(priced) else {}),
            "by_model": {model: dict(counts) for model, counts in sorted(by_model.items())}}


def drafts_denominator(stage_log: Path, run_id: str) -> int | None:
    """Conversations the run's executions say it drafted (STU-1014), or None.

    Off the same lines as the spend, so the two halves of USD per drafted
    conversation are never read from two runs. None rather than 0 for a run that
    drafted nothing: a real bill over a denominator the run never had is a
    made-up number, the same reason an unpriced model raises.

    None too when an execution left no terminal line: the drafts it made are not
    in this file, so the sum is short by an unknown amount and every figure over
    it would be a per-conversation cost the run never had (STU-1111).
    """
    closed, unrecorded = drafts_executions(stage_log, run_id)
    if unrecorded:
        return None
    count = sum(e["drafted"] for e in closed if isinstance(e.get("drafted"), int))
    return count or None


def combine(legs: dict[str, dict], drafted: int | None = None) -> dict:
    """What a run spent, over the legs its own artifacts recorded.

    No `rollup_mismatch` to report: nothing here is a roll-up. Each call was
    counted once, where it was made, so there is no second total to cross-check
    the sum against — the Studio log's leaf rule is what needed one. What that
    cross-check was guarding against is instead named outright, by the legs
    themselves: `unpriced_calls` is the undercount, and it is measured, not
    inferred from a disagreeing total.
    """
    per_stage = {name: {model: Counter(counts)
                        for model, counts in (spend.get("by_model") or {}).items()}
                 for name, spend in legs.items()}
    calls = Counter({name: spend.get("llm_calls", 0) for name, spend in legs.items()})
    unpriced = sum(spend.get("unpriced_calls", 0) for spend in legs.values())
    return _summarize(per_stage, calls, None, unpriced, drafted)
