"""Name-detection gate (STU-925): the person spans the detector must find in a corpus.

The `ner` pins in `pyproject.toml` are the whole guard on `anonymization.scope: full` —
tokenization is upstream of every span GLiNER returns, so a `transformers` minor bump
moves recall between two runs with nothing in the repo changing (STU-919). Nothing
failed on that: `provenance.detector_stack` records the drift only after a run has
already shipped mail to an LLM, and `eval/golden.json` scores rule outcomes, not spans.

This freezes the detected set against a deployment's synthetic, public corpus,
so the expectation can live in the repo the way the golden labels do. A missing span and
an unexpected one are both failures: STU-909 and STU-911 were over-redaction bugs, and a
false positive costs the drafter a fact it needed (STU-910).

Every field `anonymization.scope: full` tokenizes is scanned, header values included —
they carry no prose today, which is itself the expectation a name appearing in one
would break.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from email_classifier.anonymize import person_spans
from email_classifier.core.types import EmailInput
from email_classifier.stage1.policy import ANONYMIZATION_SCOPES

FIELDS = ANONYMIZATION_SCOPES["full"]


@dataclass(frozen=True, order=True)
class Span:
    start: int
    end: int
    text: str


@dataclass
class Divergence:
    message_id: str
    field: str
    kind: str        # "missing" | "unexpected"
    span: Span


@dataclass
class SpanResult:
    messages: int
    spans: int
    uncovered: list[str]   # corpus messages with no golden entry
    absent: list[str]      # golden entries whose message is not in the corpus
    divergences: list[Divergence]


def _spans(text: str) -> list[Span]:
    return [Span(start, end, text[start:end]) for start, end in person_spans(text)]


def detect(email: EmailInput) -> dict[str, list[Span]]:
    """The person spans of one email, by field. Fields with none are left out."""
    found: dict[str, list[Span]] = {}
    for field in FIELDS:
        value = getattr(email, field, None)
        if isinstance(value, str):
            found[field] = _spans(value)
        elif isinstance(value, dict):
            found |= {f"{field}.{key}": _spans(v) for key, v in value.items()}
    return {field: spans for field, spans in found.items() if spans}


def detect_all(emails: list[EmailInput]) -> dict[str, dict[str, list[Span]]]:
    return {email.message_id: detect(email) for email in emails}


def load_golden(path: Path) -> dict[str, dict[str, list[Span]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        message_id: {field: [Span(*s) for s in spans] for field, spans in fields.items()}
        for message_id, fields in data.items()
    }


def write_golden(path: Path, detected: dict[str, dict[str, list[Span]]]) -> None:
    data = {
        message_id: {
            field: [[s.start, s.end, s.text] for s in spans]
            for field, spans in sorted(fields.items())
        }
        for message_id, fields in sorted(detected.items())
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compare(
    detected: dict[str, dict[str, list[Span]]], golden: dict[str, dict[str, list[Span]]]
) -> SpanResult:
    divergences = []
    for message_id in sorted(detected.keys() & golden.keys()):
        found, wanted = detected[message_id], golden[message_id]
        for field in sorted(found.keys() | wanted.keys()):
            actual, expected = set(found.get(field, [])), set(wanted.get(field, []))
            divergences += [
                Divergence(message_id, field, "missing", span)
                for span in sorted(expected - actual)
            ]
            divergences += [
                Divergence(message_id, field, "unexpected", span)
                for span in sorted(actual - expected)
            ]
    return SpanResult(
        messages=len(detected),
        spans=sum(len(s) for fields in detected.values() for s in fields.values()),
        uncovered=sorted(detected.keys() - golden.keys()),
        absent=sorted(golden.keys() - detected.keys()),
        divergences=divergences,
    )


def render(result: SpanResult) -> str:
    lines = [
        "=== Name detection vs golden spans ===",
        f"{result.messages} messages | {result.spans} person span(s) | "
        f"{len(result.divergences)} divergence(s)",
    ]
    lines += [
        f"  {d.kind.upper()} {d.message_id} {d.field} [{d.span.start}:{d.span.end}] "
        f"{d.span.text!r}"
        for d in result.divergences
    ]
    if result.uncovered:
        lines.append(f"  uncovered (no golden entry): {', '.join(result.uncovered)}")
    if result.absent:
        lines.append(f"  absent from corpus: {', '.join(result.absent)}")
    return "\n".join(lines)
