"""Eval harness core (STU-816): replay a corpus through stage 1, diff against golden labels.

A golden label is the reviewed verdict for one message, or — since STU-936 — for one
conversation. A flip is any divergence between golden and replay: a `path` flip
(deterministic <-> llm) is a deterministic-ratio regression or gain, a `verdict` flip is a
bucket/action change. Pure module — no LLM call: stage-2 verdicts come from a cached run
report, never from a live escalation.

A conversation label is scored against the replay reconciled by the STU-940 rule, which
lives in `orchestrate/conversation.py` and is stated there once. Only the corpus knows
which messages share a thread and in what order, so scoring them needs `emails`; called
without it, conversation labels are carried and not scored, as before STU-940.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from email_classifier.core import schema
from email_classifier.core.contract import Taxonomy
from email_classifier.core.types import EmailInput
from email_classifier.orchestrate import conversation
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.stage1.policy import Policy, filter_action
from email_classifier.stage1.rule_engine import Rule, apply

_ESCALATE = "escalate_to_llm"


@dataclass(frozen=True)
class Label:
    """The reviewed verdict for one message, keyed by `message_id`.

    A label whose `unit` is `"thread"` is keyed by `thread_id` instead and states the
    outcome the reviewer wants for the whole conversation. It is scored against the
    replay reconciled by the STU-940 rule — a conversation none of whose messages stage 1
    decides reconciles to no verdict at all, so only its `path` is comparable.
    """

    path: str            # "deterministic" | "llm"
    bucket: str | None
    action: str | None   # None: escalated with no cached stage-2 verdict
    unit: str = "message"  # "message" | "thread"


@dataclass
class Flip:
    message_id: str
    kind: str            # "path" | "verdict"
    expected: Label
    actual: Label


@dataclass
class EvalResult:
    total: int
    labeled: int
    uncovered: list[str]   # corpus messages with no golden label
    absent: list[str]      # golden labels whose message is not in the corpus
    deterministic: int
    ratio_deterministic: float
    correct: int
    precision: float
    flips: list[Flip]
    newly_deterministic: int  # golden escalated, replay decides it — the ratio gain
    newly_escalated: int      # golden decided, replay escalates it — the ratio regression
    conversations: list[str]  # thread-unit golden labels covered by the run
    conversations_scored: int   # 0 when `evaluate` got no corpus to reconcile them with


def load_corpus(path: Path) -> list[EmailInput]:
    """A directory of single-email JSON files, or one JSON array dump."""
    if path.is_dir():
        return [schema.load(p) for p in sorted(path.rglob("*.json"))]
    return schema.load_many(path)


def load_golden(path: Path) -> dict[str, Label]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {message_id: Label(**label) for message_id, label in data.items()}


def write_golden(path: Path, labels: dict[str, Label]) -> None:
    data = {
        key: {"path": label.path, "bucket": label.bucket, "action": label.action}
        | ({"unit": label.unit} if label.unit != "message" else {})
        for key, label in sorted(labels.items())
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def labels_from_report(report: dict) -> dict[str, Label]:
    """Seed golden labels from a reviewed run report. Error verdicts froze no verdict and
    are skipped."""
    return {
        v["message_id"]: Label(path=v["path"], bucket=v["bucket"], action=v["suggested_action"])
        for v in report["verdicts"]
        if v["status"] == "ok"
    }


def replay(
    emails: list[EmailInput],
    rules: list[Rule],
    policy: Policy,
    stage2_cache: dict[str, Label] | None = None,
) -> dict[str, Label]:
    cache = stage2_cache or {}
    labels: dict[str, Label] = {}
    for email in emails:
        match = filter_action(apply(email, rules), policy)
        if match.action == _ESCALATE:
            labels[email.message_id] = cache.get(email.message_id) or Label(
                path="llm", bucket=None, action=None
            )
        else:
            labels[email.message_id] = Label(
                path="deterministic", bucket=match.bucket, action=match.action
            )
    return labels


def _flip_kind(expected: Label, actual: Label) -> str | None:
    if expected.path != actual.path:
        return "path"
    if expected.action is None or actual.action is None:
        return None  # escalation with no cached verdict on a side: path is all that is known
    if (expected.bucket, expected.action) != (actual.bucket, actual.action):
        return "verdict"
    return None


def _flips(actual: dict[str, Label], golden: dict[str, Label]) -> list[Flip]:
    return [
        Flip(message_id=key, kind=kind, expected=golden[key], actual=label)
        for key, label in sorted(actual.items())
        if key in golden and (kind := _flip_kind(golden[key], label)) is not None
    ]


def conversation_labels(golden: dict[str, Label]) -> dict[str, Label]:
    """The thread-unit labels of a golden file. No replay produces one, so a re-freeze
    that does not carry them across deletes them."""
    return {key: label for key, label in golden.items() if label.unit == "thread"}


def thread_labels(
    emails: list[EmailInput], labels: dict[str, Label], taxonomy: Taxonomy
) -> dict[str, Label]:
    """The conversation labels a replay implies, under the STU-940 rule.

    The message labels are lifted into report verdicts so the rule is read from its one
    home rather than restated here. A conversation stage 1 does not decide whole
    reconciles to nothing, and its label says only that it takes the LLM. `taxonomy` is
    this deployment's own bucket shape (STU-1461), threaded straight to `reconcile`.
    """
    verdicts = [
        EmailVerdict(
            message_id=email.message_id, thread_id=email.thread_id,
            path=labels[email.message_id].path, status="ok",
            bucket=labels[email.message_id].bucket,
            suggested_action=labels[email.message_id].action,
            confidence=None, rule_id=None, subtype=None, reasoning=None, error=None,
            date=email.date,
        )
        for email in emails
        if email.message_id in labels
    ]
    reconciled = {}
    for thread_id, messages in conversation.group(verdicts).items():
        decision = conversation.reconcile(messages, taxonomy)
        reconciled[thread_id] = Label(
            path="deterministic" if all(m.path == "deterministic" for m in messages) else "llm",
            bucket=decision.bucket if decision else None,
            action=decision.suggested_action if decision else None,
            unit="thread",
        )
    return reconciled


def evaluate(
    actual: dict[str, Label],
    golden: dict[str, Label],
    emails: list[EmailInput] | None = None,
    taxonomy: Taxonomy | None = None,
) -> EvalResult:
    """`taxonomy` is this deployment's own bucket shape (STU-1461), needed only to
    reconcile conversation-unit labels. Neither it nor `emails` defaults the other:
    without both, thread scoring is skipped exactly as it was before either existed —
    an explicit narrower answer, not another deployment's taxonomy standing in for a missing one.
    """
    total = len(actual)
    deterministic = sum(1 for label in actual.values() if label.path == "deterministic")
    conversations = conversation_labels(golden)
    # The two units share one key space and Gmail spells a lone message's thread id with
    # its message id, so they are compared side by side and never merged into one dict.
    golden = {key: label for key, label in golden.items() if label.unit == "message"}
    threads = (
        thread_labels(emails, actual, taxonomy)
        if conversations and emails is not None and taxonomy is not None
        else {}
    )
    scored_threads = {t: threads[t] for t in conversations if t in threads}
    flips = _flips(actual, golden) + _flips(scored_threads, conversations)
    absent = [key for key in golden if key not in actual]
    if threads:
        absent += [t for t in conversations if t not in threads]
    labeled = sum(1 for key in actual if key in golden) + len(scored_threads)
    correct = labeled - len(flips)
    return EvalResult(
        total=total,
        labeled=labeled,
        uncovered=sorted(message_id for message_id in actual if message_id not in golden),
        absent=sorted(absent),
        deterministic=deterministic,
        ratio_deterministic=(deterministic / total if total else 0.0),
        correct=correct,
        precision=(correct / labeled if labeled else 0.0),
        flips=flips,
        newly_deterministic=sum(
            1 for f in flips if f.kind == "path" and f.actual.path == "deterministic"
        ),
        newly_escalated=sum(1 for f in flips if f.kind == "path" and f.actual.path == "llm"),
        conversations=sorted(conversations),
        conversations_scored=len(scored_threads),
    )


def format_label(label: Label) -> str:
    return f"{label.path}/{label.bucket or '-'}/{label.action or '-'}"


def render(result: EvalResult) -> str:
    # `total` counts messages and `labeled` counts labels of both units, so the split is
    # spelled out rather than left as arithmetic that does not work.
    breakdown = (
        f" ({result.labeled - result.conversations_scored} messages "
        f"+ {result.conversations_scored} conversations)"
        if result.conversations_scored
        else ""
    )
    lines = [
        "=== Eval vs golden ===",
        f"{result.total} messages | {result.deterministic} deterministic "
        f"({round(result.ratio_deterministic * 100)}%) | {result.labeled} labeled{breakdown}",
        f"precision {round(result.precision * 100)}% ({result.correct}/{result.labeled}) | "
        f"{len(result.flips)} flip(s) | +{result.newly_deterministic} deterministic / "
        f"-{result.newly_escalated} escalated",
    ]
    lines += [
        f"  FLIP[{f.kind}] {f.message_id}: {format_label(f.expected)} -> {format_label(f.actual)}"
        for f in result.flips
    ]
    if result.uncovered:
        lines.append(f"  uncovered (no golden label): {', '.join(result.uncovered)}")
    if result.absent:
        lines.append(f"  absent from corpus: {', '.join(result.absent)}")
    if result.conversations:
        how = "scored, reconciled per STU-940" if result.conversations_scored else (
            "carried, not scored — no corpus was passed to reconcile them"
        )
        lines.append(
            f"  {len(result.conversations)} conversation label(s) {how}: "
            f"{', '.join(result.conversations)}"
        )
    return "\n".join(lines)
