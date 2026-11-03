"""The commitments a drafted reply makes, kept where a human works them off (STU-1065).

A draft can tell the sender something happened, or will happen, that nothing in this
system performs: a message forwarded, an answer to come back. The sentence is true only
because a human reads the draft and does it before sending, so the obligation rides on
the draft staying a draft. `logs/drafts/commitments.jsonl` beside the drafts journal is
the list of them, one entry per (conversation, draft, sentence).

Detection is a pattern list, not a field the drafter fills. The drafter is the party told
not to promise, so asking it to report its promises asks the suspect; a field would also
make every deployment's drafter prose and contract a fingerprinted edit. Patterns are a
floor: a promise phrased outside them is not listed, and an empty list says no matched
sentence, never no commitment.

The file is append-only and the last row of an id is its state. `write-draft` appends
`open` after the mailbox accepted the body, so a run that dies before `drafts-post` has
already recorded it (STU-917). When that stage rewrites the same draft, an open sentence
the new body no longer carries is `withdrawn`: the draft that made the promise no longer
exists, and a rejected attempt the group then fixed is the usual case. Only a human
`settled` row closes an entry otherwise; a later run's silence closes nothing.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

OPEN, WITHDRAWN, SETTLED = "open", "withdrawn", "settled"

_SUBJECT_FR = r"(?:j'ai|j’ai|nous avons|on a|l'équipe a|l’équipe a)"
# "à nous écrire", "de nous confirmer": the sender is asked, nobody promises.
_SUBJECT_FR_FUTURE = r"(?<!à )(?<!de )(?:je|nous|on|l'équipe|l’équipe)"
_SUBJECT_EN = r"(?:i|we)(?:\s+have|'ve|’ve)"
_SUBJECT_EN_FUTURE = r"(?:i|we)(?:\s+will|'ll|’ll|\s+are going to|'re going to|’re going to|\s+am going to|'m going to|’m going to)"

# (what the sentence obliges someone to do, pattern)
_PATTERNS = [
    ("forward the message, as the reply says was done",
     rf"\b{_SUBJECT_FR}\s+(?:transféré|transmis|fait suivre|acheminé|relayé)"
     rf"|\ba été (?:transféré|transmis|acheminé|relayé)"
     rf"|\b{_SUBJECT_EN}\s+(?:forwarded|passed (?:on|along)|sent (?:it|your message) (?:on|to))"),
    ("forward the message, as the reply promises",
     rf"\b{_SUBJECT_FR_FUTURE}\s+(?:allons |vais |va )?(?:vous\s+)?"
     rf"(?:transmettr\w*|transférer\w*|ferons suivre|ferai suivre|fera suivre|faire suivre)"
     rf"|\bsera (?:transféré|transmis|acheminé)"
     rf"|\b{_SUBJECT_EN_FUTURE}\s+(?:forward|pass (?:it|this|your message) (?:on|along))"),
    ("do what the reply says was done",
     rf"\b{_SUBJECT_FR}\s+(?:vérifié|contacté|communiqué avec|informé|avisé|parlé)"
     rf"|\b{_SUBJECT_EN}\s+(?:checked|contacted|informed|notified|spoken|talked)"),
    ("send the follow-up the reply promises",
     rf"\b{_SUBJECT_FR_FUTURE}\s+(?:allons |vais |va )?(?:vous\s+)?"
     rf"(?:reviendr\w*|revenir|recontacter\w*|rappeler\w*|rappeller\w*|confirmer\w*|"
     rf"répondr\w*|écrir\w*|informer\w*|tiendr\w* au courant|tenir au courant|vérifier\w*)"
     rf"|\bvous sera (?:confirmé|communiqué)e?s?"
     rf"|\b{_SUBJECT_EN_FUTURE}\s+(?:get back|follow up|confirm|let you know|reach out|"
     rf"contact|check|write|keep you posted)"),
]
_COMPILED = [(obliges, re.compile(pattern, re.IGNORECASE)) for obliges, pattern in _PATTERNS]

_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+|\n+")


def path_beside(journal: Path) -> Path:
    return journal.with_name("commitments.jsonl")


def detect(body: str) -> list[dict]:
    """One `{sentence, obliges}` per sentence that matches, first pattern wins."""
    found = []
    for sentence in (s.strip() for s in _SENTENCE_END.split(body)):
        for obliges, pattern in _COMPILED:
            if sentence and pattern.search(sentence):
                found.append({"sentence": sentence, "obliges": obliges})
                break
    return found


def entry_id(thread_id: str, draft_id: str, sentence: str) -> str:
    return hashlib.sha256(f"{thread_id}\0{draft_id}\0{sentence}".encode()).hexdigest()[:12]


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def latest(rows: list[dict]) -> dict[str, dict]:
    """Each entry's opening row, carrying the status and note of its last row."""
    entries: dict[str, dict] = {}
    for row in rows:
        if row["id"] in entries and row["status"] != OPEN:
            entries[row["id"]] = {**entries[row["id"]], "status": row["status"],
                                  "note": row.get("note"), "closed_at": row["timestamp"]}
        else:
            entries[row["id"]] = row
    return entries


def record_draft(path: Path, run_id: str | None, thread_id: str, draft_id: str,
                 body: str) -> list[str]:
    """Bring the list in line with the body the mailbox now holds under `draft_id`."""
    current = latest(read(path))
    rows = []
    found = {entry_id(thread_id, draft_id, c["sentence"]): c for c in detect(body)}
    for eid, entry in current.items():
        if entry["draft_id"] == draft_id and entry["status"] == OPEN and eid not in found:
            rows.append({"id": eid, "status": WITHDRAWN, "run_id": run_id})
    for eid, commitment in found.items():
        if eid not in current or current[eid]["status"] == WITHDRAWN:
            rows.append({"id": eid, "status": OPEN, "run_id": run_id, "thread_id": thread_id,
                         "draft_id": draft_id, **commitment})
    _append(path, rows)
    return [row["id"] for row in rows if row["status"] == OPEN]


def settle(path: Path, eid: str, note: str) -> None:
    entry = latest(read(path)).get(eid)
    if entry is None or entry["status"] != OPEN:
        raise KeyError(f"no open commitment {eid}")
    _append(path, [{"id": eid, "status": SETTLED, "note": note}])


def unsettled(path: Path) -> list[dict]:
    return [e for e in latest(read(path)).values() if e["status"] == OPEN]


def _append(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    now = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({"timestamp": now, **row}, ensure_ascii=False) + "\n")
