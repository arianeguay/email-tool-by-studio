"""Anonymized-vs-cleartext A/B (STU-837): prove tokenizing PII does not change a verdict.

Stage 1 runs on cleartext whatever the scope, so the two arms can only diverge on the
escalated subset — the A/B classifies exactly those conversations twice per arm and diffs
bucket/action. Conversations, not messages: a run collapses a thread into one call
(STU-944), and its `thread_history` is the widest PII surface the prompt carries, so an
A/B that sent one message at a time would never test it.
The LLM is not deterministic: a borderline message flips between two runs
of the SAME arm. So each arm is repeated and a message only counts as a divergence when
both arms are self-consistent across their repeats and still disagree. Anything unstable
within an arm is model noise, not an anonymization regression, and is reported apart.
"""

import logging
from dataclasses import dataclass
from typing import Callable

from email_classifier.core.types import EmailInput
from email_classifier.orchestrate import conversation
from email_classifier.orchestrate.stage2_client import Stage2Classifier
from email_classifier.stage1.policy import Policy, filter_action
from email_classifier.stage1.rule_engine import Rule, apply

_ESCALATE = "escalate_to_llm"

_log = logging.getLogger("email_classifier.eval.ab")

ClassifierFactory = Callable[[list[str]], Stage2Classifier]


@dataclass(frozen=True)
class Verdict:
    bucket: str | None
    action: str | None
    error: str | None = None


@dataclass
class Arm:
    name: str
    fields: list[str]
    repeats: list[dict[str, Verdict]]


@dataclass
class Divergence:
    message_id: str
    left: Verdict
    right: Verdict


@dataclass
class ABResult:
    left: str
    right: str
    escalated: list[str]
    repeats: int
    divergences: list[Divergence]
    unstable: list[str]  # not self-consistent across repeats in at least one arm
    errored: list[str]   # at least one stage-2 error in at least one arm


def escalations(
    emails: list[EmailInput], rules: list[Rule], policy: Policy
) -> list[conversation.Escalation]:
    """What the LLM is asked, exactly as a run builds it."""
    escalated = {e.message_id for e in emails
                 if filter_action(apply(e, rules), policy).action == _ESCALATE}
    return conversation.collapse(emails, escalated)


def run_arm(
    name: str,
    fields: list[str],
    conversations: list[conversation.Escalation],
    factory: ClassifierFactory,
    repeats: int,
) -> Arm:
    classifier = factory(fields)
    rounds: list[dict[str, Verdict]] = []
    for index in range(repeats):
        _log.info("arm %s repeat %d/%d — %d conversation(s)",
                  name, index + 1, repeats, len(conversations))
        round_: dict[str, Verdict] = {}
        for escalation in conversations:
            verdict = _classify(
                classifier, escalation.head, conversation.transcript(escalation.history)
            )
            # One verdict, every message it covers — the divergence report stays keyed
            # by message, which is what the golden and the reader both use.
            for message_id in escalation.message_ids:
                round_[message_id] = verdict
        rounds.append(round_)
    return Arm(name=name, fields=fields, repeats=rounds)


def _classify(
    classifier: Stage2Classifier, email: EmailInput, thread_history: str
) -> Verdict:
    result = classifier.classify(email, thread_history)
    if not result.ok:
        return Verdict(bucket=None, action=None, error=result.error)
    return Verdict(
        bucket=result.classification.bucket, action=result.classification.suggested_action
    )


def _settled(arm: Arm, message_id: str) -> Verdict | None:
    """The arm's verdict when every repeat agreed and none errored, else None."""
    seen = {round_[message_id] for round_ in arm.repeats if message_id in round_}
    if len(seen) != 1:
        return None
    verdict = seen.pop()
    return None if verdict.error else verdict


def _errored(arm: Arm, message_id: str) -> bool:
    return any(round_.get(message_id, Verdict(None, None)).error for round_ in arm.repeats)


def compare(left: Arm, right: Arm, message_ids: list[str]) -> ABResult:
    divergences, unstable, errored = [], [], []
    for message_id in message_ids:
        if _errored(left, message_id) or _errored(right, message_id):
            errored.append(message_id)
            continue
        a, b = _settled(left, message_id), _settled(right, message_id)
        if a is None or b is None:
            unstable.append(message_id)
        elif a != b:
            divergences.append(Divergence(message_id=message_id, left=a, right=b))
    return ABResult(
        left=left.name,
        right=right.name,
        escalated=message_ids,
        repeats=len(left.repeats),
        divergences=divergences,
        unstable=unstable,
        errored=errored,
    )


def _fmt(verdict: Verdict) -> str:
    return f"{verdict.bucket or '-'}/{verdict.action or '-'}"


def render(result: ABResult) -> str:
    lines = [
        f"=== A/B {result.left} vs {result.right} ===",
        f"{len(result.escalated)} escalated message(s) | {result.repeats} repeat(s) per arm | "
        f"{len(result.divergences)} stable divergence(s)",
    ]
    lines += [
        f"  DIVERGE {d.message_id}: {result.left}={_fmt(d.left)} -> "
        f"{result.right}={_fmt(d.right)}"
        for d in result.divergences
    ]
    if result.unstable:
        lines.append(
            "  unstable across repeats (model noise, not compared): "
            + ", ".join(result.unstable)
        )
    if result.errored:
        lines.append("  stage-2 error (not compared): " + ", ".join(result.errored))
    return "\n".join(lines)
