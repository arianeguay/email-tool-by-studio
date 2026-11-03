# End-to-End Orchestrator + Classification Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only end-to-end orchestrator (`run_email.py`) that loads emails (frozen dump or live Gmail fetch), runs Stage 1 (deterministic rules), escalates only grey-zone emails to Stage 2 (Studio `classify` pipeline), and emits a classification report (human stdout + JSON file).

**Architecture:** Stage 1 runs in-process (pure Python). Stage 2 lives behind a `Stage2Classifier` Protocol; the real impl shells out to `studio run classify --json` per escalated email (mirroring wiki-creator's `relationship_extraction.py`), and a `FakeClassifier` is injected for tests. Orchestration logic sits in an importable `email_classifier/orchestrate/` package; `run_email.py` is a thin CLI wrapper. A `Makefile` exposes the entry points.

**Tech Stack:** Python 3, pytest, PyYAML, `subprocess` (to invoke the Studio CLI). No Anthropic SDK in Python — Studio owns the LLM.

**Spec:** [docs/superpowers/specs/2026-06-15-orchestrator-classification-report-design.md](../specs/2026-06-15-orchestrator-classification-report-design.md)

**Conventions to know (verified):**
- Tests run from the repo root (`python -m pytest` or `pytest`); the repo root is on `sys.path`, so `from tests.conftest import RULES_DIR` and `import run_email` both work. `tests/` has no `__init__.py`.
- Existing pure pieces this plan reuses, with exact signatures:
  - `email_classifier.stage1.rule_engine.load_rules(path: Path) -> list[Rule]`, `apply(email: EmailInput, rules: list[Rule]) -> RuleMatch`
  - `email_classifier.stage1.policy.load_policy(path: Path) -> Policy`, `filter_action(match: RuleMatch, policy: Policy) -> RuleMatch`
  - `email_classifier.stage1.frontier_log.FrontierLogger(log_path: Path, stdout: bool=False)` with attrs `.run_id` and `.log(email, match)`; `make_logger(policy, log_path) -> FrontierLogger`
  - `email_classifier.core.schema.load_many(path: Path) -> list[EmailInput]`, `validate(data: dict) -> EmailInput`
  - `email_classifier.core.contract.validate_classification(data: dict) -> ValidationResult` (has `.valid`, `.errors`)
  - `email_classifier.core.types`: `EmailInput`, `RuleMatch`, `Classification` (7 fields: `thread_id, bucket, subtype, confidence, suggested_action, suggested_label, reasoning`)
- The catch-all rule emits `action="escalate_to_llm"`, `rule_id="catch_all"`. The Studio stage name is `classify`.

---

## File Structure

| File | Responsibility |
|---|---|
| `email_classifier/orchestrate/__init__.py` | Package marker (empty). |
| `email_classifier/orchestrate/report.py` | `EmailVerdict` / `Report` dataclasses + aggregation + JSON dict + human render. Pure. |
| `email_classifier/orchestrate/stage2_client.py` | `Stage2Result`, `Stage2Classifier` Protocol, `StudioCliClassifier` (subprocess + `--json` parsing + error taxonomy). |
| `email_classifier/orchestrate/pipeline.py` | `run(...)` — the core loop + the frontier invariant; returns a `Report`. |
| `run_email.py` | Thin CLI: load dump/fetch, preflight checks, build deps, call `run`, write+print report. |
| `Makefile` | `.PHONY` entry points (`run`, `run-fetch`, `fetch`, `test`, `clean`). |
| `.gitignore` | Add `reports/`. |
| `tests/test_report.py` | Aggregation math, JSON shape, human format. |
| `tests/test_stage2_client.py` | stdout/JSONL parsing + full error taxonomy (subprocess mocked). |
| `tests/test_orchestrate_pipeline.py` | Frontier invariant + aggregation with `FakeClassifier`. |
| `tests/test_run_email.py` | Arg parsing (mutual exclusion) + dump loading. |

Build order: report → stage2_client → pipeline → run_email → Makefile (each depends only on earlier ones plus existing pure modules).

---

## Task 1: Report model (`report.py`)

**Files:**
- Create: `email_classifier/orchestrate/__init__.py`
- Create: `email_classifier/orchestrate/report.py`
- Test: `tests/test_report.py`

- [ ] **Step 1: Create the package marker**

Create `email_classifier/orchestrate/__init__.py` as an empty file:

```python
```

- [ ] **Step 2: Write the failing tests**

Create `tests/test_report.py`:

```python
import json

from email_classifier.orchestrate.report import (
    EmailVerdict,
    build_report,
    render_human,
    report_to_dict,
)


def _det(thread_id, bucket="A", action="keep_visible"):
    return EmailVerdict(
        thread_id=thread_id, path="deterministic", status="ok", bucket=bucket,
        suggested_action=action, confidence="high", rule_id="some_rule",
        subtype=None, reasoning=None, error=None,
    )


def _llm(thread_id, bucket="C", action="label_archive"):
    return EmailVerdict(
        thread_id=thread_id, path="llm", status="ok", bucket=bucket,
        suggested_action=action, confidence="medium", rule_id="catch_all",
        subtype="newsletter", reasoning="looks promotional", error=None,
    )


def _llm_err(thread_id, error="studio_run_timeout"):
    return EmailVerdict(
        thread_id=thread_id, path="llm", status="error", bucket=None,
        suggested_action=None, confidence=None, rule_id="catch_all",
        subtype=None, reasoning=None, error=error,
    )


def test_totals_and_ratio():
    report = build_report("run1", [_det("a"), _det("b"), _det("c"), _llm("d")])
    assert report.totals == {"emails": 4, "deterministic": 3, "llm": 1, "errors": 0}
    assert report.ratio_deterministic == 0.75


def test_by_bucket_and_action_count_ok_verdicts_only():
    verdicts = [
        _det("a", bucket="A", action="keep_visible"),
        _det("b", bucket="A", action="keep_visible"),
        _llm("c", bucket="C", action="label_archive"),
        _llm_err("d"),
    ]
    report = build_report("run1", verdicts)
    assert report.by_bucket == {"A": 2, "C": 1}
    assert report.by_action == {"keep_visible": 2, "label_archive": 1}
    assert report.totals["errors"] == 1


def test_empty_ratio_is_zero():
    report = build_report("run1", [])
    assert report.ratio_deterministic == 0.0
    assert report.totals == {"emails": 0, "deterministic": 0, "llm": 0, "errors": 0}


def test_report_to_dict_is_json_serializable():
    report = build_report("run1", [_det("a"), _llm("b")])
    payload = report_to_dict(report)
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["run_id"] == "run1"
    assert len(payload["verdicts"]) == 2
    assert payload["verdicts"][0]["path"] == "deterministic"


def test_render_human_shows_ratio_and_escalated_lines():
    report = build_report("abc", [_det("a"), _det("b"), _llm("c"), _llm_err("d")])
    text = render_human(report)
    assert "run abc" in text
    assert "(50%)" in text                 # 2 of 4 deterministic
    assert "c   catch_all -> LLM -> C / label_archive" in text
    assert "ERROR (studio_run_timeout)" in text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_report.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'email_classifier.orchestrate.report'`

- [ ] **Step 4: Write the implementation**

Create `email_classifier/orchestrate/report.py`:

```python
"""Classification report (STU-392): aggregate both paths into a report.

Pure module — no I/O, no LLM. `build_report` does the aggregation; `report_to_dict`
renders the JSON-serializable shape written to disk; `render_human` renders the stdout
summary. The deterministic/LLM ratio is the economy argument — surface it prominently.
"""

from dataclasses import asdict, dataclass


@dataclass
class EmailVerdict:
    thread_id: str
    path: str               # "deterministic" | "llm"
    status: str             # "ok" | "error"
    bucket: str | None
    suggested_action: str | None
    confidence: str | None
    rule_id: str | None     # deterministic path, and catch_all for escalated ones
    subtype: str | None     # llm path only
    reasoning: str | None   # llm path only
    error: str | None       # error-taxonomy tag when status == "error"


@dataclass
class Report:
    run_id: str
    totals: dict
    ratio_deterministic: float
    by_bucket: dict
    by_action: dict
    verdicts: list


def build_report(run_id: str, verdicts: list[EmailVerdict]) -> Report:
    total = len(verdicts)
    deterministic = sum(1 for v in verdicts if v.path == "deterministic")
    llm = sum(1 for v in verdicts if v.path == "llm")
    errors = sum(1 for v in verdicts if v.status == "error")

    by_bucket: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for v in verdicts:
        if v.status != "ok":
            continue
        if v.bucket is not None:
            by_bucket[v.bucket] = by_bucket.get(v.bucket, 0) + 1
        if v.suggested_action is not None:
            by_action[v.suggested_action] = by_action.get(v.suggested_action, 0) + 1

    return Report(
        run_id=run_id,
        totals={"emails": total, "deterministic": deterministic, "llm": llm, "errors": errors},
        ratio_deterministic=(deterministic / total if total else 0.0),
        by_bucket=by_bucket,
        by_action=by_action,
        verdicts=verdicts,
    )


def report_to_dict(report: Report) -> dict:
    return {
        "run_id": report.run_id,
        "totals": report.totals,
        "ratio_deterministic": report.ratio_deterministic,
        "by_bucket": report.by_bucket,
        "by_action": report.by_action,
        "verdicts": [asdict(v) for v in report.verdicts],
    }


def render_human(report: Report) -> str:
    t = report.totals
    det_pct = round(report.ratio_deterministic * 100)
    llm_pct = round(t["llm"] / t["emails"] * 100) if t["emails"] else 0
    lines = [
        f"=== Classification Report (run {report.run_id}) ===",
        f"{t['emails']} emails | {t['deterministic']} deterministic ({det_pct}%) | "
        f"{t['llm']} -> LLM ({llm_pct}%) | {t['errors']} errors",
        "By bucket:  " + "  ".join(f"{b}:{n}" for b, n in sorted(report.by_bucket.items())),
        "By action:  " + "  ".join(f"{a}:{n}" for a, n in sorted(report.by_action.items())),
        "Escalated (frontier):",
    ]
    escalated = [v for v in report.verdicts if v.path == "llm"]
    if not escalated:
        lines.append("  (none)")
    for v in escalated:
        if v.status == "ok":
            lines.append(f"  {v.thread_id}   catch_all -> LLM -> {v.bucket} / {v.suggested_action}")
        else:
            lines.append(f"  {v.thread_id}   catch_all -> LLM -> ERROR ({v.error})")
    return "\n".join(lines)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_report.py -v`
Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add email_classifier/orchestrate/__init__.py email_classifier/orchestrate/report.py tests/test_report.py
git commit -m "feat: classification report model + aggregation (STU-392)"
```

---

## Task 2: Stage 2 client (`stage2_client.py`)

**Files:**
- Create: `email_classifier/orchestrate/stage2_client.py`
- Test: `tests/test_stage2_client.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stage2_client.py`:

```python
import json
import subprocess

from email_classifier.core.types import EmailInput
from email_classifier.orchestrate.stage2_client import StudioCliClassifier


def _email(thread_id="t1"):
    return EmailInput(
        thread_id=thread_id, from_address="x@y.com", subject="S",
        headers={}, body_snippet="b", date="2026-06-10T00:00:00+00:00",
    )


def _valid_output(thread_id="t1"):
    return {
        "thread_id": thread_id, "bucket": "C", "subtype": "newsletter",
        "confidence": "medium", "suggested_action": "label_archive",
        "suggested_label": "Archive/News", "reasoning": "promotional",
    }


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(monkeypatch, completed):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: completed)


def test_success_returns_classification(monkeypatch):
    payload = {
        "id": "r1", "status": "success",
        "stages": [{"stage_name": "classify", "status": "success", "output": _valid_output()}],
    }
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier().classify(_email())
    assert result.ok
    assert result.classification.bucket == "C"
    assert result.classification.suggested_action == "label_archive"


def test_nonzero_exit_is_studio_run_failed(monkeypatch):
    _patch_run(monkeypatch, _FakeCompleted(1, "", "boom"))
    result = StudioCliClassifier().classify(_email())
    assert not result.ok
    assert result.error == "studio_run_failed"


def test_unparseable_stdout_is_json_parse_error(monkeypatch):
    _patch_run(monkeypatch, _FakeCompleted(0, "not json at all"))
    result = StudioCliClassifier().classify(_email())
    assert result.error == "studio_output_json_parse_error"


def test_no_successful_stage_is_output_missing(monkeypatch):
    payload = {"id": "r1", "stages": [{"stage_name": "classify", "status": "failed"}]}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier().classify(_email())
    assert result.error == "studio_run_output_missing"


def test_output_that_refails_validation_is_output_invalid(monkeypatch):
    bad = _valid_output()
    bad["suggested_action"] = "keep_visible"  # not allowed for bucket C (INV-08)
    payload = {"id": "r1", "stages": [{"stage_name": "classify", "status": "success", "output": bad}]}
    _patch_run(monkeypatch, _FakeCompleted(0, json.dumps(payload)))
    result = StudioCliClassifier().classify(_email())
    assert result.error == "studio_run_output_invalid"


def test_missing_cli_is_studio_cli_missing(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError()
    monkeypatch.setattr(subprocess, "run", _raise)
    result = StudioCliClassifier().classify(_email())
    assert result.error == "studio_cli_missing"


def test_timeout_is_studio_run_timeout(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="studio", timeout=1)
    monkeypatch.setattr(subprocess, "run", _raise)
    result = StudioCliClassifier().classify(_email())
    assert result.error == "studio_run_timeout"


def test_provider_flag_is_passed_to_command(monkeypatch):
    captured = {}

    def _capture(cmd, *a, **k):
        captured["cmd"] = cmd
        payload = {"id": "r1", "stages": [
            {"stage_name": "classify", "status": "success", "output": _valid_output()}]}
        return _FakeCompleted(0, json.dumps(payload))

    monkeypatch.setattr(subprocess, "run", _capture)
    StudioCliClassifier(provider="mock").classify(_email())
    assert "--provider" in captured["cmd"]
    assert "mock" in captured["cmd"]
    assert captured["cmd"][:3] == ["studio", "run", "classify"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_stage2_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'email_classifier.orchestrate.stage2_client'`

- [ ] **Step 3: Write the implementation**

Create `email_classifier/orchestrate/stage2_client.py`:

```python
"""Stage 2 boundary (STU-392): classify one escalated email via the Studio CLI.

The orchestrator depends only on the Stage2Classifier Protocol. The real impl shells
out to `studio run classify --input-file <tmp> --json` per email and parses the result,
mirroring wiki-creator's relationship_extraction.py. Errors are returned as data (a tag
on Stage2Result), never raised — the orchestrator records them per-email and continues.
No Anthropic SDK here: Studio makes the LLM call.
"""

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from email_classifier.core.contract import validate_classification
from email_classifier.core.types import Classification, EmailInput

_PIPELINE = "classify"
_STAGE_NAME = "classify"
_DEFAULT_TIMEOUT = 180


@dataclass
class Stage2Result:
    classification: Classification | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.classification is not None


class Stage2Classifier(Protocol):
    def classify(self, email: EmailInput) -> Stage2Result: ...


def _email_to_input(email: EmailInput) -> dict:
    return {
        "thread_id": email.thread_id,
        "from_address": email.from_address,
        "subject": email.subject,
        "headers": email.headers,
        "body_snippet": email.body_snippet,
        "date": email.date,
        "provider_category": email.provider_category,
    }


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
        provider: str = "anthropic",
        cwd: Path | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self.provider = provider
        self.cwd = cwd
        self.timeout = timeout

    def classify(self, email: EmailInput) -> Stage2Result:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".yaml", delete=False
        ) as tmp:
            yaml.safe_dump(_email_to_input(email), tmp, sort_keys=False, allow_unicode=True)
            input_path = tmp.name

        cmd = ["studio", "run", _PIPELINE, "--input-file", input_path, "--json"]
        if self.provider:
            cmd += ["--provider", self.provider]

        try:
            result = subprocess.run(
                cmd, cwd=self.cwd, capture_output=True, text=True, timeout=self.timeout
            )
        except FileNotFoundError:
            return Stage2Result(error="studio_cli_missing")
        except subprocess.TimeoutExpired:
            return Stage2Result(error="studio_run_timeout")
        finally:
            Path(input_path).unlink(missing_ok=True)

        if result.returncode != 0:
            return Stage2Result(error="studio_run_failed")

        try:
            run_payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return Stage2Result(error="studio_output_json_parse_error")

        output = _stage_output(run_payload)
        if output is None:
            return Stage2Result(error="studio_run_output_missing")

        # Defence in depth: the RALPH hook already validated, but never trust an LLM
        # output blindly — fail visibly rather than misroute.
        if not validate_classification(output).valid:
            return Stage2Result(error="studio_run_output_invalid")

        return Stage2Result(classification=_to_classification(output))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_stage2_client.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add email_classifier/orchestrate/stage2_client.py tests/test_stage2_client.py
git commit -m "feat: Stage2 Studio CLI client with error taxonomy (STU-392)"
```

---

## Task 3: Orchestrator pipeline (`pipeline.py`)

**Files:**
- Create: `email_classifier/orchestrate/pipeline.py`
- Test: `tests/test_orchestrate_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_orchestrate_pipeline.py`:

```python
import json

from email_classifier.core.types import Classification, EmailInput
from email_classifier.orchestrate.pipeline import run
from email_classifier.orchestrate.stage2_client import Stage2Result
from email_classifier.stage1.frontier_log import FrontierLogger
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import load_rules
from tests.conftest import RULES_DIR


class FakeClassifier:
    """Records the thread_ids it is asked to classify (to assert the frontier
    invariant) and returns canned results."""

    def __init__(self, results=None):
        self.results = results or {}
        self.seen = []

    def classify(self, email):
        self.seen.append(email.thread_id)
        return self.results.get(
            email.thread_id,
            Stage2Result(classification=Classification(
                thread_id=email.thread_id, bucket="C", subtype="newsletter",
                confidence="medium", suggested_action="label_archive",
                suggested_label=None, reasoning="default fake",
            )),
        )


def _email(thread_id, from_address):
    return EmailInput(
        thread_id=thread_id, from_address=from_address, subject="S",
        headers={}, body_snippet="b", date="2026-06-10T00:00:00+00:00",
    )


def _load():
    return load_rules(RULES_DIR / "rules.yaml"), load_policy(RULES_DIR / "policy.yaml")


def test_only_escalated_emails_reach_the_llm(tmp_path):
    rules, policy = _load()
    emails = [
        _email("det1", "noreply@payments.interac.ca"),   # interac_payment -> keep_visible
        _email("grey1", "stranger@unknown-domain.test"),  # catch_all -> escalate_to_llm
    ]
    fake = FakeClassifier()
    report = run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert fake.seen == ["grey1"]  # Stage-1-decided email NEVER classified by LLM
    det = next(v for v in report.verdicts if v.thread_id == "det1")
    assert det.path == "deterministic"
    assert det.suggested_action == "keep_visible"
    grey = next(v for v in report.verdicts if v.thread_id == "grey1")
    assert grey.path == "llm"


def test_frontier_log_records_escalation_reason(tmp_path):
    rules, policy = _load()
    log_path = tmp_path / "f.jsonl"
    run([_email("grey1", "stranger@unknown-domain.test")], rules, policy,
        FakeClassifier(), FrontierLogger(log_path=log_path))
    entry = json.loads(log_path.read_text().strip().splitlines()[0])
    assert entry["action"] == "escalate_to_llm"
    assert entry["rule_id"] == "catch_all"


def test_stage2_error_is_recorded_and_run_continues(tmp_path):
    rules, policy = _load()
    emails = [
        _email("grey1", "a@unknown1.test"),
        _email("grey2", "b@unknown2.test"),
    ]
    fake = FakeClassifier({"grey1": Stage2Result(error="studio_run_timeout")})
    report = run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert report.totals["errors"] == 1
    g1 = next(v for v in report.verdicts if v.thread_id == "grey1")
    assert g1.status == "error" and g1.error == "studio_run_timeout"
    g2 = next(v for v in report.verdicts if v.thread_id == "grey2")
    assert g2.status == "ok"


def test_report_run_id_matches_logger(tmp_path):
    rules, policy = _load()
    logger = FrontierLogger(log_path=tmp_path / "f.jsonl")
    report = run([_email("det1", "noreply@payments.interac.ca")], rules, policy,
                 FakeClassifier(), logger)
    assert report.run_id == logger.run_id
    assert report.totals == {"emails": 1, "deterministic": 1, "llm": 0, "errors": 0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_orchestrate_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'email_classifier.orchestrate.pipeline'`

- [ ] **Step 3: Write the implementation**

Create `email_classifier/orchestrate/pipeline.py`:

```python
"""End-to-end orchestrator core (STU-392).

The single chokepoint `if match.action == "escalate_to_llm"` guarantees two acceptance
criteria at once: Stage-1-decided emails never reach the LLM, and only escalated ones do.
Read-only: this module makes no mailbox mutations and has no code path that could.
"""

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.orchestrate.report import EmailVerdict, Report, build_report
from email_classifier.orchestrate.stage2_client import Stage2Classifier, Stage2Result
from email_classifier.stage1.frontier_log import FrontierLogger
from email_classifier.stage1.policy import Policy, filter_action
from email_classifier.stage1.rule_engine import Rule, apply

_ESCALATE = "escalate_to_llm"


def _deterministic_verdict(email: EmailInput, match: RuleMatch) -> EmailVerdict:
    return EmailVerdict(
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
    )


def _llm_verdict(email: EmailInput, match: RuleMatch, result: Stage2Result) -> EmailVerdict:
    if result.ok:
        c = result.classification
        return EmailVerdict(
            thread_id=email.thread_id,
            path="llm",
            status="ok",
            bucket=c.bucket,
            suggested_action=c.suggested_action,
            confidence=c.confidence,
            rule_id=match.rule_id,
            subtype=c.subtype,
            reasoning=c.reasoning,
            error=None,
        )
    return EmailVerdict(
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
    )


def run(
    emails: list[EmailInput],
    rules: list[Rule],
    policy: Policy,
    stage2: Stage2Classifier,
    logger: FrontierLogger,
) -> Report:
    verdicts: list[EmailVerdict] = []
    for email in emails:
        match = filter_action(apply(email, rules), policy)
        logger.log(email, match)
        if match.action == _ESCALATE:
            verdicts.append(_llm_verdict(email, match, stage2.classify(email)))
        else:
            verdicts.append(_deterministic_verdict(email, match))
    return build_report(logger.run_id, verdicts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_orchestrate_pipeline.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add email_classifier/orchestrate/pipeline.py tests/test_orchestrate_pipeline.py
git commit -m "feat: orchestrator pipeline with frontier invariant (STU-392)"
```

---

## Task 4: CLI entry point (`run_email.py`)

**Files:**
- Create: `run_email.py`
- Modify: `.gitignore` (add `reports/`)
- Test: `tests/test_run_email.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_run_email.py`:

```python
import json

import pytest

import run_email


def test_parse_args_requires_a_source():
    with pytest.raises(SystemExit):
        run_email.parse_args([])


def test_dump_and_fetch_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        run_email.parse_args(["--dump", "x.json", "--fetch"])


def test_load_emails_from_dump(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text(
        json.dumps([{
            "thread_id": "t1", "from_address": "a@b.com", "subject": "s",
            "headers": {}, "body_snippet": "x", "date": "2026-06-10T00:00:00+00:00",
        }]),
        encoding="utf-8",
    )
    args = run_email.parse_args(["--dump", str(dump)])
    emails = run_email._load_emails(args)
    assert len(emails) == 1
    assert emails[0].thread_id == "t1"


def test_load_emails_missing_dump_exits(tmp_path):
    args = run_email.parse_args(["--dump", str(tmp_path / "nope.json")])
    with pytest.raises(SystemExit):
        run_email._load_emails(args)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_run_email.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'run_email'`

- [ ] **Step 3: Write the implementation**

Create `run_email.py`:

```python
#!/usr/bin/env python3
"""End-to-end orchestrator (STU-392): dump|fetch -> stage1 -> stage2 -> report.

Read-only of the mailbox from end to end: no apply_label, no trash, no mutation path.
Config/setup errors fail loud BEFORE processing (missing `studio` CLI, malformed rules/
policy, bad dump). Per-email Stage 2 errors are recorded in the report and the run
continues. Exit 0 if every email classified; exit 2 if any per-email error occurred.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from email_classifier.core import schema
from email_classifier.orchestrate.pipeline import run
from email_classifier.orchestrate.report import render_human, report_to_dict
from email_classifier.orchestrate.stage2_client import StudioCliClassifier
from email_classifier.stage1.frontier_log import make_logger
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import load_rules

REPO_ROOT = Path(__file__).parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--dump", type=Path, help="Frozen EmailInput JSON array dump.")
    source.add_argument("--fetch", action="store_true", help="Live read-only Gmail fetch.")
    parser.add_argument("--query", default="", help="Gmail search query (with --fetch).")
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--provider", default="anthropic", help="Studio provider (anthropic|mock).")
    parser.add_argument("--report-dir", type=Path, default=REPO_ROOT / "reports")
    parser.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    parser.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")
    parser.add_argument("--log", type=Path, default=REPO_ROOT / "logs" / "frontier.jsonl")
    return parser.parse_args(argv)


def _load_emails(args: argparse.Namespace) -> list:
    if args.dump:
        if not args.dump.exists():
            print(f"[ERROR] dump not found: {args.dump}", file=sys.stderr)
            raise SystemExit(1)
        return schema.load_many(args.dump)
    from email_classifier.fetch import gmail

    creds = gmail.get_credentials()
    service = gmail.build_service(creds)
    raw = gmail.fetch_inputs(service, query=args.query, max_results=args.max_results)
    return [schema.validate(item) for item in raw]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Config/setup: fail loud BEFORE any processing.
    if shutil.which("studio") is None:
        print("[ERROR] `studio` CLI not found on PATH. Install Studio first.", file=sys.stderr)
        return 1
    rules = load_rules(args.rules)    # raises loudly on missing/malformed rules
    policy = load_policy(args.policy)  # raises loudly on missing/malformed policy
    emails = _load_emails(args)        # raises loudly on bad dump / schema violation

    logger = make_logger(policy, args.log)
    stage2 = StudioCliClassifier(provider=args.provider, cwd=REPO_ROOT)
    report = run(emails, rules, policy, stage2, logger)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"{report.run_id}.json"
    report_path.write_text(
        json.dumps(report_to_dict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(render_human(report))
    print(f"\nReport written to {report_path}", file=sys.stderr)

    return 2 if report.totals["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add `reports/` to `.gitignore`**

Append `reports/` to `.gitignore` (after the existing `logs/` line). The file should now contain a `reports/` entry alongside `logs/`, `dumps/`, and `secrets/`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_run_email.py -v`
Expected: PASS (4 passed)

- [ ] **Step 6: Commit**

```bash
git add run_email.py tests/test_run_email.py .gitignore
git commit -m "feat: run_email.py CLI orchestrator entry point (STU-392)"
```

---

## Task 5: Makefile entry points

**Files:**
- Create: `Makefile`

- [ ] **Step 1: Create the Makefile**

Create `Makefile`:

```makefile
.PHONY: run run-fetch fetch test clean

DUMP     ?= dumps/gmail-dump.json
QUERY    ?=
MAX      ?= 100
PROVIDER ?= anthropic

run:                 ## End-to-end run on a frozen dump
	python run_email.py --dump $(DUMP) --provider $(PROVIDER)

run-fetch:           ## Read-only fetch then end-to-end run
	python run_email.py --fetch --query "$(QUERY)" --max-results $(MAX) --provider $(PROVIDER)

fetch:               ## Fetch only -> write the JSON dump (STU-391)
	python scripts/fetch_gmail.py --query "$(QUERY)" --max-results $(MAX) --output $(DUMP)

test:                ## Run the test suite
	pytest

clean:               ## Remove run artifacts
	rm -rf reports/ logs/
```

- [ ] **Step 2: Verify the Makefile targets resolve**

Run: `make -n run run-fetch fetch test clean`
Expected: prints the five commands (dry-run) with no "No rule to make target" errors. Example first line: `python run_email.py --dump dumps/gmail-dump.json --provider anthropic`

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "feat: Makefile entry points for orchestrator (STU-392)"
```

---

## Task 6: Full suite green + manual eval runbook

**Files:**
- Create: `docs/superpowers/runbooks/2026-06-15-orchestrator-manual-eval.md`

- [ ] **Step 1: Run the entire test suite**

Run: `python -m pytest -q`
Expected: PASS — the previous 98 tests plus the new ones (report 5, stage2_client 8, pipeline 4, run_email 4 = 21 new), 119 total. If any pre-existing test broke, fix the regression before continuing.

- [ ] **Step 2: Write the manual eval runbook**

Create `docs/superpowers/runbooks/2026-06-15-orchestrator-manual-eval.md`:

```markdown
# Manual eval — end-to-end orchestrator (STU-392)

Covers acceptance criteria that need a real Studio + LLM (and optionally real Gmail).
Requires `ANTHROPIC_API_KEY`, the `studio` CLI on PATH, and the Studio `validators` hook
installed (see STU-390 spec §6).

## 1. Full run on a frozen dump (no Gmail)
1. Build a small dump from the fixtures (or reuse a previous `dumps/gmail-dump.json`).
   Each entry must be a valid `EmailInput`.
2. Run:
   ```bash
   make run DUMP=dumps/gmail-dump.json
   # or: python run_email.py --dump dumps/gmail-dump.json
   ```
3. Confirm: a human report prints, `reports/<run-id>.json` is written, and the
   deterministic/LLM ratio is shown. The run exits 0 (or 2 if any per-email LLM error).

## 2. Frontier invariant on a real run
1. Open `logs/frontier.jsonl` for the run (run-id matches the report).
2. Confirm every deterministically-decided email has its rule_id and is NOT in the LLM
   path of the report; only `action == "escalate_to_llm"` / `rule_id == "catch_all"`
   entries correspond to `path == "llm"` verdicts.

## 3. Real read-only fetch end-to-end (personal inbox)
1. Ensure `secrets/credentials.json` exists (see STU-391). First run opens a browser once.
2. Run:
   ```bash
   make run-fetch QUERY="newer_than:7d" MAX=50
   ```
3. Audit `reports/<run-id>.json` manually: spot-check bucket/action assignments against
   the taxonomy. Confirm ZERO mailbox changes occurred (read-only).

## 4. Economy argument
1. On a representative inbox slice, confirm `totals.deterministic` >> `totals.llm`
   (the majority resolved without a token). Record the ratio.
```

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/runbooks/2026-06-15-orchestrator-manual-eval.md
git commit -m "docs: manual eval runbook for end-to-end orchestrator (STU-392)"
```

---

## Acceptance Criteria Coverage

| STU-392 criterion | Where satisfied |
|---|---|
| Full run on frozen dump → report, no error | Task 3 `test_report_run_id_matches_logger` + Task 4 dump load; manual Task 6 §1 |
| Stage-1-decided emails never reach the LLM (frontier log) | Task 3 `test_only_escalated_emails_reach_the_llm` + `test_frontier_log_records_escalation_reason` |
| Only `escalate_to_llm` reach Stage 2 | Task 3 `test_only_escalated_emails_reach_the_llm` (`fake.seen == ["grey1"]`) |
| Report aggregates both paths + deterministic/LLM ratio | Task 1 `test_totals_and_ratio`, `test_by_bucket_and_action_count_ok_verdicts_only` |
| Zero mailbox actions (read-only end to end) | Structural — no mutation code path exists (pipeline.py docstring); manual Task 6 §3 |
| Works on frozen dump AND real fetch | Task 4 `_load_emails` (both branches); dump via pytest, fetch via runbook Task 6 §3 |
| Real run on personal inbox → auditable report | Runbook Task 6 §3 |
| `pytest` passes with LLM mocked | All Task 1–4 tests use `FakeClassifier` / mocked `subprocess`; Task 6 §1 full suite |

## Out of Scope (do not implement)
- Any mailbox mutation (apply_label / trash). PII anonymization (STU-393). Batching Stage 2.
  `deterministic_can_finalize: false` behavior (v1 assumes `true`).
```
