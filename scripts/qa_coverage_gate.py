#!/usr/bin/env python3
"""Replay the frozen coverage reads past the reader (STU-1151).

    scripts/qa_coverage_gate.py [--corpus eval/draft-coverage-cases.json] [--repeats 2]
    scripts/qa_coverage_gate.py --check

`--check` is the half that runs unattended (STU-1156). The gate itself is `claude:local`
— one live call per entry per repeat — so no push can run it. What a push can run is the
question of whether it still needs running: a pass writes `eval/draft-coverage-gate.json`
with a digest of every file the reader read, and `--check` compares those against the
tree it is standing in. Exit 3 when one moved, 2 when nothing is recorded, and the repair
for both is `make qa-coverage-gate`. Exit 5 when the digests and the record come from two
trees, which describes neither reader (STU-1109).

Exits non-zero when any entry comes back anything but the verdict it was frozen with —
that being the whole statement. `make qa-coverage` measures a run and exits 0 whatever it
finds; this asks whether the read that measurement rests on still separates the two runs
STU-1140 was argued from, and those two journals are on a 30-day shelf.

Exit 4 when every case's every read came back unreadable (STU-1254) — a dead provider is
the concrete cause this was measured against, but the check is on the read, not the
cause. A gate nobody could interrogate has not fired, so it still exits non-zero and
still leaves the record alone; what changes is that the summary stops calling an unread
case `READS NO LONGER`, prose reserved for a frozen read that came back the other way,
and stops printing `0/N still read the way they were frozen` when nothing asked the
reader anything. A single case going unreadable beside others that answered stays inside
the ordinary non-zero exit — that is a real per-case finding, not this whole-run one.

No `--model`: the reader runs on the model `coverage-reader.agent.yaml` pins, because the
question is whether *production's* reader still says this, not whether some model would.

`--repeats` defaults to 2 and every read has to agree, where `qa_coverage` takes 2 to tell
a flag from a draw. The reasons are not the same one: there a lone flag is a finding a
human might chase, here a lone disagreement *is* the finding — a read that moved has moved
whichever side the majority fell on.

Live: one call per entry per repeat. Nothing is drafted and no mailbox is written — the
reader has no tools — but the pipeline runs, so this needs a provider.

`--deployment-root` is the seam a second deployment plugs into (STU-1459), the same as
`qa_gate.py`'s: `eval/`, `.studio/agents/coverage-reader.agent.yaml` and its sources are
read relative to it, `email_classifier` never is.
"""

import argparse
import json
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from email_classifier import (coverage_read, qa_coverage_gate, qa_provider,
                              qa_studio_version, tree_guard)
from email_classifier import deployment

REPO_ROOT = deployment.ROOT

_CORPUS = REPO_ROOT / "eval" / "draft-coverage-cases.json"
# Committed, for the reason the corpus beside it is: digests name nobody, and the ids
# they hold are the gate names the corpus already publishes. A file of its own rather
# than a block inside the corpus, matching `qa_gate` and keeping the corpus a thing
# nothing rewrites.
_RECORD = REPO_ROOT / "eval" / "draft-coverage-gate.json"


def _paths_for(deployment_root: Path) -> tuple[Path, Path]:
    """corpus, record for a deployment root other than this repo's own.

    Kept separate from the `_CORPUS`/`_RECORD` module constants above rather than
    replacing them: tests monkeypatch `_RECORD` directly, and a constant read fresh off
    the module at call time is what makes that work (mirrors `qa_gate.py`).
    """
    return (
        deployment_root / "eval" / "draft-coverage-cases.json",
        deployment_root / "eval" / "draft-coverage-gate.json",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deployment-root", type=Path, default=None,
        help="Where eval/, .studio/ and the reader's sources live. Defaults to this "
             "repo's own root.",
    )
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--provider", default=coverage_read.DEFAULT_PROVIDER,
                        help="Studio provider the reader runs under. Empty for the "
                             "config default (see `coverage_read.py`).")
    parser.add_argument("-o", "--out", type=Path, default=Path("logs/qa-coverage-gate.json"))
    parser.add_argument(
        "--check", action="store_true",
        help="ask whether the recorded pass still describes this tree's reader, "
             "without calling one",
    )
    args = parser.parse_args(argv)

    if args.repeats < 1:
        parser.error("--repeats must be at least 1")

    if args.deployment_root:
        deployment_root = args.deployment_root.resolve()
        canonical_corpus, record = _paths_for(deployment_root)
    else:
        deployment_root = REPO_ROOT
        canonical_corpus, record = _CORPUS, _RECORD
    corpus = args.corpus or canonical_corpus

    # `qa_coverage_gate.sources()` is read off the package and digested against this
    # tree's files, so a mixed pair fingerprints neither of them (STU-1109). This asks
    # about the package's own checkout, never about `deployment_root` (see `qa_gate.py`).
    if args.check:
        refusal = tree_guard.mismatch(Path(__file__).resolve().parents[1])
        if refusal:
            print(refusal, file=sys.stderr)
            return tree_guard.MIXED_TREES

    today = qa_coverage_gate.fingerprint(deployment_root, corpus)
    if args.check:
        return qa_coverage_gate.check(today, record)

    # The record names one corpus and `--corpus` names any, so a pass on another one
    # would replace a verified record with a measurement of something else.
    if corpus.resolve() != canonical_corpus:
        print(f"[qa-coverage-gate] {corpus} is not {canonical_corpus.name}; this run "
              f"will not write {record.name}", file=sys.stderr)

    cases = qa_coverage_gate.load_corpus(corpus)
    if not cases:
        print(f"[qa-coverage-gate] {corpus} holds no cases", file=sys.stderr)
        return 1

    # Resolved, not requested (STU-1175): `--provider ""` asks for the config default,
    # and a record holding the empty flag would say nothing about what answered.
    provider = qa_provider.resolve(deployment_root, qa_provider.READER, args.provider)
    studio_version = qa_studio_version.resolve(deployment_root)
    run_id = str(uuid.uuid4())
    print(f"[qa-coverage-gate] {len(cases)} case(s) x {args.repeats} read(s) "
          f"| provider {provider or 'unresolved'} | studio "
          f"{studio_version or 'unresolved'} | run id {run_id}", file=sys.stderr)

    outcomes = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for n, case in enumerate(cases, 1):
            print(f"[qa-coverage-gate] {n}/{len(cases)} {case.id}", file=sys.stderr)
            reads = []
            for repeat in range(1, args.repeats + 1):
                label = f"{case.id}#{repeat}"
                read = coverage_read.read(case.group, workdir, run_id, repeat, label,
                                          deployment_root, args.provider)
                print(f"  {read.verdict}", file=sys.stderr)
                reads.append(read)
            outcomes.append(qa_coverage_gate.Outcome(case, tuple(reads)))

    print(qa_coverage_gate.render(outcomes, provider, studio_version))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(qa_coverage_gate.as_dict(outcomes, args.repeats, provider,
                                            studio_version)
                   | {"run_id": run_id}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"[qa-coverage-gate] wrote {args.out}", file=sys.stderr)

    dead = qa_coverage_gate.unreadable(outcomes)
    if dead and len(dead) == len(outcomes):
        # Nobody could interrogate this gate, so it has not fired (STU-1254) — checked
        # before the generic failure count, the position `qa_gate.py` checks
        # `TOOL_UNREACHABLE` in, and for the same reason: the condition disqualifies
        # every verdict below it rather than being one more of them.
        return qa_coverage_gate.NOTHING_READ
    if qa_coverage_gate.failures(outcomes):
        return 1
    if corpus.resolve() != canonical_corpus:
        return 0

    record.write_text(
        json.dumps(
            qa_coverage_gate.as_record(
                outcomes, today, run_id,
                datetime.now(timezone.utc).strftime("%Y-%m-%d"), provider,
                studio_version,
            ),
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[qa-coverage-gate] wrote {record}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
