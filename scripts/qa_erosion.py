#!/usr/bin/env python3
"""Measure whether the draft/QA group erodes the reviewer across iterations (STU-1104).

    scripts/qa_erosion.py [--corpus eval/draft-qa-rejections.json] [--repeats 2]

Replays each frozen known-bad draft through `draft-qa-erosion` — the same echo stage
`make qa-gate` uses, put inside `email-draft`'s group — and reports the verdict at every
pass the group reached. The draft never changes, so the only thing that moves between
passes is the reviewer's own critique coming back as `group_feedback`.

A measurement, not a gate: this exits 0 whatever it finds. A flip is a human's to read,
and the fix — a rule the feedback cannot outweigh, a cap on iterations, judging the final
draft cold — is the second question, decided by what the number says. A reviewer tool
that raised on every call it made is named in the table and in the JSON, and does not
change the exit code either (STU-1147): the reason `make qa-gate` exits 4 on one is the
record it would otherwise stamp, and nothing reads this file back as an assertion.

No `--model`, for the reason `qa_gate.py` has none: the question is whether *production's*
reviewer erodes. `--repeats` defaults to 2 because a single flip is inside the noise floor
STU-1014 measured (21 of 22 first attempts, Opus judging the same drafts twice), so one
pass could not tell a flip from a draw.
"""

import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

from email_classifier import (qa_erosion, qa_gate, qa_provider, qa_replay,
                              qa_studio_version)
from email_classifier import deployment

REPO_ROOT = deployment.ROOT

_CORPUS = REPO_ROOT / "eval" / "draft-qa-rejections.json"
_DRAFT_CONTRACT = REPO_ROOT / ".studio" / "contracts" / "qa-replay-draft.contract.yaml"
_PIPELINE = REPO_ROOT / ".studio" / "pipelines" / "draft-qa-erosion.pipeline.yaml"


def _required_draft_fields() -> list[str]:
    contract = yaml.safe_load(_DRAFT_CONTRACT.read_text(encoding="utf-8"))
    return contract["schema"]["required_fields"]


def _max_iterations() -> int:
    """Read off the pipeline rather than restated: the table's K axis is that number,
    and a copy of it here would keep printing the old one after the group was retuned."""
    pipeline = yaml.safe_load(_PIPELINE.read_text(encoding="utf-8"))
    return pipeline["stages"][0]["max_iterations"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=_CORPUS)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("-o", "--out", type=Path, default=Path("logs/qa-erosion.json"))
    args = parser.parse_args(argv)

    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    cases = qa_gate.load_corpus(args.corpus, _required_draft_fields())
    if not cases:
        print(f"[qa-erosion] {args.corpus} holds no cases", file=sys.stderr)
        return 1

    max_iterations = _max_iterations()
    provider = qa_provider.resolve(REPO_ROOT, qa_provider.REVIEWER)
    studio_version = qa_studio_version.resolve(REPO_ROOT)
    run_id = str(uuid.uuid4())
    print(
        f"[qa-erosion] {len(cases)} case(s) x {args.repeats} repeat(s), "
        f"up to {max_iterations} passes each | provider {provider or 'unresolved'} "
        f"| studio {studio_version or 'unresolved'} | run id {run_id}",
        file=sys.stderr,
    )

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for case in cases:
            passes = []
            for repeat in range(1, args.repeats + 1):
                label = f"{case.id}#{repeat}"
                print(f"[qa-erosion] {label}", file=sys.stderr)
                judgement = qa_replay.judge_passes(
                    case.item, case.draft, workdir, run_id, REPO_ROOT, label=label
                )
                print(f"  {' -> '.join(judgement.verdicts) or '(no verdict)'}",
                      file=sys.stderr)
                passes.append(qa_erosion.Pass(
                    case.id, repeat, judgement.verdicts, judgement.unreachable))
            results.append(qa_erosion.CaseResult(case.id, case.gate, tuple(passes)))

    print(qa_erosion.render(results, max_iterations, provider, studio_version))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            qa_erosion.as_dict(results, max_iterations, args.repeats, provider,
                               studio_version)
            | {"run_id": run_id},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[qa-erosion] wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
