"""Stage 2 boundary (STU-392): classify one escalated conversation via the Studio CLI.

The orchestrator depends only on the Stage2Classifier Protocol. The real impl shells
out to `studio run classify --input-file <tmp> --json` per conversation (STU-944) and
parses the result, mirroring wiki-creator's relationship_extraction.py. Errors are
returned as data (a tag on Stage2Result), never raised — the orchestrator records them
per-email and continues.
No Anthropic SDK here: Studio makes the LLM call.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from email_classifier import alerts
from email_classifier.anonymize import missing_detector
from email_classifier.core.contract import Taxonomy, validate_classification
from email_classifier.core.types import Classification, EmailInput

_PIPELINE = "classify"
_STAGE_NAME = "classify"
# What `classifier.agent.yaml` interpolates its `model` from. Unset there, so a run reads
# `defaults.model`; the bake-off sets it per arm (STU-1045).
_MODEL_ENV = "CLASSIFY_MODEL"
_DEFAULT_TIMEOUT = 180

# Stage-2 diagnostics. Quiet by default (the orchestrator/CLI configures handlers);
# run_email.py wires this to stderr, and -v raises it to DEBUG.
_log = logging.getLogger("email_classifier.stage2")

_TAIL = 800  # chars of captured studio output to surface in logs

# First Studio release that reads `anonymize_fields` from the input file
# (studio-foundation/studio#265). Older builds treat it as an ordinary task field: the
# list lands in the prompt and, with no scope reaching the middleware, every field is
# anonymized instead of the scoped ones — silently (STU-834).
_MIN_SCOPE_VERSION = (0, 16, 0)

# First Studio release that spawns the `claude` CLI with `--setting-sources ''` and
# CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 (studio-foundation/studio#271). Below it, every
# agent call inherits the operator's ~/.claude and the operated repo's CLAUDE.md
# through a cwd the spawn never sets (STU-863, STU-1115).
_MIN_SEALED_PROMPT_VERSION = (0, 17, 1)


def _tail(text: str | None) -> str:
    """Last _TAIL chars of captured output, single-lined, for log lines."""
    if not text:
        return ""
    text = text.strip()
    clipped = text[-_TAIL:]
    prefix = "…" if len(text) > _TAIL else ""
    return prefix + clipped.replace("\n", "\\n")


_REDACTED = "[redacted]"

# The input fields the child echoes back that carry mail (STU-1010). `thread_id` is not
# one of them: it is the handle a failure is diagnosed by, and it names a conversation,
# never a person.
_MAIL_FIELDS = ("from_address", "subject", "headers", "body_snippet", "thread_history")

# Under this length a sent value is not identifying, and redacting it would shred the
# log — a one-character subject would blank every "S" in a stack trace. The keyed pass
# below still covers a short value where it is actually echoed back.
_MIN_REDACTED_LEN = 4

# `"body_snippet": "…"` in whatever the child printed, terminated or cut off mid-value:
# a killed studio leaves its JSON half-written, and the half of a body that made it out
# is still a body. Two shapes, because `headers` comes back as an object (flat, so no
# nesting to balance) where the rest come back as strings.
_KEYED_MAIL = re.compile(
    r'"(' + "|".join(_MAIL_FIELDS) + r')"\s*:\s*(?:'
    r'"(?:[^"\\]|\\.)*(?:"|\Z)'
    r'|\{(?:[^{}"]|"(?:[^"\\]|\\.)*")*(?:\}|\Z)'
    r')'
)

# What a run record says about its own failure. A whitelist, because a blacklist would
# have to know every place a future studio release decides to echo the input back.
_DIAGNOSTIC_KEYS = ("status", "error", "errors", "message", "attempts", "retries")

# The error tag for a failure the provider caused rather than the contract (STU-1016).
# Not `studio_*` like the others: studio ran, the pipeline ran, and what came back was
# the provider declining to answer at all.
PROVIDER_UNAVAILABLE = "provider_unavailable"

# What the provider answers instead of doing the work. Under `defaults.provider:
# claude-code` studio spawns the local CLI and returns whatever it printed, so a spent
# account session limit arrives as the agent's own output — RALPH then reads a string
# where the contract wants an object, retries twice against the same wall, and the stage
# fails as a contract violation (measured on the serving host, run 119a588c: three attempts,
# `total_tokens` 0 on each, which is the tell — nothing was spent because nothing was
# asked).
#
# Two wordings of one notice, and no more: a pattern for a message nobody has seen the
# provider print would name a failure mode on speculation, and a false positive here
# silences a real contract bug behind a "wait for the limit" alert. The optional reset
# clause is matched, not just skipped, because when the classification comes back is the
# only part of the notice that is actionable — and the separator in front of it is left
# open rather than named, the `\u00b7` the CLI prints reaching us as its six-character
# escape once `--json` has been through it.
_NOTICE = re.compile(
    r"(?:you(?:['\u2019]ve|\s+have)?\s+)?"
    r"(?:(?:hit|reached)\s+your\s+(?:session|usage)\s+limit"
    r"|(?:session|usage)\s+limit\s+reached)"
    r"(?:[^\"\n]{0,12}?resets?\s[^\"\n]{0,40})?",
    re.IGNORECASE,
)


# `--json` prints the notice with its non-ASCII escaped, and the alert this becomes is
# read on a phone. Lone surrogates are left as written: they would not survive the POST.
_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescaped(match: re.Match) -> str:
    codepoint = int(match[1], 16)
    return match[0] if 0xD800 <= codepoint <= 0xDFFF else chr(codepoint)


# The same fault from the other provider (STU-1013). On `anthropic` there is no CLI to
# answer in prose: the API refuses, the SDK raises, and the engine records it on the
# agent run instead of in the agent's output. Measured with an invalid key --
#
#   "error": "401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\",
#   \"message\":\"API key is invalid.\"},\"request_id\":null}"
#
# -- so what is matched is the HTTP status the engine prefixes and the error type the
# envelope carries, both of which are structure rather than wording. A list of messages
# would be a guess at the ones nobody has seen; a status and a type are what the shape
# guarantees. Quotes are optionally backslashed because the same record reaches us both
# raw and once-escaped depending on where in the child's output it was found.
#
# One attempt, not the three a session limit produced: RALPH does not retry a transport
# error, so nothing inside the run absorbs it either.
_Q = r'\\?"'  # a quote as printed, or as escaped once inside the record's own JSON
_API_ERROR = re.compile(
    r"([45]\d\d)\s*\{.{0,400}?"
    + _Q + "error" + _Q + r"\s*:\s*\{.{0,200}?"
    + _Q + "type" + _Q + r"\s*:\s*" + _Q + r"(\w+)",
    re.DOTALL,
)


def provider_unavailable_notice(text: str) -> str | None:
    """The provider's own "not right now" answer inside `text`, or None.

    Only the match itself is returned, never the text around it: this string is logged,
    alerted and kept in the report, and the stream it was found in is the child's whole
    output.
    """
    match = _NOTICE.search(text)
    if match is not None:
        return " ".join(_JSON_ESCAPE.sub(_unescaped, match[0]).split())
    match = _API_ERROR.search(text)
    if match is None:
        return None
    return f"HTTP {match[1]} {match[2]}"


def _forms(value: str) -> list[str]:
    """One sent value in every encoding the child could echo it back in.

    Its own lines are in there too: a record cut short mid-echo carries only some of
    them, and half a body is a body.
    """
    candidates = [
        value,
        json.dumps(value)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        *value.splitlines(),
    ]
    return [c.strip() for c in candidates if len(c.strip()) >= _MIN_REDACTED_LEN]


def _mail_values(payload: dict) -> tuple[str, ...]:
    """The strings we sent the child that must not come back out in a log line."""
    values: list[str] = []
    for field in _MAIL_FIELDS:
        sent = payload.get(field)
        if isinstance(sent, str):
            values.append(sent)
        elif isinstance(sent, dict):  # headers: the names are not the secret, the values are
            values += [v for v in sent.values() if isinstance(v, str)]
    forms = {form for value in values for form in _forms(value)}
    # Longest first, so a whole body goes as one span before its own lines do.
    return tuple(sorted(forms, key=len, reverse=True))


def _scrub(text: str | None, mail: tuple[str, ...]) -> str:
    """`text` with everything we sent the child taken back out of it (STU-1010)."""
    if not text:
        return ""
    text = _KEYED_MAIL.sub(lambda m: f'"{m[1]}": "{_REDACTED}"', text)
    for value in mail:
        text = text.replace(value, _REDACTED)
    return text


def _run_record(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        payload = json.loads(text)
    except ValueError:  # json.JSONDecodeError is a subclass
        return None
    return payload if isinstance(payload, dict) else None


def _diagnostics(fields: dict, mail: tuple[str, ...]) -> str:
    return " ".join(
        f"{key}={_tail(_scrub(str(fields[key]), mail))}"
        for key in _DIAGNOSTIC_KEYS
        if fields.get(key) not in (None, "", [], {})
    )


def _agent_runs(stage: dict) -> list[dict]:
    tasks = stage.get("tasks")
    return [
        run
        for task in (tasks if isinstance(tasks, list) else [])
        if isinstance(task, dict) and isinstance(task.get("agent_runs"), list)
        for run in task["agent_runs"]
        if isinstance(run, dict)
    ]


def _studio_output(text: str | None, mail: tuple[str, ...]) -> str:
    """What the child printed about its own failure, minus the mail printed alongside.

    `--json` prints the whole run record, the input echoed back inside it; on the homelab
    that tail lands in journald, which no policy, no `scope: full` and no keymap reaches
    (STU-1010). So a record is reported by what it says about itself — its status, its
    stages and their errors — and nothing else. Anything that is not a record (a
    half-written one from a killed studio, a stack trace, a usage line) still falls back
    to the tail, with the values we sent taken out of it: dropping it outright would put
    us back where STU-820 started, a stage-2 failure with no output at all.
    """
    record = _run_record(text)
    if record is None:
        return _tail(_scrub(text, mail))
    parts = [_diagnostics(record, mail)]
    stages = record.get("stages")
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict):
            continue
        parts.append(f"stage[{stage.get('stage_name')}] {_diagnostics(stage, mail)}".strip())
        # The engine records a failed attempt's cause on its agent run, never on the
        # stage: without this a spawn failure read `stage[classify] status=failed` and
        # nothing else (STU-1497).
        for run in _agent_runs(stage):
            label = f"agent[{run.get('agent_name')}#{run.get('attempt')}]"
            parts.append(f"{label} {_diagnostics(run, mail)}".strip())
    return " ".join(part for part in parts if part) or "run record carried no diagnostics"


def _scrubbed(stderr: str | None, stdout: str | None, mail: tuple[str, ...]) -> tuple[str, str]:
    """The two captured streams, ready to be logged and to be kept in the report."""
    return _tail(_scrub(stderr, mail)), _studio_output(stdout, mail)


def _scanned(stderr: str | None, stdout: str | None, mail: tuple[str, ...]) -> str:
    """Everything the child printed, mail taken out — where a notice is looked for.

    Not `_studio_output`: that keeps only what a run record says about *itself*, and the
    provider's answer is not on that whitelist — it rides in the record's
    `agent_output_raw`, next to the echoed input the whitelist exists to drop (STU-1010).
    Widening the whitelist to reach it would publish the mail alongside it, so the scan
    reads the whole scrubbed stream instead and reports only its own bounded match.
    Scrubbed and not raw, because mail quoting a limit notice would otherwise raise one.
    """
    return f"{_scrub(stdout, mail)}\n{_scrub(stderr, mail)}"


def _installed_version(cwd: Path | None = None) -> tuple[int, int, int] | None:
    try:
        proc = subprocess.run(
            ["studio", "--version"], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", proc.stdout)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def preflight_anonymize_scope(
    anonymize_fields: list[str] | None, *, cwd: Path | None = None
) -> str | None:
    """None when the run may proceed, else why the scope cannot be honoured here.

    Two ways it can't: the installed Studio ignores `anonymize_fields` (STU-834), or the
    `ner` extra is absent so no name would be detected (STU-908). A version that cannot
    be read is treated as unsupported: sending mail through a build that ignores the
    scope is the failure this guard exists to prevent.
    """
    if not anonymize_fields:
        return None
    no_detector = missing_detector()
    if no_detector:
        return no_detector
    minimum = ".".join(str(n) for n in _MIN_SCOPE_VERSION)
    version = _installed_version(cwd)
    if version is None:
        return (
            f"cannot read `studio --version`; anonymization.scope needs studio >= {minimum} "
            "to be honoured. Upgrade Studio, or clear anonymization.scope in the policy."
        )
    if version < _MIN_SCOPE_VERSION:
        return (
            f"installed studio {'.'.join(str(n) for n in version)} ignores anonymize_fields: "
            f"it would anonymize every field and leak the scope list into the prompt. "
            f"Upgrade to >= {minimum}, or clear anonymization.scope in the policy."
        )
    return None


def claude_code_inherits_host_config(*, cwd: Path | None = None) -> str | None:
    """None when a `claude-code` call cannot pick up the host's own config, else why.

    The provider drives the local CLI, which builds its prompt from the running
    account and from the cwd. Studio seals both from #271 on, so the warning this
    answers stops being true the moment a host upgrades — and a warning that lies
    about where a prompt came from is worse than no warning, being read as the reason
    a leak is or is not possible. A version that cannot be read counts as inheriting:
    an unreadable version has to fail toward the loud answer (STU-1118).
    """
    minimum = ".".join(str(n) for n in _MIN_SEALED_PROMPT_VERSION)
    version = _installed_version(cwd)
    if version is None:
        return f"`studio --version` is unreadable, so it may predate {minimum}"
    if version < _MIN_SEALED_PROMPT_VERSION:
        return f"studio {'.'.join(str(n) for n in version)} predates {minimum}"
    return None


@dataclass
class Stage2Result:
    classification: Classification | None = None
    error: str | None = None
    # Captured studio output for the error tag. Console logs are gone once a run
    # finishes, and studio writes no JSONL when the CLI dies before pipeline start —
    # without this, a cold post-mortem has nothing to read (STU-820). It carries what
    # the child said about the failure, never the mail it was handed (STU-1010).
    error_detail: str | None = None
    # What this call made and spent, as the fan-out item `cost.leg` prices (STU-1012,
    # STU-1244): the child's own per-stage record, attempts and usage together. None
    # when the child left no record — it still cost something, but only the child knew
    # how much and it did not say, so the leg counts it as one unrecorded item.
    spend: dict | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.classification is not None


class Stage2Classifier(Protocol):
    def classify(self, email: EmailInput, thread_history: str = "") -> Stage2Result: ...


def outgoing_payload(
    email: EmailInput, anonymize_fields: list[str] | None, thread_history: str
) -> dict:
    """The input file a stage-2 call is handed — and what a recall audit reads.

    Public because the run records it (STU-1036): a recall verdict is a claim about
    the text that reached the model, so it has to be made about this dict and not
    about a reconstruction of it.
    """
    payload = {
        "thread_id": email.thread_id,
        "from_address": email.from_address,
        "subject": email.subject,
        "headers": email.headers,
        "body_snippet": email.body_snippet,
        "date": email.date,
        "provider_category": email.provider_category,
        # The rest of the conversation this run fetched (STU-944), flat because the
        # engine's anonymization middleware scopes plain string fields only — a nested
        # shape would reach the LLM untokenized.
        "thread_history": thread_history,
    }
    if anonymize_fields is not None:
        # A turn's body rides in the same scopes as the head's: a scope tokenizes the
        # transcript, which is composed of the very fields that scope names.
        payload["anonymize_fields"] = [*anonymize_fields, "thread_history"]
    return payload


def _spend(run_payload: dict | None) -> dict | None:
    """The child run's stages in the shape `cost.leg` prices a fan-out item by.

    The subprocess path is the one orchestration Studio does not hand a
    `SpawnResult` to: `classify-batch`'s escalations are `map:` items, so their
    per-stage record — stage, status, RALPH `attempts`, `token_usage` — arrives
    built (STU-1064). Here the child is a `studio run classify` whose `--json` is
    the reader-shaped `PipelineRun`, so the record is derived from it instead
    (STU-1244). Without that, a leg fell to `leg`'s no-record floor of one call per
    escalation while the batch path counted attempts, and the two orchestrations
    reported different `llm_calls` for one corpus — the substitutability STU-1128
    is about, on a second axis.

    `attempts` is the kernel's own count, written on the stage since Studio 0.21.0
    (STU-1287, STU-1493); this used to re-derive it from the stage's `agent_runs`.

    None when the child left no stages to read, or a stage carries no `attempts` (a
    host below 0.21.0) — the record is what `leg` counts attempts from, and one built
    without them would price as zero calls rather than as the floor. `token_usage`
    is not carried beside it: the flat sum *is* the stages added up, and `leg` reads
    the record, so carrying both would price the same tokens twice.
    """
    stages = (run_payload or {}).get("stages")
    stages = [stage for stage in (stages if isinstance(stages, list) else [])
              if isinstance(stage, dict)]
    if not stages or any(not isinstance(stage.get("attempts"), int) for stage in stages):
        return None
    return {"stages": [
        {"stage": stage.get("stage_name"),
         "status": stage.get("status"),
         "attempts": stage["attempts"],
         **({"token_usage": stage["token_usage"]} if stage.get("token_usage") else {})}
        for stage in stages
    ]}


def _stage_output(run_payload: dict) -> dict | None:
    stages = run_payload.get("stages", [])
    if not isinstance(stages, list):
        return None
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        if stage.get("stage_name") != _STAGE_NAME or stage.get("status") != "success":
            continue
        output = stage.get("output")
        if isinstance(output, dict):
            return output
    return None


def _to_classification(output: dict) -> Classification:
    return Classification(
        thread_id=output["thread_id"],
        bucket=output["bucket"],
        subtype=output["subtype"],
        confidence=output["confidence"],
        suggested_action=output["suggested_action"],
        suggested_label=output.get("suggested_label"),
        reasoning=output["reasoning"],
    )


class StudioCliClassifier:
    def __init__(
        self,
        *,
        taxonomy: Taxonomy,
        provider: str = "anthropic",
        cwd: Path | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        anonymize_fields: list[str] | None = None,
        model: str | None = None,
        alert_on_provider_failure: bool = True,
    ) -> None:
        # This deployment's own bucket shape (STU-1461), required and never defaulted:
        # the client re-validates the agent's output in-process, defence in depth
        # against RALPH's own hook, and a hidden default taxonomy is exactly the
        # silent fallback that hook was built to refuse.
        self.taxonomy = taxonomy
        self.provider = provider
        self.cwd = cwd
        self.timeout = timeout
        # The model the classifier agent runs on, for a harness that scores two of them
        # against each other (STU-1045). None leaves the agent's own `${CLASSIFY_MODEL}`
        # unset, so a run reads `defaults.model` — production never names a model here.
        self.model = model
        # The alert says escalated mail is being left unclassified, which is true of a
        # run and false of a harness deliberately pointing an unvetted model at committed
        # fixtures (STU-1045). Nobody is paged for a model failing the test it is taking.
        self.alert_on_provider_failure = alert_on_provider_failure
        # Which input fields Studio's own middleware must tokenize before the LLM sees
        # them (STU-393). Studio deanonymizes its own output, so a classification comes
        # back carrying real values. Only the A/B harness's anonymized arm still asks
        # for this: a run tokenizes before it gets here (STU-1009), and setting it as
        # well would hand the middleware tokens to re-mint. None/empty = it does none.
        self.anonymize_fields = anonymize_fields or None
        # A spent limit answers every conversation of the run identically, and one alert
        # per unclassified email would bury the one fact behind it (STU-1016). Scoped to
        # the classifier and not to the process: a run builds its own, so a limit still
        # standing at the next run announces itself again — that run's silence is new.
        self._provider_alerted = False

    def _failure(
        self, tag: str, detail: str | None, *, scanned: str, thread_id: str,
        spend: dict | None = None,
    ) -> Stage2Result:
        """One stage-2 failure, named for the provider when that is what answered.

        A spent session limit is the same class of fault as a lost mailbox grant
        (STU-930): the provider is unavailable, no retry inside the run can help, and the
        mail behind it is dropped on purpose — so the only thing left is being told, while
        it is happening rather than in the next audit (STU-1016). It reaches us as an
        ordinary contract violation, because the notice *is* the agent's answer, which is
        why it is read off the output rather than off a tag.

        `spend` rides through both shapes, because that fault is exactly the one worth
        counting: the notice is the agent's answer, so RALPH retries it twice against
        the same wall and the child burns three attempts before the stage fails. A
        failure priced as one call is the half of STU-1016 the cost artifact would keep
        losing, and it is the half the caller cannot reconstruct.
        """
        notice = provider_unavailable_notice(scanned)
        if notice is None:
            return Stage2Result(error=tag, error_detail=detail, spend=spend)
        _log.error(
            "stage2 PROVIDER UNAVAILABLE thread=%s | %s (reported as %s)",
            thread_id, notice, tag,
        )
        if self.alert_on_provider_failure and not self._provider_alerted:
            self._provider_alerted = True
            alerts.notify(
                "email-classifier: stage 2 is not classifying — the provider answered "
                f"\u201c{notice}\u201d instead of a verdict. Escalated mail is being left "
                "unclassified until that clears; no retry inside a run can get past it."
            )
        return Stage2Result(error=PROVIDER_UNAVAILABLE, error_detail=notice, spend=spend)

    def classify(self, email: EmailInput, thread_history: str = "") -> Stage2Result:
        # Capture the temp-file name BEFORE writing, and clean up in a single outer
        # finally, so the file is always removed even if anything below raises. A
        # serialization failure is a correctness bug (EmailInput is fully typed), so we
        # let it propagate loudly rather than mask it as a fake Studio error.
        input_path: str | None = None
        payload = outgoing_payload(email, self.anonymize_fields, thread_history)
        # What the child is about to be handed is exactly what must not come back out of
        # it into a log line (STU-1010).
        mail = _mail_values(payload)
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".yaml", delete=False
            ) as tmp:
                input_path = tmp.name
                yaml.safe_dump(payload, tmp, sort_keys=False, allow_unicode=True)

            cmd = ["studio", "run", _PIPELINE, "--input-file", input_path, "--json"]
            if self.provider:
                cmd += ["--provider", self.provider]
            if self.anonymize_fields:
                cmd.append("--anonymize")

            _log.debug("classify thread=%s cmd=%s", email.thread_id, " ".join(cmd))
            started = time.monotonic()
            env = {**os.environ, _MODEL_ENV: self.model} if self.model else None
            try:
                result = subprocess.run(
                    cmd, cwd=self.cwd, capture_output=True, text=True, timeout=self.timeout,
                    env=env,
                )
            except FileNotFoundError:
                _log.error("studio CLI not found on PATH (cmd=%s)", " ".join(cmd))
                return Stage2Result(error="studio_cli_missing")
            except subprocess.TimeoutExpired as exc:
                # The hang case: surface whatever studio/claude emitted before the kill,
                # so a timeout isn't a black box.
                stderr, stdout = _scrubbed(exc.stderr, exc.stdout, mail)
                _log.warning(
                    "stage2 TIMEOUT thread=%s after %.0fs | stderr=%s | stdout=%s",
                    email.thread_id, self.timeout, stderr, stdout,
                )
                return Stage2Result(
                    error="studio_run_timeout",
                    error_detail=f"after {self.timeout}s | stderr={stderr} | stdout={stdout}",
                )

            elapsed = time.monotonic() - started
            if result.returncode != 0:
                stderr, stdout = _scrubbed(result.stderr, result.stdout, mail)
                _log.warning(
                    "stage2 FAILED thread=%s rc=%s in %.1fs | stderr=%s | stdout=%s",
                    email.thread_id, result.returncode, elapsed, stderr, stdout,
                )
                return self._failure(
                    "studio_run_failed",
                    f"rc={result.returncode} | stderr={stderr} | stdout={stdout}",
                    scanned=_scanned(result.stderr, result.stdout, mail),
                    thread_id=email.thread_id,
                    # A failed run still prints its record and *then* exits 1, so this
                    # branch usually has one to price. Only a throw around the engine
                    # run leaves stdout empty, and that is the floor below.
                    spend=_spend(_run_record(result.stdout)),
                )

            try:
                run_payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                stderr, stdout = _scrubbed(result.stderr, result.stdout, mail)
                _log.warning(
                    "stage2 UNPARSEABLE stdout thread=%s in %.1fs | stdout=%s | stderr=%s",
                    email.thread_id, elapsed, stdout, stderr,
                )
                return self._failure(
                    "studio_output_json_parse_error",
                    f"stdout={stdout} | stderr={stderr}",
                    scanned=_scanned(result.stderr, result.stdout, mail),
                    thread_id=email.thread_id,
                )

            output = _stage_output(run_payload)
            if output is None:
                _log.warning(
                    "stage2 NO SUCCESSFUL '%s' stage thread=%s run_status=%s in %.1fs",
                    _STAGE_NAME, email.thread_id, run_payload.get("status"), elapsed,
                )
                return self._failure(
                    "studio_run_output_missing",
                    None,
                    scanned=_scanned(result.stderr, result.stdout, mail),
                    thread_id=email.thread_id,
                    spend=_spend(run_payload),
                )

            # Defence in depth: the RALPH hook already validated, but never trust an LLM
            # output blindly — fail visibly rather than misroute. Not scanned for a
            # provider notice (STU-1016): a well-shaped object came back, so the provider
            # did answer, and the only free text left to scan is the model's own
            # reasoning about the mail — where a match would be a false positive.
            validation = validate_classification(output, self.taxonomy)
            if not validation.valid:
                _log.warning(
                    "stage2 OUTPUT re-failed validation thread=%s errors=%s",
                    email.thread_id, validation.errors,
                )
                return Stage2Result(error="studio_run_output_invalid",
                                    spend=_spend(run_payload))

            classification = _to_classification(output)
            _log.info(
                "stage2 ok thread=%s bucket=%s action=%s in %.1fs",
                email.thread_id, classification.bucket, classification.suggested_action, elapsed,
            )
            return Stage2Result(classification=classification,
                                spend=_spend(run_payload))
        finally:
            if input_path is not None:
                Path(input_path).unlink(missing_ok=True)
