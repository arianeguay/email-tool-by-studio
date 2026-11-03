#!/usr/bin/env python3
"""Score two `draft-qa` models on the drafts a finished run recorded (STU-1014).

Replays every `write-draft` output in a Studio run journal past the reviewer twice —
once per model — through `draft-qa-replay`, and diffs the two arms' verdicts. Nothing
is drafted and no mailbox is written; the reviewer's own tools still run, so this needs
the same credentials a drafting run does.

    scripts/qa_bakeoff.py .studio/runs/<run>.jsonl \
        --baseline claude-opus-4-8 --candidate claude-sonnet-5 -o logs/qa-bakeoff.json

Exits non-zero when the arms disagree on any draft — the whole point being the drafts
they judge the same way. A reviewer tool that raised on every call it made is named in
the summary and in the JSON, and does not change the exit code (STU-1147): the number
here is read by a human the day it prints, where `make qa-gate` exits 4 on the same
finding because it would otherwise stamp a record other trees read back as an assertion.
"""

import argparse
import json
import sys
import tempfile
import uuid
from pathlib import Path

from email_classifier import qa_replay
from email_classifier.qa_bakeoff import compare, drafts_from_journal, unreachable_note
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("journal", type=Path, help="Studio run journal of a finished run")
    parser.add_argument("--baseline", default="claude-opus-4-8")
    parser.add_argument("--candidate", default="claude-sonnet-5")
    parser.add_argument("--limit", type=int, help="judge only the first N drafts")
    parser.add_argument("-o", "--out", type=Path, help="write the full result as JSON")
    args = parser.parse_args()

    drafts = drafts_from_journal(args.journal.read_text(encoding="utf-8").splitlines())
    if args.limit:
        drafts = drafts[: args.limit]
    if not drafts:
        sys.exit(f"[qa-bakeoff] no drafts in {args.journal}")

    print(
        f"[qa-bakeoff] {len(drafts)} draft(s) over "
        f"{len({d.thread_id for d in drafts})} conversation(s)",
        file=sys.stderr,
    )

    bakeoff_id = str(uuid.uuid4())
    print(f"[qa-bakeoff] run id {bakeoff_id}", file=sys.stderr)

    arms: dict[str, dict[str, str]] = {args.baseline: {}, args.candidate: {}}
    # Pooled per arm, not per draft: a tool that raised is down for the machine and the
    # grant, not for one conversation, so naming the drafts it was missing on would be
    # a longer way of naming the arm.
    unreachable: dict[str, set[str]] = {args.baseline: set(), args.candidate: set()}
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        for n, draft in enumerate(drafts, 1):
            print(f"[qa-bakeoff] {n}/{len(drafts)} {draft.key}", file=sys.stderr)
            for model, verdicts in arms.items():
                judgement = qa_replay.judge(
                    draft.item, draft.draft, workdir, bakeoff_id, REPO_ROOT,
                    model=model, label=model,
                )
                verdicts[draft.key] = judgement.verdict
                unreachable[model].update(judgement.unreachable)
                print(f"  {model}: {verdicts[draft.key]}", file=sys.stderr)

    by_arm = {model: tuple(sorted(tools)) for model, tools in unreachable.items()}

    comparison = compare(arms[args.baseline], arms[args.candidate])
    print(
        f"\n[qa-bakeoff] {comparison.agreed}/{comparison.judged} agree "
        f"({comparison.agreement:.0%}) | {len(comparison.missed_rejections)} missed rejection(s)"
    )
    for d in comparison.disagreements:
        flag = "MISSED REJECTION" if d.missed_rejection else "extra rejection"
        print(f"  {d.key}: {args.baseline}={d.baseline} {args.candidate}={d.candidate}  [{flag}]")
    for line in unreachable_note(by_arm):
        print(line)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({
            "journal": str(args.journal),
            "run_id": bakeoff_id,
            "baseline": {"model": args.baseline, "verdicts": arms[args.baseline],
                         "unreachable": list(by_arm[args.baseline])},
            "candidate": {"model": args.candidate, "verdicts": arms[args.candidate],
                          "unreachable": list(by_arm[args.candidate])},
            "judged": comparison.judged,
            "agreed": comparison.agreed,
            "disagreements": [
                {"key": d.key, "baseline": d.baseline, "candidate": d.candidate,
                 "missed_rejection": d.missed_rejection}
                for d in comparison.disagreements
            ],
        }, indent=2), encoding="utf-8")
        print(f"[qa-bakeoff] wrote {args.out}", file=sys.stderr)

    sys.exit(1 if comparison.disagreements else 0)


if __name__ == "__main__":
    main()
