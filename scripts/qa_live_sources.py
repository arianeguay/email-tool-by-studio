#!/usr/bin/env python3
"""Measure whether a live source can flip a qa-gate verdict (STU-1114).

    scripts/qa_live_sources.py [--corpus eval/draft-qa-rejections.json] [--repeats 2]

Replays each frozen known-bad draft twice over: once with the reviewer's tools live, and
once with `org_web-search_site` and `mail_precedents-search_mail` answering nothing. A
case that rejects both ways is judged on its own prose, so `qa_gate.SOURCES` covers what
decides it; a case whose settled verdict moves rests on a source no fingerprint watches,
and the table names it and the tool the live arm was answered by.

`--repeats` defaults to 2 for the reason `qa_erosion.py` has the same default: STU-1014
put the reviewer's floor at 21 of 22 first attempts judging the same drafts twice, so an
arm that has not agreed with itself cannot be one side of a comparison.

A measurement, not a gate: exits 0 whatever it finds, like `make qa-erosion` and unlike
`make qa-gate`. What makes the gate exit non-zero is the record it would otherwise stamp
for other trees to read; nothing reads `logs/qa-live-sources.json` back as an assertion,
and a human reads the table the day it is printed. A tool that *raised* is the other
question (STU-1139) and disqualifies the case's reading in the finding rather than in an
exit code.

No `--model`: the question is whether *production's* reviewer turns on a live source.
"""

import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

from email_classifier import (qa_gate, qa_live_sources, qa_provider, qa_replay,
                              qa_studio_version)
from email_classifier import deployment

REPO_ROOT = deployment.ROOT

_CORPUS = REPO_ROOT / "eval" / "draft-qa-rejections.json"
_DRAFT_CONTRACT = REPO_ROOT / ".studio" / "contracts" / "qa-replay-draft.contract.yaml"
_SHIM = Path(__file__).resolve().parent / "qa_silent_tool.py"


def _required_draft_fields() -> list[str]:
    contract = yaml.safe_load(_DRAFT_CONTRACT.read_text(encoding="utf-8"))
    return contract["schema"]["required_fields"]


def _silent_env() -> dict[str, str]:
    """What turns the two silenceable tools into empty answers for one replay.

    `PYTHON` is what every tool in `.studio/tools/` runs, so the shim reaches all of
    them without a second copy of the agent, the pipeline or the tool YAMLs — the arm
    differs from the live one in what came back and in nothing else.
    """
    return {
        "PYTHON": str(_SHIM),
        qa_live_sources.REAL_PYTHON_ENV: sys.executable,
        qa_live_sources.SILENCED_ENV: ",".join(qa_live_sources.SILENCEABLE),
    }


def _arm(name, env, case, workdir, run_id, repeats):
    """One case judged `repeats` times under one source condition."""
    verdicts, called, unreachable = [], [], []
    for repeat in range(1, repeats + 1):
        label = f"{case.id}#{repeat} {name}"
        print(f"[qa-live-sources] {label}", file=sys.stderr)
        judgement = qa_replay.judge(
            case.item, case.draft, workdir, run_id, REPO_ROOT, label=label, extra_env=env
        )
        print(f"  {judgement.verdict}", file=sys.stderr)
        verdicts.append(judgement.verdict)
        called.extend(t for t in judgement.called if t not in called)
        unreachable.extend(t for t in judgement.unreachable if t not in unreachable)
    return qa_live_sources.Arm(name, tuple(verdicts), tuple(called), tuple(unreachable))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=_CORPUS)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("-o", "--out", type=Path, default=Path("logs/qa-live-sources.json"))
    args = parser.parse_args(argv)

    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    cases = qa_gate.load_corpus(args.corpus, _required_draft_fields())
    if not cases:
        print(f"[qa-live-sources] {args.corpus} holds no cases", file=sys.stderr)
        return 1

    provider = qa_provider.resolve(REPO_ROOT, qa_provider.REVIEWER)
    studio_version = qa_studio_version.resolve(REPO_ROOT)
    run_id = str(uuid.uuid4())
    print(
        f"[qa-live-sources] {len(cases)} case(s) x 2 arms x {args.repeats} repeat(s) | "
        f"silencing {', '.join(qa_live_sources.SILENCEABLE)} | "
        f"provider {provider or 'unresolved'} | studio {studio_version or 'unresolved'} "
        f"| run id {run_id}",
        file=sys.stderr,
    )

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for case in cases:
            arms = {
                name: _arm(name, env, case, workdir, run_id, args.repeats)
                for name, env in (
                    (qa_live_sources.LIVE, {}),
                    (qa_live_sources.SILENT, _silent_env()),
                )
            }
            results.append(qa_live_sources.CaseResult(
                case.id, case.gate,
                arms[qa_live_sources.LIVE], arms[qa_live_sources.SILENT],
            ))

    print(qa_live_sources.render(results, args.repeats, provider, studio_version))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            qa_live_sources.as_dict(results, args.repeats, provider, studio_version)
            | {"run_id": run_id},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[qa-live-sources] wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
