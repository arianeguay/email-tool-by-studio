"""Shared logic of the `classify-batch` pipeline (STU-843).

Studio orchestrates and Python is the leaf: `classify-pre` splits the corpus and builds
the fan-out items, the engine's `map:` runs the unchanged `classify` pipeline once per
escalated email, `classify-post` turns the collected verdicts into the report. This
module holds what both scripts need, so the scripts stay stdin/stdout adapters.

Pure module — no I/O beyond hashing the config files a fingerprint covers.
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import yaml

from email_classifier import prompt_sources
from email_classifier.anonymize import Tokenizer, anonymize_fields, deanonymize
from email_classifier.core.contract import Taxonomy, validate_classification
from email_classifier.core.types import RuleMatch
from email_classifier.orchestrate import conversation
from email_classifier.orchestrate.report import EmailVerdict

# The head message's fields, as `classify` has always received them; the conversation
# rides alongside in `thread_history`.
_EMAIL_FIELDS = (
    "thread_id", "from_address", "subject", "headers", "body_snippet",
    "date", "provider_category",
)

_CLASSIFIER_AGENT = "classifier"

# What a fingerprint covers besides the classifier's own skills, read off its agent
# YAML rather than re-listed here (STU-1458): everything that shapes an LLM verdict but
# is not the email. `../rules/taxonomy.yaml` (STU-1461) is the one entry outside
# `.studio/`: which bucket/action/subtype pairs the RALPH validator accepts moved out
# of `core/contract.py`'s literals into this deployment's own file, so it shapes a
# verdict exactly as much as the contract YAML two lines up — an edit to either now
# means the same thing to the cache. The cost is named and accepted, not measured
# here: production owes one stage-2 re-pay over the grey zone the next time this
# lands, and that repay is a deploy-time consequence, not something this fingerprint's
# own tests need to trigger.
_PROMPT_SOURCES = (
    "agents/classifier.agent.yaml",
    "contracts/classification.contract.yaml",
    "pipelines/classify.pipeline.yaml",
    "invariants.md",
    "../rules/taxonomy.yaml",
)


def _default_prompt_sources(studio_dir: Path) -> tuple[str, ...]:
    """The default: the classifier agent's declared skills, plus the explicit
    contract/pipeline/invariants no agent declaration can name."""
    skills = prompt_sources.skill_paths(studio_dir, _CLASSIFIER_AGENT)
    return tuple(dict.fromkeys((*_PROMPT_SOURCES[:1], *skills.values(), *_PROMPT_SOURCES[1:])))


# A YAML source is covered by its *parsed document*, everything else by its bytes
# (STU-1149). The fingerprint rides in the fan-out item, and the item is the `resume:`
# key of the `map:` stage, so whatever it covers is what a corpus re-pays stage 2 for.
# Hashing the bytes of `agents/classifier.agent.yaml` made a `# ...` line — text no
# model ever sees — re-price every escalated conversation, and that file is where this
# repo writes its measurements down: of the last three commits touching it, one was
# 34 changed lines and 34 of them comments. The scope the fingerprint claims is
# "everything that shapes an LLM verdict but is not the email", so the bust was the
# fingerprint failing its own contract; the cost was the symptom.
#
# `.md` sources keep byte hashing, and not by omission: `invariants.md` and
# `skills/taxonomy.skill.md` reach the prompt verbatim, so in them a comment is prose
# the model reads and a reflow is a different prompt.
_PARSED_SUFFIXES = (".yaml", ".yml")


def _source_digest(path: Path) -> bytes:
    """What a single source contributes to the fingerprint.

    Mapping keys are sorted and sequences are left alone, which is the order decision
    made once for every YAML here rather than inherited: re-ordering an agent's keys
    changes nothing that reaches a model, while `required_fields` is a sequence and
    stays order-sensitive — a contract that lists its fields differently is a different
    contract, and no canonicalization gets to decide otherwise.

    Unparseable YAML falls back to its bytes rather than raising. This function's job is
    to never under-cover; a broken `.studio/` file is the loader's error to report, with
    a better message than a fingerprint could give, and until it does the run is safest
    missing the cache.
    """
    if not path.exists():
        return b""
    raw = path.read_bytes()
    if path.suffix not in _PARSED_SUFFIXES:
        return raw
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw
    return yaml.safe_dump(
        parsed, sort_keys=True, default_flow_style=False, allow_unicode=True
    ).encode("utf-8")


def prompt_fingerprint(
    studio_dir: Path,
    anonymization_fields: list[str],
    sources: tuple[str, ...] | None = None,
) -> str:
    """Fingerprint of everything the LLM sees except the email itself.

    A cached verdict is only replayable under the prompt that produced it, so editing
    the agent's own prompt, model, skills or contract, the taxonomy skill or the
    invariants must miss the cache — while prose *about* the agent replays for free.
    The anonymization scope is in here too: the same email tokenized differently is a
    different question. The drafts fan-out passes its own `sources` (STU-850).

    `sources` defaults to `None` rather than to a module constant: the classifier's
    skills are read off its own agent YAML under `studio_dir`, which a static default
    cannot do since it would fix `studio_dir` at import time rather than at call time.
    """
    if sources is None:
        sources = _default_prompt_sources(studio_dir)
    h = hashlib.sha256()
    for relative in sources:
        h.update(_source_digest(studio_dir / relative))
        h.update(b"\x00")
    h.update(json.dumps(sorted(anonymization_fields)).encode("utf-8"))
    return h.hexdigest()


def stage1_fingerprint(matches: list[RuleMatch]) -> str:
    """Fingerprint of the rules' effect *on this conversation*, not of the whole rule set.

    Hashing `rules/` wholesale would invalidate every cached verdict on any rule edit —
    including the promotions this repo makes routinely (STU-822). What actually changes
    an escalated conversation's question is the Stage-1 outcome of each message it
    escalated: a rule that starts deciding one drops it from the call, and a rule that
    changes which match escalated it changes this hash. Everything else legitimately
    replays — a decided sibling's bucket included, since the call sees its body, never
    its verdict.
    """
    payload = [[m.rule_id, m.action, m.action_original, m.confidence, m.bucket]
               for m in matches]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


def anonymize_escalation(
    escalation: conversation.Escalation,
    tokenizer: Tokenizer | None,
    anonymization_fields: list[str],
) -> conversation.Escalation:
    """The escalation as stage 2 must receive it: head and history tokenized.

    Both orchestrations call this, so a scope means the same thing whichever one runs
    (STU-1009). `pipeline.run` used to hand Studio the cleartext conversation and
    declare the scope for the engine's middleware to honour instead — but that
    detector anchors a name on a salutation, which is the one place French mail never
    puts one, so every parent's name on the push path reached the LLM in clear. That
    is the same finding that moved names to GLiNER here (STU-890); the fan-out was
    covered and the per-email path was not.

    A turn is tokenized whenever any scope is set — it is composed of the very fields a
    scope names, and over-tokenizing a block the child only reads costs nothing, while
    under-tokenizing it sends real names to the LLM.

    Each turn is tokenized on its own, never the rendered transcript: a body truncated
    mid-name ends the text it was cut from, and only there does the trailing-fragment
    veto see it (STU-920). Joining first would bury every turn's tail mid-string and
    mint `Carolin` beside `Caroline` into the keymap this run persists.

    History before head, and the head's fields in the scope's own order: that order is
    what decides which value gets which token, and the item below is the fan-out's
    resume key. It also means a name first minted in the head was unknown to every
    turn before it, so the turns are settled against the keymap once the head is done
    (STU-1993).
    """
    if not anonymization_fields or tokenizer is None:
        return escalation
    history = [
        replace(turn,
                from_address=tokenizer.anonymize(turn.from_address),
                body_snippet=tokenizer.anonymize(
                    turn.body_snippet, from_address=turn.from_address
                ))
        for turn in escalation.history
    ]
    head: dict = {field: getattr(escalation.head, field) for field in _EMAIL_FIELDS}
    anonymize_fields(
        head, tokenizer, anonymization_fields, from_address=escalation.head.from_address
    )
    history = [
        replace(turn, body_snippet=tokenizer.redact_known(turn.body_snippet))
        for turn in history
    ]
    return replace(escalation, head=replace(escalation.head, **head), history=history)


def escalation_item(
    escalation: conversation.Escalation,
    matches: list[RuleMatch],
    tokenizer: Tokenizer,
    anonymization_fields: list[str],
    prompt_fp: str,
) -> dict:
    """One fan-out item: the child's input, and therefore its resume key.

    One item per conversation, not per message (STU-944): the head is the escalation's
    most recent message and `thread_history` is the rest of the conversation this run
    fetched.
    """
    escalation = anonymize_escalation(escalation, tokenizer, anonymization_fields)
    item: dict = {field: getattr(escalation.head, field) for field in _EMAIL_FIELDS}
    item["thread_history"] = conversation.transcript(escalation.history)
    item["prompt_fingerprint"] = prompt_fp
    item["stage1_fingerprint"] = stage1_fingerprint(matches)
    return item


def deterministic_verdict(record: dict) -> EmailVerdict:
    return EmailVerdict(
        message_id=record["message_id"],
        thread_id=record["thread_id"],
        path="deterministic",
        status="ok",
        bucket=record["bucket"],
        suggested_action=record["action"],
        confidence=record["confidence"],
        rule_id=record["rule_id"],
        subtype=None,
        reasoning=None,
        error=None,
        date=record["date"],
        expires_at=record.get("expires_at", ""),
    )


def _error_verdict(record: dict, error: str, detail: str | None) -> EmailVerdict:
    return EmailVerdict(
        message_id=record["message_id"],
        thread_id=record["thread_id"],
        path="llm",
        status="error",
        bucket=None,
        suggested_action=None,
        confidence=None,
        rule_id=record["rule_id"],
        subtype=None,
        reasoning=None,
        error=error,
        error_detail=detail,
        date=record["date"],
    )


def llm_verdict(
    record: dict, result: dict | None, keymap: dict[str, str], taxonomy: Taxonomy
) -> EmailVerdict:
    """One escalated email's verdict, from its fan-out item result.

    `result` is None when the whole fan-out never produced output — a condition-skipped
    or tolerated-failed `call` stage. That is the STU-392 bias preserved: the email is
    recorded as a Stage-2 error, and the run still completes.
    """
    if result is None:
        return _error_verdict(record, "studio_run_output_missing", None)
    if result.get("status") != "success":
        return _error_verdict(record, "studio_run_failed", result.get("error"))

    output = result.get("output")
    if not isinstance(output, dict):
        return _error_verdict(record, "studio_run_output_missing", None)

    # Defence in depth: RALPH already validated, but never trust an LLM output blindly —
    # fail visibly rather than misroute.
    validation = validate_classification(output, taxonomy)
    if not validation.valid:
        return _error_verdict(record, "studio_run_output_invalid", "; ".join(validation.errors))

    return EmailVerdict(
        message_id=record["message_id"],
        thread_id=record["thread_id"],
        path="llm",
        status="ok",
        bucket=output["bucket"],
        suggested_action=output["suggested_action"],
        confidence=output["confidence"],
        rule_id=record["rule_id"],
        subtype=deanonymize(output["subtype"], keymap),
        reasoning=deanonymize(output["reasoning"], keymap),
        error=None,
        date=record["date"],
    )


def results_by_index(map_output: dict | None) -> dict[int, dict]:
    """Index the fan-out's per-item results; `outputs` holds successes only."""
    if not isinstance(map_output, dict):
        return {}
    results = map_output.get("results")
    if not isinstance(results, list):
        return {}
    return {r["index"]: r for r in results if isinstance(r, dict) and "index" in r}
