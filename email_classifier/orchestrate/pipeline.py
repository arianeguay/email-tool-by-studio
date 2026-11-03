"""End-to-end orchestrator core (STU-392).

The `escalated` set built from `match.action == "escalate_to_llm"` is the single
chokepoint, and guarantees two acceptance criteria at once: Stage-1-decided emails never
reach the LLM, and only escalated ones do. It is also what stage 2 is called over — once
per conversation the set touches (STU-944), never once per message.
It is also where the anonymization scope is honoured: what crosses to stage 2 is
tokenized here, by the same `batch.anonymize_escalation` the fan-out uses (STU-1009).
Read-only: this module makes no mailbox mutations and has no code path that could.
"""

import logging
from pathlib import Path

from email_classifier import cost
from email_classifier.anonymize import Tokenizer, deanonymize, record_sent
from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.learn.store import VerdictStore
from email_classifier.orchestrate import batch, composition, conversation
from email_classifier.orchestrate.report import EmailVerdict, Report, build_report
from email_classifier.orchestrate.stage2_client import (
    Stage2Classifier,
    Stage2Result,
    outgoing_payload as stage2_payload,
)
from email_classifier.stage1.frontier_log import FrontierLogger
from email_classifier.stage1.policy import Policy, filter_action
from email_classifier.stage1.rule_engine import Rule, apply

_ESCALATE = "escalate_to_llm"

_log = logging.getLogger("email_classifier.orchestrate")


def dedupe_by_message(emails: list[EmailInput]) -> list[EmailInput]:
    """Collapse to one email per message_id.

    The processing/dedup unit is the individual message, not the thread: a thread
    with N distinct messages must produce N verdicts (e.g. two invoices for two
    different pets share a thread but are distinct objects). message_id is unique
    per real message, so this dedup is a pure idempotency net against the same
    message arriving twice. Keep the most recent occurrence (ISO-8601 UTC dates
    compare correctly) and preserve first-appearance order.
    """
    latest: dict[str, EmailInput] = {}
    order: list[str] = []
    for email in emails:
        existing = latest.get(email.message_id)
        if existing is None:
            order.append(email.message_id)
            latest[email.message_id] = email
        elif email.date > existing.date:
            latest[email.message_id] = email
    return [latest[mid] for mid in order]


def _deterministic_verdict(email: EmailInput, match: RuleMatch) -> EmailVerdict:
    return EmailVerdict(
        message_id=email.message_id,
        thread_id=email.thread_id,
        path="deterministic",
        status="ok",
        bucket=match.bucket,
        suggested_action=match.action,
        confidence=match.confidence,
        rule_id=match.rule_id,
        subtype=None,
        reasoning=None,
        error=None,
        date=email.date,
    )


def _restore(text: str | None, keymap: dict[str, str]) -> str | None:
    """Tokens back to values, on a field that need not carry any.

    `Classification` is only contract-validated by the real stage-2 client, so a
    classifier standing in for it can hand back a null subtype — not worth failing a
    whole run over on a field nothing routes on.
    """
    return deanonymize(text, keymap) if isinstance(text, str) else text


def _llm_verdict(
    email: EmailInput, match: RuleMatch, result: Stage2Result, keymap: dict[str, str]
) -> EmailVerdict:
    if result.ok:
        c = result.classification
        return EmailVerdict(
            message_id=email.message_id,
            thread_id=email.thread_id,
            path="llm",
            status="ok",
            bucket=c.bucket,
            suggested_action=c.suggested_action,
            confidence=c.confidence,
            rule_id=match.rule_id,
            # The model answers in the tokens it was given, so the two free-text fields
            # come back carrying them. Restored here, exactly as `classify-post` does it
            # for the fan-out: the report is the audit corpus and reads in the clear.
            # An empty keymap is the identity — nothing was tokenized to begin with.
            subtype=_restore(c.subtype, keymap),
            reasoning=_restore(c.reasoning, keymap),
            error=None,
            date=email.date,
        )
    return EmailVerdict(
        message_id=email.message_id,
        thread_id=email.thread_id,
        path="llm",
        status="error",
        bucket=None,
        suggested_action=None,
        confidence=None,
        rule_id=match.rule_id,
        subtype=None,
        reasoning=None,
        error=result.error,
        error_detail=result.error_detail,
        date=email.date,
    )


def run(
    emails: list[EmailInput],
    rules: list[Rule],
    policy: Policy,
    stage2: Stage2Classifier,
    logger: FrontierLogger,
    provenance: dict | None = None,
    store: VerdictStore | None = None,
    tokenizer: Tokenizer | None = None,
    sent_log: Path | None = None,
) -> Report:
    # The scope is honoured here, not by the engine's middleware (STU-1009). A caller
    # that sets one and brings no tokenizer would send the mailbox to the LLM in
    # cleartext while the report claimed six tokenized fields, so it is refused rather
    # than defaulted: the safe branch must not be the one you have to remember.
    if policy.anonymization_fields and tokenizer is None:
        raise RuntimeError(
            "anonymization.scope is set but pipeline.run got no tokenizer: stage 2 "
            "would receive the conversation in cleartext"
        )
    unique = dedupe_by_message(emails)
    collapsed = len(emails) - len(unique)
    if collapsed:
        _log.info(
            "deduped %d emails -> %d unique messages (collapsed %d duplicate(s))",
            len(emails), len(unique), collapsed,
        )

    # Stage 1 over the whole run first, so the frontier log is written before any LLM
    # call — a dead Stage 2 never costs the eval corpus its entries.
    matches: dict[str, RuleMatch] = {}
    for email in unique:
        match = filter_action(apply(email, rules), policy)
        logger.log(email, match)
        matches[email.message_id] = match

    # One call per conversation, not per message (STU-944). Its verdict stands for every
    # escalated message of the thread; the store still records each of them, because it
    # is also what tells a catch-up which mail has already been ruled on (STU-954).
    # Not counting them as separate evidence is `promote`'s job, on the thread_id.
    escalated = {mid for mid, m in matches.items() if m.action == _ESCALATE}
    results: dict[str, Stage2Result] = {}
    spend: list[dict | None] = []
    sent: list[dict] = []
    for escalation in conversation.collapse(unique, escalated):
        outgoing = batch.anonymize_escalation(
            escalation, tokenizer, policy.anonymization_fields
        )
        transcript = conversation.transcript(outgoing.history)
        # What an audit reads to settle recall (STU-1036). This path has no input
        # artifact to replay when the caller is the webhook, so the payload is kept
        # here, where it is assembled, rather than reconstructed later.
        sent.append(stage2_payload(outgoing.head, None, transcript))
        result = stage2.classify(outgoing.head, transcript)
        # Per call, not per message: one verdict stands for the whole conversation, and
        # counting it once per message would multiply the run's bill by thread length.
        spend.append(result.spend)
        for message_id in escalation.message_ids:
            results[message_id] = result

    verdicts: list[EmailVerdict] = []
    for email in unique:
        match = matches[email.message_id]
        if email.message_id not in escalated:
            verdicts.append(_deterministic_verdict(email, match))
            continue
        verdict = _llm_verdict(
            email, match, results[email.message_id],
            tokenizer.keymap if tokenizer is not None else {},
        )
        if store is not None and verdict.status == "ok":
            store.record(
                run_id=logger.run_id,
                message_id=email.message_id,
                thread_id=email.thread_id,
                from_address=email.from_address,
                bucket=verdict.bucket,
                suggested_action=verdict.suggested_action,
                confidence=verdict.confidence,
            )
        verdicts.append(verdict)
    # Only under a scope, as the two batch stages do. With none, stage 2 sees the mail
    # in clear by policy and there is no recall to settle — while on the push path the
    # file would be a run-named corpus of mail that never touched disk (STU-859).
    if sent_log is not None and policy.anonymization_fields:
        record_sent(sent_log, sent)
    return build_report(
        logger.run_id, verdicts, provenance, composition.measure(unique, verdicts),
        # One item per `studio run classify`, carrying the per-stage record the client
        # derives from the child's own run payload (STU-1244). `classify-batch` gets
        # that record built, its escalations being `map:` items; here it is translated,
        # so that both orchestrations count a call as a RALPH attempt and one corpus
        # reports the same `llm_calls` whichever ran it.
        # `{}` is the floor for a child that left no record at all — an unparseable
        # stdout or a timeout — and `leg` counts it in `unrecorded_items`, which is
        # what says how much of the number is still in the old unit.
        {cost.CLASSIFY_LEG: cost.leg((item or {} for item in spend),
                                     (provenance or {}).get("provider"))},
    )
