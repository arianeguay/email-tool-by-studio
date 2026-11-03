"""Candidate drafting rules from the reviewer's corrections (STU-1108).

A precedent search already carries the sent reply forward as phrasing. What it never
shows is what was wrong with ours, because the send overwrites the draft. The pair is
that signal, and this turns a window of pairs into a short list of candidate rules,
each with the pairs that support it, for a human to accept into a skill or reject.
Nothing here edits a prompt, and no pair is replayed as a few-shot example.

Live, driven through `coverage_read.run_stage` like the other one-stage readers.
"""

import json
import sys
import tempfile
import uuid
from pathlib import Path

import yaml

from email_classifier import coverage_read, draft_delta
from email_classifier.anonymize import Tokenizer, vouched
from email_classifier.precedents import redact
from email_classifier.qa_coverage import ERROR, INCOHERENT, list_field

PIPELINE = "draft-corrections"
STAGE = "draft-corrections"
MODEL_ENV = "CORRECTIONS_MODEL"
DEFAULT_PROVIDER = coverage_read.DEFAULT_PROVIDER
OK = "ok"

# Two distinct conversations. One pair cannot tell a drafter defect from reviewer noise
# (a fact only the reviewer had, a reply moved to another channel, a phone send); the
# same correction on two unrelated conversations is the least that can. Not higher: a
# pilot round corrects about a dozen replies, so a bar of three would keep the list
# empty for months, and the human accepting a candidate is the second filter.
MIN_SUPPORT = 2


def corrected(drafted: dict[str, str], sent: dict[str, str]) -> dict[str, tuple[str, str]]:
    """The joined pairs the reviewer changed: a reply sent unchanged corrects nothing."""
    return {
        t: (drafted[t], sent[t]) for t in sorted(drafted)
        if t in sent and draft_delta.outcome(
            draft_delta.similarity(drafted[t], sent[t])) != draft_delta.UNCHANGED_LABEL
    }


def redacted(pairs: dict[str, tuple[str, str]],
             tokenizer: Tokenizer | None = None) -> dict[str, dict]:
    """Each pair under a short id, both bodies redacted the way a precedent is.

    Index-free placeholders and the unvouched-proper-noun pass, because the result is
    read and never restored, and it is both what the reader sees and what the
    candidates file keeps on disk.
    """
    tokenizer = tokenizer or Tokenizer()
    words = vouched()
    return {
        f"P{n}": {"thread_id": t, "draft": redact(ours, tokenizer, words),
                  "sent": redact(theirs, tokenizer, words)}
        for n, (t, (ours, theirs)) in enumerate(pairs.items(), start=1)
    }


def candidates_of(output: dict, pairs: dict[str, dict]) -> dict:
    """The reader's output, held to the pairs it was given and to `MIN_SUPPORT`.

    A cited id the input never carried is dropped rather than trusted: support is
    counted on pairs that exist. A candidate left under the floor is kept, under
    `single_pair`, so a human sees what was proposed and why it is not a candidate.
    A pair cited and also called unexplained, or neither, makes the whole reading
    `incoherent`: the reader contradicted itself about what the pair shows.
    """
    raw, unexplained = list_field(output, "candidates"), list_field(output, "unexplained")
    if raw is None or unexplained is None or not all(isinstance(c, dict) for c in raw):
        return {"reading": ERROR}
    cited: set[str] = set()
    candidates, single = [], []
    for item in raw:
        ids = [i for i in pairs if i in {str(c) for c in list_field(item, "pairs") or []}]
        cited.update(ids)
        entry = {"rule": str(item.get("rule", "")), "why": str(item.get("why", "")),
                 "support": len(ids), "pairs": [pairs[i] for i in ids]}
        (candidates if len(ids) >= MIN_SUPPORT else single).append(entry)
    unexplained_ids = {str(i) for i in unexplained if str(i) in pairs}
    if cited & unexplained_ids or cited | unexplained_ids != set(pairs):
        return {"reading": INCOHERENT}
    candidates.sort(key=lambda c: -c["support"])
    return {"reading": OK, "candidates": candidates, "single_pair": single,
            "unexplained": [p["thread_id"] for i, p in pairs.items() if i in unexplained_ids]}


def propose(pairs: dict[str, dict], cwd: Path,
            provider: str | None = DEFAULT_PROVIDER) -> dict:
    """One reading over every redacted pair of the window, in a single item.

    The item carries a run id of its own, for `fact_read.read`'s reason: Studio
    journals the input, and naming an audited run would make `cost` ambiguous.
    """
    if len(pairs) < MIN_SUPPORT:
        return {"reading": OK, "candidates": [], "single_pair": [], "unexplained": [],
                "note": f"{len(pairs)} corrected pair(s), under the {MIN_SUPPORT} a "
                        "candidate needs: no reader was called."}
    with tempfile.TemporaryDirectory() as workdir:
        item_path = Path(workdir) / "corrections.yaml"
        item_path.write_text(
            yaml.safe_dump({"pairs": [{"id": i, "draft": p["draft"], "sent": p["sent"]}
                                      for i, p in pairs.items()],
                            "run_id": f"draft-corrections-{uuid.uuid4().hex[:8]}"},
                           sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        output = coverage_read.run_stage(PIPELINE, STAGE, item_path, cwd, provider,
                                         "corrections")
    result = candidates_of(output, pairs)
    if result["reading"] == INCOHERENT:
        # Redacted text only, so printing it leaks nothing the candidates file would not.
        print(f"  corrections: incoherent | {json.dumps(output, ensure_ascii=False)}",
              file=sys.stderr)
    return result
