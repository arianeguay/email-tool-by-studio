#!/usr/bin/env python3
"""Score two drafting models on the conversations a finished run drafted (STU-1073).

Authors every conversation in a Studio run journal's fan-out once per model per repeat,
through `draft-bakeoff` — `email-draft` with the mailbox write stood down — and scores
each arm on what the same Opus reviewer accepted, and on what it spent getting there.

    scripts/draft_bakeoff.py .studio/runs/<run>.jsonl \
        --baseline claude-opus-4-8 --candidate claude-sonnet-5

Both arms author live. Nothing is written to the mailbox, but the drafter's reading
tools are production's — the site search, the precedent search over real sent mail, the
club memory — so this needs the credentials a drafting run needs, and the detector
behind them.

`--provider anthropic` by default, which is what staging serves under: `claude-code`
builds its system prompt from the cwd, so every arm would carry this repo's CLAUDE.md
into the prompt and into the bill (STU-1013), and a bake-off deciding a production pin
would be measuring the harness.

A reviewer or drafter tool that raised on every call it made is named per arm in the
summary and in the JSON, and does not change the exit code (STU-1147). The journal
lookup is `qa_replay`'s rather than a second one here: two harnesses reading tool health
two different ways would be measuring two different things.

Exits non-zero when the candidate reaches fewer accepted drafts than the baseline —
the decision this exists to make. Two arms that tie exit 0: a cheaper drafter that
holds the line is the trade this licenses. It also exits non-zero when the baseline
settled nothing of its own, which is a pass that measured nothing rather than one that
found the candidate wanting.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

from email_classifier import draft_bakeoff, qa_replay
from email_classifier.qa_bakeoff import items_from_journal
from email_classifier import deployment

REPO_ROOT = deployment.ROOT

_PIPELINE = "draft-bakeoff"
_DRAFT_STAGE = "generate-draft"
_QA_STAGE = "draft-qa"
_MODEL_ENV = "DRAFT_MODEL"
_PROVIDER_ENV = "DRAFT_PROVIDER"
_DEFAULT_PROVIDER_ENV = "STUDIO_DEFAULT_PROVIDER"
_TIMEOUT = 1800

_OUT = Path("logs/draft-bakeoff.json")


def default_out(limit: int | None) -> Path:
    """A limited pass never lands on the full bake-off's name (STU-1042)."""
    return _OUT if limit is None else _OUT.with_stem(f"{_OUT.stem}-limit{limit}")


def _authored(thread_id: str, stdout: str, unreachable: tuple[str, ...] = (),
              empty_store: tuple[str, ...] = ()) -> draft_bakeoff.Authored:
    """One authoring run, read off the record `studio run --json` prints.

    A group appends a `StageRun` per stage per iteration, so the reviewer's verdicts in
    order are the run's iterations in order, and each stage's `token_usage` already
    carries its own RALPH retries. A run that reached the reviewer no times reports no
    verdict, which `score` counts as an error rather than as a rejection — a dead run
    and a bad draft are not the same finding.
    """
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        return draft_bakeoff.Authored(thread_id, (), 0, {}, unreachable, empty_store)

    stages = [s for s in record.get("stages") or [] if isinstance(s, dict)]
    verdicts = tuple(
        stage["output"]["verdict"]
        for stage in stages
        if stage.get("stage_name") == _QA_STAGE
        and isinstance(stage.get("output"), dict)
        and isinstance(stage["output"].get("verdict"), str)
    )
    attempts = sum(1 for stage in stages if stage.get("stage_name") == _DRAFT_STAGE)

    by_model: dict[str, dict[str, int]] = {}
    for stage in stages:
        for model, counts in ((stage.get("token_usage") or {}).get("by_model") or {}).items():
            totals = by_model.setdefault(model, {})
            for field, value in counts.items():
                totals[field] = totals.get(field, 0) + value
    return draft_bakeoff.Authored(
        thread_id, verdicts, attempts, by_model, unreachable, empty_store
    )


def _invocation(item_path: Path, model: str, provider: str | None,
                arm_provider: str | None) -> tuple[list[str], dict[str, str]]:
    """The `studio run` command and env for one arm.

    `--provider` moves every agent in the run, reviewer included. An arm on its own
    provider (a local model judged by the hosted reviewer) pins the drafter and sends
    the reviewer to the shared provider through `defaults.provider`, not through its
    agent file, which the QA gate fingerprints.
    """
    command = ["studio", "run", _PIPELINE, "--input-file", str(item_path), "--json"]
    env = {_MODEL_ENV: model}
    if arm_provider:
        env[_PROVIDER_ENV] = arm_provider
        if provider:
            env[_DEFAULT_PROVIDER_ENV] = provider
    elif provider:
        command += ["--provider", provider]
    return command, env


def _author(item: dict, model: str, provider: str | None, workdir: Path,
            run_id: str, arm_provider: str | None = None,
            timeout: int = _TIMEOUT) -> draft_bakeoff.Authored:
    item_path = workdir / "item.yaml"
    # The item's own run id is replaced, not dropped, for `qa_bakeoff`'s reason: Studio
    # logs the input, so a replay carrying it makes its journal one more log naming that
    # report, and `audit_run.py cost` then refuses the report for ambiguity.
    item_path.write_text(
        yaml.safe_dump({**item, "run_id": run_id}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    command, arm_env = _invocation(item_path, model, provider, arm_provider)
    try:
        result = subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, **arm_env},
        )
    except subprocess.TimeoutExpired:
        print(f"  {model}: TIMEOUT after {timeout}s", file=sys.stderr)
        return draft_bakeoff.Authored(item["thread_id"], (), 0, {})

    _, unreachable, empty_store = qa_replay.health_in(result.stdout, REPO_ROOT)
    authored = _authored(item["thread_id"], result.stdout, unreachable, empty_store)
    if authored.errored:
        # stderr only: stdout is the run record, and it echoes the item back (STU-1010).
        print(f"  {model}: no verdict | rc={result.returncode}", file=sys.stderr)
    return authored


def _append(trace: Path, journal: Path, model: str, repeat: int,
            run: draft_bakeoff.Authored) -> None:
    with trace.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "journal": str(journal), "model": model, "repeat": repeat,
            "thread_id": run.thread_id, "verdicts": list(run.verdicts),
            "attempts": run.attempts, "by_model": run.by_model,
            "unreachable": list(run.unreachable),
            "empty_store": list(run.empty_store),
        }) + "\n")


def _load_trace(trace: Path, journal: Path) -> dict:
    """Runs an earlier pass already paid for, by (model, repeat, thread).

    Only rows naming the same journal: a drafting item is the whole prompt, so a row
    from another corpus is another question's answer, and reusing it would publish a
    verdict on drafts these two models were never asked for.

    Nothing checks that the agents have not changed since. `--fresh` is the answer to
    that, and re-authoring is cheap to ask for beside the pass it saves.
    """
    if not trace.is_file():
        return {}
    done = {}
    for line in trace.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("journal") != str(journal):
            continue
        done[(row["model"], row["repeat"], row["thread_id"])] = draft_bakeoff.Authored(
            row["thread_id"], tuple(row["verdicts"]), row["attempts"], row["by_model"],
            # A row from before STU-1147 cannot say its tools answered, and unrecorded
            # is not answered — the same direction `qa_replay.UNRECORDED` exists for.
            tuple(row.get("unreachable", [qa_replay.UNRECORDED])),
            # A row from before STU-1235 has no opinion on this at all: it predates the
            # distinction, not the store being empty, so it says nothing rather than
            # excusing a raise it never separated out.
            tuple(row.get("empty_store", ())))
    return done


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path,
                        help="Studio run journal of a finished drafting run")
    parser.add_argument("--baseline", default="claude-opus-4-8")
    parser.add_argument("--candidate", default="claude-sonnet-5")
    parser.add_argument("--repeats", type=int, default=2,
                        help="Runs per arm per conversation. Under 2 there is no noise "
                             "floor to read a gap between the models against.")
    parser.add_argument("--limit", type=int, help="Author only the first N conversations.")
    parser.add_argument("--provider", default="anthropic",
                        help="Studio provider both arms run under. Empty for the "
                             "pipeline default.")
    parser.add_argument("--candidate-provider",
                        help="Provider for the candidate's drafter only; the reviewer "
                             "stays on --provider. For a local model judged by the "
                             "hosted reviewer, e.g. ollama.")
    parser.add_argument("--timeout", type=int, default=_TIMEOUT,
                        help="Seconds one conversation may take, per arm and repeat. "
                             "A local model on Athena needs more than the default.")
    parser.add_argument("--fresh", action="store_true",
                        help="Author everything again instead of reusing the runs the "
                             "trace beside the output already records.")
    parser.add_argument("-o", "--out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.limit is not None and args.limit < 1:
        print("[ERROR] --limit must be at least 1: 0 would author nothing while naming "
              "the output file after a limit", file=sys.stderr)
        return 1

    if args.baseline == args.candidate:
        print("[ERROR] both arms name the same model: that measures the noise floor "
              "twice and nothing else", file=sys.stderr)
        return 1

    items = items_from_journal(args.journal.read_text(encoding="utf-8").splitlines())
    thread_ids = sorted(items)
    if args.limit:
        thread_ids = thread_ids[: args.limit]
    if not thread_ids:
        print(f"[ERROR] no drafting items in {args.journal}", file=sys.stderr)
        return 1

    print(f"[draft-bakeoff] {len(thread_ids)} conversation(s) | {args.repeats} repeat(s) "
          f"per arm | {args.baseline} vs {args.candidate}", file=sys.stderr)

    bakeoff_id = str(uuid.uuid4())
    print(f"[draft-bakeoff] run id {bakeoff_id}", file=sys.stderr)

    out = args.out or default_out(args.limit)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Every run appended as it finishes, and read back on the next pass. A full pass is
    # hours of live authoring under two models: a summary written only at the end leaves
    # a killed one having bought nothing — the same reason `drafts-pre` opens its stage
    # log before touching the mailbox (STU-917) — and re-authoring what it did buy pays
    # twice for one measurement.
    trace = out.with_suffix(".runs.jsonl")
    done = {} if args.fresh else _load_trace(trace, args.journal)
    if done:
        print(f"[draft-bakeoff] reusing {len(done)} run(s) from {trace}", file=sys.stderr)

    models = (args.baseline, args.candidate)
    authored: dict[str, list[dict[str, draft_bakeoff.Authored]]] = {
        model: [{} for _ in range(args.repeats)] for model in models
    }
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        # Conversation-major and both arms together, the way `qa_bakeoff` judges: an
        # arm-major pass that is interrupted has authored one whole arm and none of the
        # other, which is no measurement at all however far it got.
        for repeat in range(1, args.repeats + 1):
            for n, thread_id in enumerate(thread_ids, 1):
                for model in models:
                    run = done.get((model, repeat, thread_id))
                    where = f"{model} repeat {repeat} {n}/{len(thread_ids)} {thread_id}"
                    if run is not None:
                        print(f"[draft-bakeoff] {where}: reused", file=sys.stderr)
                    else:
                        print(f"[draft-bakeoff] {where}", file=sys.stderr)
                        arm_provider = (args.candidate_provider
                                        if model == args.candidate else None)
                        run = _author(items[thread_id], model, args.provider, workdir,
                                      bakeoff_id, arm_provider, args.timeout)
                        print(f"  verdicts: {', '.join(run.verdicts) or '(none)'}",
                              file=sys.stderr)
                        _append(trace, args.journal, model, repeat, run)
                    authored[model][repeat - 1][thread_id] = run

    arms = [draft_bakeoff.Arm(model, tuple(authored[model])) for model in models]
    result = draft_bakeoff.compare(arms[0], arms[1], thread_ids)
    print(draft_bakeoff.render(result))

    payload = draft_bakeoff.as_dict(result) | {
        "journal": str(args.journal),
        "run_id": bakeoff_id,
        "repeats": args.repeats,
        "provider": args.provider,
    }
    if args.limit:
        payload["limit"] = args.limit
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[draft-bakeoff] wrote {out}", file=sys.stderr)

    return 0 if result.measured and not result.candidate_is_worse else 1


if __name__ == "__main__":
    raise SystemExit(main())
