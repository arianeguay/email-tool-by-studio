#!/usr/bin/env python3
"""Replay the frozen known-bad drafts past the reviewer (STU-1078).

    scripts/qa_gate.py [--corpus eval/draft-qa-rejections.json] [-o logs/qa-gate.json]
    scripts/qa_gate.py --check

`--check` is the half that runs unattended (STU-1086). The gate itself is `claude:local`
— a live Opus call, and the reviewer's own tools read the mailbox — so no push can run
it. What a push can run is the question of whether it still needs running: a pass writes
`eval/draft-qa-gate.json` with a digest of every file the reviewer read, and `--check`
compares those against the tree it is standing in. Exit 3 when one moved, 2 when nothing
is recorded, and the repair for both is `make qa-gate`. Exit 5 when the digests and the
record come from two trees, which describes neither reviewer (STU-1109). Exit 6 when
the record does not match its own digest: a `git merge` assembles one out of two runs
without either agreeing to it, and the result describes a reviewer neither replayed
(STU-1153).

Exits non-zero when any entry comes back anything but `rejected` — that being the whole
statement. No `--model`: the reviewer runs on the model `draft-reviewer.agent.yaml` pins,
because the question is whether production still rejects these, not whether some model
would. `make qa-bakeoff` is where a model is flipped.

Exit 4 when one of the reviewer's tools raised on every call it made (STU-1139), and the
record is not stamped. That run judged with a reviewer production does not have, so its
`rejected` is not the statement the record would claim — it comes before the verdicts
because it disqualifies reading them at all, whichever way they went.

A raise that a tool's own store explains does not count (STU-1235): `org_memory-read_memory`
raises for any name when its index is verifiably empty in this tree, and that is the store
saying so, not a lost source. Named beside the verdicts (`empty_store`) instead, and never
disqualifies — a gate that refused to run on an empty store could never run on this laptop.

One pass per entry, no repeats. The LLM is not deterministic, so a lone `approved` could
be a flake — but a gate that re-asks until it likes the answer is not a gate, and the
finding is worth a human reading either way.

`--deployment-root` is the seam a second deployment plugs into (STU-1459): everything
above reads `eval/`, `.studio/agents/draft-reviewer.agent.yaml` and its skills/tools/
contracts relative to it. Left unset it is this repo's own root, unchanged — the reviewer
`email_classifier`'s tests fingerprint is still `email_classifier`'s. `email_classifier`
itself is never relative to it: `tree_guard` asks a different question (does the script
and the installed package agree on which checkout they are in), and a deployment living
in a subdirectory of this same checkout answers that question the same way regardless of
which one's `eval/` is being replayed.
"""

import argparse
import json
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from email_classifier import qa_gate, qa_provider, qa_replay, qa_studio_version, tree_guard
from email_classifier import deployment

REPO_ROOT = deployment.ROOT

_CORPUS = REPO_ROOT / "eval" / "draft-qa-rejections.json"
# The reviewer's `previous_stage_output` contract: what a replayed draft has to carry for
# the pipeline to hand it over at all. Read rather than restated, so a contract that gains
# a field fails the corpus here instead of one live call later.
_DRAFT_CONTRACT = REPO_ROOT / ".studio" / "contracts" / "qa-replay-draft.contract.yaml"
# Committed, for the reason the corpus beside it is: digests name nobody, and the ids
# they hold are the gate names the corpus already publishes.
_RECORD = REPO_ROOT / "eval" / "draft-qa-gate.json"


def _paths_for(deployment_root: Path) -> tuple[Path, Path, Path]:
    """corpus, record, draft-contract for a deployment root other than this repo's own.

    Kept separate from the `_CORPUS`/`_RECORD`/`_DRAFT_CONTRACT` module constants above
    rather than replacing them: several tests monkeypatch `_RECORD` directly, and a
    constant read fresh off the module at call time is what makes that work.
    """
    return (
        deployment_root / "eval" / "draft-qa-rejections.json",
        deployment_root / "eval" / "draft-qa-gate.json",
        deployment_root / ".studio" / "contracts" / "qa-replay-draft.contract.yaml",
    )


def _required_draft_fields(draft_contract: Path) -> list[str]:
    contract = yaml.safe_load(draft_contract.read_text(encoding="utf-8"))
    return contract["schema"]["required_fields"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-root", type=Path, default=None,
        help="Where eval/, .studio/ and the reviewer's sources live. Defaults to this "
             "repo's own root.",
    )
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("-o", "--out", type=Path, default=Path("logs/qa-gate.json"))
    parser.add_argument(
        "--check", action="store_true",
        help="ask whether the recorded pass still describes this tree's reviewer, "
             "without calling one",
    )
    args = parser.parse_args(argv)

    if args.deployment_root:
        deployment_root = args.deployment_root.resolve()
        canonical_corpus, record, draft_contract = _paths_for(deployment_root)
    else:
        deployment_root = REPO_ROOT
        canonical_corpus, record, draft_contract = _CORPUS, _RECORD, _DRAFT_CONTRACT
    corpus = args.corpus or canonical_corpus

    # `qa_gate.sources()` is read off the package and digested against this tree's files,
    # so a mixed pair fingerprints neither of them (STU-1109). This asks about the
    # package's own checkout, never about `deployment_root` — a second deployment living
    # in a subdirectory of this same checkout is still one tree as far as tree_guard is
    # concerned; it is only ever "two trees" when the editable install resolves somewhere
    # else entirely (a stale worktree, STU-1109's original case).
    if args.check:
        refusal = tree_guard.mismatch(Path(__file__).resolve().parents[1])
        if refusal:
            print(refusal, file=sys.stderr)
            return tree_guard.MIXED_TREES

    today = qa_gate.fingerprint(deployment_root, corpus)
    if args.check:
        return qa_gate.check(today, record, deployment_root / "rules" / "policy.yaml")

    # The record names one corpus and `--corpus` names any, so a pass on another one
    # would replace a verified record with a measurement of something else. Refused the
    # way `observe_spans.py` refuses `--publish --limit`: a run measuring part of the
    # question does not get to answer it.
    if corpus.resolve() != canonical_corpus:
        print(f"[qa-gate] {corpus} is not {canonical_corpus.name}; this run will not "
              f"write {record.name}", file=sys.stderr)

    cases = qa_gate.load_corpus(corpus, _required_draft_fields(draft_contract))
    if not cases:
        print(f"[qa-gate] {corpus} holds no cases", file=sys.stderr)
        return 1

    # Resolved before the first call, not read back off the tree afterwards: what these
    # verdicts ran on is a property of this run (STU-1175).
    provider = qa_provider.resolve(deployment_root, qa_provider.REVIEWER)
    studio_version = qa_studio_version.resolve(deployment_root)
    run_id = str(uuid.uuid4())
    print(f"[qa-gate] {len(cases)} case(s) | provider {provider or 'unresolved'} "
          f"| studio {studio_version or 'unresolved'} | run id {run_id}", file=sys.stderr)

    outcomes = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for n, case in enumerate(cases, 1):
            print(f"[qa-gate] {n}/{len(cases)} {case.id}", file=sys.stderr)
            judgement = qa_replay.judge(
                case.item, case.draft, workdir, run_id, deployment_root, label=case.id
            )
            print(f"  {judgement.verdict}", file=sys.stderr)
            outcomes.append(
                qa_gate.Outcome(
                    case, judgement.verdict, judgement.unreachable, judgement.empty_store
                )
            )

    print(qa_gate.render(outcomes, provider, studio_version))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(qa_gate.as_dict(outcomes, provider, studio_version)
                   | {"run_id": run_id}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[qa-gate] wrote {args.out}", file=sys.stderr)

    if qa_gate.unverified(outcomes):
        return qa_gate.TOOL_UNREACHABLE
    if qa_gate.failures(outcomes):
        return 1
    if corpus.resolve() != canonical_corpus:
        return 0

    record.write_text(
        json.dumps(
            qa_gate.as_record(
                outcomes, today, run_id,
                datetime.now(timezone.utc).strftime("%Y-%m-%d"), provider,
                studio_version,
            ),
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[qa-gate] wrote {record}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
