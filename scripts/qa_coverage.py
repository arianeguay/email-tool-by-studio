#!/usr/bin/env python3
"""Did the bodies a run shipped answer what their senders asked? (STU-1140)

    scripts/qa_coverage.py .studio/runs/<run>.jsonl [--repeats 2] [--limit N]

Reads a finished drafting journal as a sequence rather than as its last verdict, then
asks one cold reader, per conversation, whether the body that shipped addresses every
request in the message it answers. The reader has never seen the draft's history and is
not `draft-reviewer`: on the run that motivated this the reviewer approved its own prior
instruction coming back, so it is the party under suspicion.

The sequence read is printed beside each verdict, never as it. `converged by removal` —
the approved body shorter than the pass before it and the shortest of the run — says the
loop is what dropped the answer. A draft shrinking toward the feedback is what the loop
is for; it is only a defect when what went with it was the question.

A measurement, not a gate: this exits 0 whatever it finds. `--repeats` defaults to 2 for
the reason `qa_erosion.py`'s does — the reader is a model, and a single flag cannot be
told from a draw.

Live: one call per approved conversation per repeat. Nothing is drafted and no mailbox
is written, but the pipeline runs, so this needs a provider.

`--provider anthropic` by default, for the reasons `coverage_read.py` records.
"""

import argparse
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

from email_classifier import coverage_read, qa_coverage, qa_provider, qa_studio_version
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, help="a finished drafting run's Studio journal")
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--limit", type=int, help="read only the first N approved groups")
    parser.add_argument("--model",
                        help=f"override the reader's pin via {coverage_read.MODEL_ENV}")
    parser.add_argument("--provider", default=coverage_read.DEFAULT_PROVIDER,
                        help="Studio provider the reader runs under. Empty for the "
                             "config default (see `coverage_read.py`).")
    parser.add_argument("-o", "--out", type=Path, default=Path("logs/qa-coverage.json"))
    args = parser.parse_args(argv)

    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.model:
        os.environ[coverage_read.MODEL_ENV] = args.model

    with args.journal.open(encoding="utf-8") as handle:
        try:
            groups = qa_coverage.groups_from_journal(handle)
        except (qa_coverage.Unattributable, qa_coverage.MalformedJournal,
                qa_coverage.OrphanGroup, qa_coverage.EchoJournal) as exc:
            print(f"[qa-coverage] {args.journal}: {exc}", file=sys.stderr)
            return 2

    shipped = [g for g in groups if g.approved]
    skipped = len(groups) - len(shipped)
    if args.limit:
        shipped = shipped[: args.limit]
    if not shipped:
        print(
            f"[qa-coverage] {args.journal} holds no approved group "
            f"({len(groups)} group(s), {skipped} without a shipped body)",
            file=sys.stderr,
        )
        return 1

    # Resolved, not requested (STU-1175): `--provider ""` asks for the config default,
    # and a record holding the empty flag would say nothing about what answered.
    provider = qa_provider.resolve(REPO_ROOT, qa_provider.READER, args.provider)
    studio_version = qa_studio_version.resolve(REPO_ROOT)
    run_id = str(uuid.uuid4())
    print(
        f"[qa-coverage] {len(shipped)} shipped body(ies) x {args.repeats} read(s)"
        + (f", {skipped} group(s) skipped without one" if skipped else "")
        + f" | provider {provider or 'unresolved'} | studio "
        f"{studio_version or 'unresolved'} | run id {run_id}",
        file=sys.stderr,
    )

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for group in shipped:
            reads = []
            for repeat in range(1, args.repeats + 1):
                label = f"{group.thread_id[:12]}#{repeat}"
                print(f"[qa-coverage] {label}", file=sys.stderr)
                read = coverage_read.read(group, workdir, run_id, repeat, label,
                                          REPO_ROOT, args.provider)
                print(f"  {read.verdict}", file=sys.stderr)
                reads.append(read)
            results.append(qa_coverage.GroupResult(group, tuple(reads)))

    print(qa_coverage.render(results, provider, studio_version))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            qa_coverage.as_dict(results, args.repeats, provider, studio_version)
            | {"run_id": run_id, "journal": str(args.journal),
               "skipped_unapproved": skipped},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[qa-coverage] wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
