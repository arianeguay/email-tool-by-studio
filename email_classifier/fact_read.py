"""Did the reply sent in a draft's place keep, extend or contradict its facts (STU-1998).

Lexical similarity cannot tell a reviewer who sent the draft's substance in their own
words from one who corrected it: both score `replaced`. The second is the case a pilot
needs counted, so each joined pair gets a reading from `draft-facts`, a reader shaped
like `coverage-reader`: two texts, no history, no tools.

Live, driven through `coverage_read.run_stage`, and opt-in: `reviewed` stays offline
unless asked for it.
"""

import json
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

from email_classifier import coverage_read
from email_classifier.qa_coverage import ERROR, INCOHERENT, list_field

PIPELINE = "draft-facts"
STAGE = "draft-facts"
MODEL_ENV = "FACTS_MODEL"
DEFAULT_PROVIDER = coverage_read.DEFAULT_PROVIDER

KEPT = "kept"
EXTENDED = "extended"
CONTRADICTED = "contradicted"
VERDICTS = (KEPT, EXTENDED, CONTRADICTED)
READINGS = (*VERDICTS, ERROR, INCOHERENT)


def read_of(output: dict) -> dict:
    """The reader's output as `{verdict, contradicted}`, cross-checked.

    `verdict` restates what `facts` and `added` already say, on purpose, as in
    `qa_coverage.read_of`: a contradicted fact must make the pair `contradicted`, and
    otherwise an added fact makes it `extended`. A reader whose verdict disagrees with
    its own lists is `incoherent`, not a reading, and an output of the wrong shape is
    `error`; the two stay apart because their repairs differ.
    """
    facts, added = list_field(output, "facts"), list_field(output, "added")
    verdict = output.get("verdict")
    if facts is None or added is None or verdict not in VERDICTS:
        return {"verdict": ERROR, "contradicted": []}
    contradicted = [str(f.get("fact", "")) for f in facts
                    if isinstance(f, dict) and f.get("reading") == CONTRADICTED]
    expected = CONTRADICTED if contradicted else EXTENDED if added else KEPT
    if verdict != expected:
        return {"verdict": INCOHERENT, "contradicted": contradicted}
    return {"verdict": verdict, "contradicted": contradicted}


def read(thread_id: str, draft_body: str, sent_body: str, cwd: Path,
         provider: str | None = DEFAULT_PROVIDER) -> dict:
    """One reading of one (draft, sent) pair: the two bodies and nothing else.

    The item carries a run id of its own, never the audited run's: Studio journals the
    input, and a journal naming the audited run would make `cost` refuse it as ambiguous.
    """
    with tempfile.TemporaryDirectory() as workdir:
        item_path = Path(workdir) / "facts.yaml"
        item_path.write_text(
            yaml.safe_dump({"thread_id": thread_id, "draft_body": draft_body,
                            "sent_body": sent_body,
                            "run_id": f"draft-facts-{uuid.uuid4().hex[:8]}"},
                           sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        output = coverage_read.run_stage(PIPELINE, STAGE, item_path, cwd, provider,
                                         thread_id)
    parsed = read_of(output)
    if parsed["verdict"] == INCOHERENT:
        print(f"  {thread_id}: incoherent | {json.dumps(output, ensure_ascii=False)}",
              file=sys.stderr)
    return parsed


def annotate(rows: list[dict], drafted: dict[str, str], sent: dict[str, str],
             cwd: Path, provider: str | None = DEFAULT_PROVIDER) -> dict:
    """Add a `facts` reading to every joined row, and return the count per reading.

    An unjoined row has nothing to compare and gets none: a missing reading is not a
    `kept`.
    """
    tally = dict.fromkeys(READINGS, 0)
    for row in rows:
        thread_id = row["thread_id"]
        if thread_id not in sent or thread_id not in drafted:
            continue
        row["facts"] = read(thread_id, drafted[thread_id], sent[thread_id], cwd, provider)
        tally[row["facts"]["verdict"]] += 1
    return tally
