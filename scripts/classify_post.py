#!/usr/bin/env python3
"""Post-stage of `classify-batch` (STU-843): collect the verdicts, write the report.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

Reads the Stage-1 split from `classify-pre` and the fan-out results from the
`classify-verdicts` call. A missing call output — condition-skipped because nothing
escalated, or a tolerated failure — is not an error here: the deterministic verdicts
stand and each escalated email is recorded as a Stage-2 error, which is the STU-392
bias the per-email orchestrator already had. Exits 0 either way, so one dead email
never fails the batch.

Output: { "run_id", "report_path", "totals", "ratio_deterministic" }
"""

import json
import sys
from pathlib import Path

from email_classifier import cost
from email_classifier.anonymize import load_keymap
from email_classifier.core.contract import load_taxonomy
from email_classifier.learn.store import VerdictStore
from email_classifier.orchestrate import batch
from email_classifier.orchestrate.report import build_report, render_human, report_to_dict

_CALL_STAGE = "classify-verdicts"
_PRE_STAGE = "classify-pre"


def main() -> None:
    payload = json.load(sys.stdin)
    outputs = payload.get("previous_outputs") or {}
    pre = outputs[_PRE_STAGE]
    paths = pre["paths"]

    keymap = load_keymap(Path(paths["keymap"]))
    taxonomy = load_taxonomy(Path(paths["taxonomy"]))
    results = batch.results_by_index(outputs.get(_CALL_STAGE))

    store = VerdictStore(Path(paths["verdict_store"]))
    verdicts = []
    for record in pre["stage1"]:
        if not record["escalated"]:
            verdicts.append(batch.deterministic_verdict(record))
            continue
        verdict = batch.llm_verdict(
            record, results.get(record["escalation_index"]), keymap, taxonomy
        )
        verdicts.append(verdict)
        if verdict.status == "ok":
            store.record(
                run_id=pre["run_id"],
                message_id=record["message_id"],
                thread_id=record["thread_id"],
                from_address=record["from_address"],
                bucket=verdict.bucket,
                suggested_action=verdict.suggested_action,
                confidence=verdict.confidence,
            )

    # What the fan-out spent, off the per-item results it already reported (STU-1012),
    # priced from the per-stage record each item carries — so an item whose child run
    # died contributes the attempts it really made (STU-1210).
    # A resumed item is dropped rather than counted unpriced: its verdict was bought by
    # an earlier run, so it is not a call this one made.
    # The provider the run resolved, carried from `classify-pre` rather than read
    # off the tree at audit time (STU-1145).
    spend = cost.leg((result for result in results.values() if not result.get("cached")),
                     pre["provenance"].get("provider"))
    report = build_report(pre["run_id"], verdicts, pre["provenance"], pre["composition"],
                          {cost.CLASSIFY_LEG: spend})
    report_dir = Path(paths["report_dir"])
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{report.run_id}.json"
    report_path.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(render_human(report), file=sys.stderr)
    print(f"\nReport written to {report_path}", file=sys.stderr)
    json.dump({
        "run_id": report.run_id,
        "report_path": str(report_path),
        "totals": report.totals,
        "ratio_deterministic": report.ratio_deterministic,
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
