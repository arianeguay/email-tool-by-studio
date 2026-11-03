# Classification Contract + Studio Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the frozen email-classification contract, a pure-Python binary validator (INV-08 action↔bucket table + enums + presence/types), and the `.studio/` Studio pipeline that drives the LLM and enforces the contract via RALPH using the Studio `validators` hook.

**Architecture:** One source of truth in `core/contract.py` (`ALLOWED_ACTIONS` table + enums + `validate_classification`). Two callers: pytest calls the validator directly on mocked JSON; the Studio engine calls the *same* validator through a CLI (`stage2/validate_cli.py`) wired to the contract's `validators` hook. The hook receives the **real stage output** as JSON on stdin and returns `{valid, errors}` — invalid output drives a RALPH retry with those errors as feedback. The agent classifies grey-zone emails from provider-neutral signals only.

**Tech Stack:** Python 3.11+, `dataclasses`, `json`, `sys.stdin`, `pytest` (existing). No new Python deps (the Studio engine, already installed, makes LLM calls). Studio v7 configs in `.studio/`.

**Branch:** `arianedguay/stu-390-contrat-de-classification-pipeline-studio-etage-2-llm-etage` (created off clean `main`).

**Upstream dependency:** the Studio `validators` hook (Studio branch `arianedguay/studio-external-validator-hook`, commit `fc6dcf2`) must be merged/installed before the Task 5 manual eval. The pytest tasks (1–3) do **not** depend on it.

**Reference:** `docs/superpowers/specs/2026-06-15-classification-contract-studio-pipeline-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `email_classifier/core/types.py` (modify) | Add `Classification` dataclass (7 frozen fields) |
| `email_classifier/core/contract.py` (create) | `BUCKETS`/`CONFIDENCES`/`ACTIONS`, `ALLOWED_ACTIONS`, `ValidationResult`, `enum_and_consistency_errors`, `validate_classification` |
| `email_classifier/stage2/validate_cli.py` (create) | Read classification JSON on stdin → `validate_classification` → print `{valid, errors}` → exit non-zero if invalid |
| `tests/test_contract.py` (create) | Unit tests for `validate_classification` on mocked JSON (forbidden pairs, enums, presence, types) |
| `tests/test_validate_cli.py` (create) | CLI stdin→exit code + JSON output match the validator |
| `.studio/config.yaml` (create) | Provider (anthropic) + default model |
| `.studio/invariants.md` (create) | INV-08, no real delete, provider neutrality — auto-injected |
| `.studio/skills/taxonomy.skill.md` (create) | A/B/C/D taxonomy + provider-neutrality rule |
| `.studio/agents/classifier.agent.yaml` (create) | Classifier agent (emits the contract JSON) |
| `.studio/contracts/classification.contract.yaml` (create) | `required_fields` + `validators: [classification]` |
| `.studio/pipelines/classify.pipeline.yaml` (create) | One stage: agent + contract + RALPH (max 3) |
| `.gitignore` (modify) | Ignore `.studio/runs/` |
| `docs/superpowers/runbooks/2026-06-15-classify-manual-eval.md` (create) | Manual `studio run` eval procedure |

---

## Task 1: `Classification` dataclass + contract constants

**Files:**
- Modify: `email_classifier/core/types.py`
- Create: `email_classifier/core/contract.py`
- Test: `tests/test_contract.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_contract.py`:

```python
from email_classifier.core.contract import (
    ACTIONS,
    ALLOWED_ACTIONS,
    BUCKETS,
    CONFIDENCES,
)
from email_classifier.core.types import Classification


def test_classification_has_seven_frozen_fields():
    c = Classification(
        thread_id="t1",
        bucket="A",
        subtype="human_real",
        confidence="high",
        suggested_action="keep_visible",
        suggested_label=None,
        reasoning="real person",
    )
    assert c.bucket == "A"
    assert c.suggested_label is None


def test_table_matches_taxonomy():
    assert ALLOWED_ACTIONS["A"] == frozenset({"keep_visible", "needs_reply"})
    assert ALLOWED_ACTIONS["B"] == frozenset({"label_archive", "keep_visible"})
    assert ALLOWED_ACTIONS["C"] == frozenset({"label_archive", "mark_for_delete"})
    assert ALLOWED_ACTIONS["D"] == frozenset({"mark_for_delete", "label_archive"})
    assert set(BUCKETS) == {"A", "B", "C", "D"}
    assert set(CONFIDENCES) == {"high", "medium", "low"}
    assert "escalate_to_llm" in ACTIONS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_contract.py -q`
Expected: FAIL — `ModuleNotFoundError: email_classifier.core.contract` / `ImportError: Classification`.

- [ ] **Step 3: Add the `Classification` dataclass**

Append to `email_classifier/core/types.py` (after the existing `RuleMatch` dataclass):

```python
@dataclass
class Classification:
    """Frozen classification contract (figé le 14 juin) — étage 2 output.

    The 7 fields are exactly the contract; do not add provider-specific fields.
    """

    thread_id: str
    bucket: str                  # "A" | "B" | "C" | "D"
    subtype: str
    confidence: str              # "high" | "medium" | "low"
    suggested_action: str        # keep_visible | label_archive | mark_for_delete | needs_reply | escalate_to_llm
    suggested_label: str | None
    reasoning: str
```

- [ ] **Step 4: Create `email_classifier/core/contract.py` with the constants**

```python
"""Classification contract — single source of truth (STU-390).

The action↔bucket table (INV-08) lives here once. Two callers use it:
pytest (via validate_classification) and the Studio engine (via stage2.validate_cli,
wired to the contract's `validators` hook).
"""

BUCKETS = ("A", "B", "C", "D")
CONFIDENCES = ("high", "medium", "low")
ACTIONS = (
    "keep_visible",
    "label_archive",
    "mark_for_delete",
    "needs_reply",
    "escalate_to_llm",
)

# INV-08 — which actions are coherent with which bucket.
# escalate_to_llm is the universal fallback (allowed for every bucket) and is
# folded in at check time, not stored here.
ALLOWED_ACTIONS: dict[str, frozenset[str]] = {
    "A": frozenset({"keep_visible", "needs_reply"}),
    "B": frozenset({"label_archive", "keep_visible"}),
    "C": frozenset({"label_archive", "mark_for_delete"}),
    "D": frozenset({"mark_for_delete", "label_archive"}),
}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/test_contract.py -q`
Expected: PASS (2 tests).

- [ ] **Step 6: Commit**

```bash
git add email_classifier/core/types.py email_classifier/core/contract.py tests/test_contract.py
git commit -m "feat: classification contract dataclass + action↔bucket table (STU-390)"
```

---

## Task 2: The binary validator `validate_classification`

**Files:**
- Modify: `email_classifier/core/contract.py`
- Test: `tests/test_contract.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_contract.py`:

```python
import pytest

from email_classifier.core.contract import (
    ValidationResult,
    enum_and_consistency_errors,
    validate_classification,
)


def _valid_payload(bucket="A", action="keep_visible") -> dict:
    return {
        "thread_id": "t1",
        "bucket": bucket,
        "subtype": "some_subtype",
        "confidence": "high",
        "suggested_action": action,
        "suggested_label": None,
        "reasoning": "because",
    }


def test_valid_payload_per_bucket_passes():
    for bucket, actions in ALLOWED_ACTIONS.items():
        for action in actions:
            res = validate_classification(_valid_payload(bucket, action))
            assert res.valid is True, (bucket, action, res.errors)
            assert res.errors == []


def test_escalate_to_llm_valid_for_every_bucket():
    for bucket in BUCKETS:
        res = validate_classification(_valid_payload(bucket, "escalate_to_llm"))
        assert res.valid is True, (bucket, res.errors)


@pytest.mark.parametrize(
    "bucket,action",
    [
        (b, a)
        for b in BUCKETS
        for a in ACTIONS
        if a != "escalate_to_llm" and a not in ALLOWED_ACTIONS[b]
    ],
)
def test_forbidden_pair_is_invalid(bucket, action):
    res = validate_classification(_valid_payload(bucket, action))
    assert res.valid is False
    assert any("not allowed for bucket" in e for e in res.errors)


def test_bad_confidence_enum_is_invalid():
    payload = _valid_payload()
    payload["confidence"] = "0.9"
    res = validate_classification(payload)
    assert res.valid is False
    assert any("confidence" in e for e in res.errors)


def test_missing_field_is_invalid():
    payload = _valid_payload()
    del payload["reasoning"]
    res = validate_classification(payload)
    assert res.valid is False
    assert any("reasoning" in e for e in res.errors)


def test_wrong_type_is_invalid():
    payload = _valid_payload()
    payload["suggested_label"] = 123  # not str | null
    res = validate_classification(payload)
    assert res.valid is False
    assert any("suggested_label" in e for e in res.errors)


def test_unexpected_field_is_invalid():
    payload = _valid_payload()
    payload["provider_category"] = "promotions"  # input-only, never in output contract
    res = validate_classification(payload)
    assert res.valid is False
    assert any("unexpected field" in e for e in res.errors)


def test_result_is_binary_and_collects_all_errors():
    res = validate_classification({})  # everything missing
    assert isinstance(res, ValidationResult)
    assert res.valid is False
    assert len(res.errors) >= 6  # all required fields reported, not fail-fast


def test_cli_helper_matches_validator_on_enums():
    assert enum_and_consistency_errors("A", "high", "label_archive")  # forbidden -> errors
    assert enum_and_consistency_errors("A", "high", "keep_visible") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_contract.py -q`
Expected: FAIL — `ImportError: cannot import name 'validate_classification'`.

- [ ] **Step 3: Implement the validator**

Append to `email_classifier/core/contract.py`:

```python
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


_REQUIRED_STR_FIELDS = (
    "thread_id",
    "bucket",
    "subtype",
    "confidence",
    "suggested_action",
    "reasoning",
)
_ALL_FIELDS = _REQUIRED_STR_FIELDS + ("suggested_label",)


def enum_and_consistency_errors(bucket: str, confidence: str, action: str) -> list[str]:
    """Enum membership + the action↔bucket table (INV-08). Factored out so the table
    has exactly one definition shared by validate_classification (and exercised directly
    by the CLI through the full validator).
    """
    errors: list[str] = []
    if bucket not in BUCKETS:
        errors.append(f"bucket '{bucket}' not in {list(BUCKETS)}")
    if confidence not in CONFIDENCES:
        errors.append(f"confidence '{confidence}' not in {list(CONFIDENCES)}")
    if action not in ACTIONS:
        errors.append(f"suggested_action '{action}' not in {list(ACTIONS)}")
    if bucket in BUCKETS and action in ACTIONS:
        allowed = ALLOWED_ACTIONS[bucket] | {"escalate_to_llm"}
        if action not in allowed:
            errors.append(
                f"action '{action}' not allowed for bucket '{bucket}' "
                f"(allowed: {sorted(allowed)})"
            )
    return errors


def validate_classification(data: dict) -> ValidationResult:
    """Full binary validator (INV-08). Collects ALL errors (never fail-fast) so the
    error list can serve as enriched RALPH retry feedback. Pure: no LLM, no I/O.
    """
    if not isinstance(data, dict):
        return ValidationResult(False, [f"expected object, got {type(data).__name__}"])

    errors: list[str] = []

    for key in data:
        if key not in _ALL_FIELDS:
            errors.append(f"unexpected field: {key}")

    for f in _REQUIRED_STR_FIELDS:
        if f not in data:
            errors.append(f"missing required field: {f}")
        elif not isinstance(data[f], str) or data[f] == "":
            errors.append(f"field '{f}' must be a non-empty string")

    if "suggested_label" not in data:
        errors.append("missing required field: suggested_label")
    elif data["suggested_label"] is not None and not isinstance(data["suggested_label"], str):
        errors.append("field 'suggested_label' must be a string or null")

    bucket, confidence, action = (
        data.get("bucket"),
        data.get("confidence"),
        data.get("suggested_action"),
    )
    # Enum + table checks only when the three are present as strings (type/presence
    # errors above already cover the rest — avoids noisy duplicate messages).
    if all(isinstance(v, str) for v in (bucket, confidence, action)):
        errors.extend(enum_and_consistency_errors(bucket, confidence, action))

    return ValidationResult(valid=not errors, errors=errors)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_contract.py -q`
Expected: PASS (all tests, including the parametrized forbidden-pair cases).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python -m pytest -q`
Expected: PASS (all prior 76 tests + the new ones).

- [ ] **Step 6: Commit**

```bash
git add email_classifier/core/contract.py tests/test_contract.py
git commit -m "feat: binary classification validator with all-error collection (STU-390)"
```

---

## Task 3: Validator CLI (`stage2/validate_cli.py`)

The CLI is the bridge to the Studio `validators` hook: the engine pipes the real stage
output (the classification) to stdin; the CLI runs the full `validate_classification` and
exits non-zero on invalid output so RALPH retries.

**Files:**
- Create: `email_classifier/stage2/validate_cli.py`
- Test: `tests/test_validate_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_validate_cli.py`:

```python
import io
import json

from email_classifier.stage2.validate_cli import main


def _payload(bucket="C", action="label_archive") -> str:
    return json.dumps(
        {
            "thread_id": "t1",
            "bucket": bucket,
            "subtype": "merchant",
            "confidence": "high",
            "suggested_action": action,
            "suggested_label": "Newsletters",
            "reasoning": "promo",
        }
    )


def test_cli_valid_payload_exits_zero(capsys):
    code = main(stdin=io.StringIO(_payload()))
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out == {"valid": True, "errors": []}


def test_cli_forbidden_pair_exits_nonzero(capsys):
    code = main(stdin=io.StringIO(_payload(bucket="A", action="label_archive")))
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["valid"] is False
    assert any("not allowed for bucket" in e for e in out["errors"])


def test_cli_invalid_json_exits_nonzero(capsys):
    code = main(stdin=io.StringIO("not json"))
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["valid"] is False
    assert out["errors"]


def test_cli_missing_field_exits_nonzero(capsys):
    code = main(stdin=io.StringIO('{"bucket": "A"}'))
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["valid"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_validate_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: email_classifier.stage2.validate_cli`.

- [ ] **Step 3: Implement the CLI**

Create `email_classifier/stage2/validate_cli.py`:

```python
"""Studio `validators` hook entrypoint (STU-390).

The Studio engine pipes the stage output (a candidate classification) as JSON on stdin.
We run the full validate_classification and print {"valid": bool, "errors": [...]} as
JSON, exiting non-zero when invalid. A non-zero exit makes the hook fail, which drives a
RALPH retry with the errors as feedback. Validation runs against the REAL output, so the
agent cannot fake it.
"""

import json
import sys
from typing import TextIO

from email_classifier.core.contract import validate_classification


def main(stdin: TextIO | None = None) -> int:
    stream = stdin if stdin is not None else sys.stdin
    raw = stream.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"valid": False, "errors": [f"invalid JSON: {exc}"]}))
        return 1

    result = validate_classification(data)
    print(json.dumps({"valid": result.valid, "errors": result.errors}))
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_validate_cli.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Manual smoke (exercise the real stdin process boundary the hook uses)**

Run:
```bash
echo '{"thread_id":"t1","bucket":"C","subtype":"x","confidence":"high","suggested_action":"label_archive","suggested_label":"News","reasoning":"y"}' | python -m email_classifier.stage2.validate_cli; echo "exit=$?"
echo '{"thread_id":"t1","bucket":"A","subtype":"x","confidence":"high","suggested_action":"label_archive","suggested_label":null,"reasoning":"y"}' | python -m email_classifier.stage2.validate_cli; echo "exit=$?"
```
Expected: first prints `{"valid": true, "errors": []}` and `exit=0`; second prints a `valid:false` payload with a "not allowed for bucket 'A'" error and `exit=1`.

- [ ] **Step 6: Commit**

```bash
git add email_classifier/stage2/validate_cli.py tests/test_validate_cli.py
git commit -m "feat: Studio validators-hook CLI reading classification from stdin (STU-390)"
```

---

## Task 4: `.studio/` pipeline configs

**Files (all create):** `.studio/config.yaml`, `.studio/invariants.md`, `.studio/skills/taxonomy.skill.md`, `.studio/agents/classifier.agent.yaml`, `.studio/contracts/classification.contract.yaml`, `.studio/pipelines/classify.pipeline.yaml`
**Modify:** `.gitignore`

> No unit test drives this task (engine configs). Verification is the structural smoke check in Step 8 plus the manual eval in Task 5.

- [ ] **Step 1: Provider config**

Create `.studio/config.yaml`:

```yaml
providers:
  anthropic:
    apiKey: ${ANTHROPIC_API_KEY}
defaults:
  provider: anthropic
  # Strong model on purpose: Studio is governance on top of the best model.
  model: claude-opus-4-8
```

- [ ] **Step 2: Project invariants (auto-injected into every agent)**

Create `.studio/invariants.md`:

```markdown
# Invariants — email-classifier (étage 2)

- **INV-08 — validation binaire.** A classification is either valid or not; there is
  no score. `suggested_action` must be coherent with `bucket` per the taxonomy table.
- **Jamais de suppression réelle.** `mark_for_delete` only marks; never assume real
  deletion. Under any doubt between A and another bucket, prefer A (visible).
- **Neutralité provider.** Classify only from universal signals: sender, subject,
  headers, presence of `List-Unsubscribe`. `provider_category` is a weak, noisy hint —
  never a deciding feature. An email with no `provider_category` (e.g. Outlook) must be
  classified with the same quality as a Gmail one.
```

- [ ] **Step 3: Taxonomy skill**

Create `.studio/skills/taxonomy.skill.md`:

```markdown
---
name: taxonomy
description: A/B/C/D email classification taxonomy and per-bucket valid actions.
---

# Taxonomie de classement (source de vérité)

Quatre buckets, du plus important au plus jetable.

## Bucket A — ESSENTIEL
Humains réels, argent, sécurité, actions à échéance. Reste visible.
Sous-types: humain réel, financier transactionnel, reçus/facturation, crédit/dossier
officiel, sécurité/compte, confirmation de commande.
Actions valides: `keep_visible`, `needs_reply`.

## Bucket B — TRANSACTIONNEL JETABLE
Utile sur le coup, zéro valeur après 48h. Archiver + label.
Sous-types: suivi de livraison, sondage post-achat, alerte récurrente, reçu de transport.
Actions valides: `label_archive`, `keep_visible`.

## Bucket C — INFOLETTRES / PROMO
Marchands légitimes, contenu marketing. Archiver + label, ou désabonner.
Sous-types: marchand e-commerce, SaaS/outil marketing, contenu/média, newsletter produit,
créateur/abonnement.
Actions valides: `label_archive`, `mark_for_delete`.

## Bucket D — DÉMARCHAGE / QUASI-SPAM
Sollicitation non désirée. Corbeille via `mark_for_delete` (jamais suppression réelle).
Sous-types: démarchage financier, promo locale froide, réengagement insistant.
Actions valides: `mark_for_delete`, `label_archive`.

## Cas LinkedIn
`jobalerts-noreply@linkedin.com` → bucket C (contenu récurrent). `inmail-hit-reply@linkedin.com`
(recruteur en direct) → potentiellement bucket A. Ne pas confondre.

## Garde-fous
- En cas de doute, défaut sur A (visible).
- `escalate_to_llm` est permis pour tout bucket (fallback).
- Neutralité provider: features universelles seulement; `provider_category` = indice faible.
```

- [ ] **Step 4: Classifier agent**

Create `.studio/agents/classifier.agent.yaml`:

```yaml
name: classifier
version: 1.0.0
description: Classifies a grey-zone email into the A/B/C/D taxonomy under the frozen contract.

systemMessage: |
  You classify a SINGLE email that the deterministic rule engine could not resolve
  (the "grey zone"). Produce output that conforms exactly to the classification
  contract (7 fields): thread_id, bucket (A|B|C|D), subtype, confidence
  (high|medium|low), suggested_action, suggested_label (string or null), reasoning.

  - Copy thread_id verbatim from the input email.
  - Choose bucket and suggested_action consistently with the taxonomy skill.
  - suggested_action MUST be valid for the chosen bucket (see taxonomy). escalate_to_llm
    is always allowed as a fallback.
  - Output ONLY the 7 contract fields — no extra fields.

  Provider neutrality: classify ONLY from universal signals — sender address/domain,
  subject, headers, presence of List-Unsubscribe. Treat provider_category as a weak,
  noisy hint at most; never as a deciding feature.

skills:
  - taxonomy

parameters:
  temperature: 0.2
  maxTokens: 2048
```

- [ ] **Step 5: Output contract (with the `validators` hook)**

Create `.studio/contracts/classification.contract.yaml`:

```yaml
name: classification
version: 1

# required_fields is a cheap presence gate (all Studio enforces declaratively).
schema:
  required_fields:
    - thread_id
    - bucket
    - subtype
    - confidence
    - suggested_action
    - suggested_label
    - reasoning

# The real binary contract (enums + types + action↔bucket table, INV-08) runs against
# the actual output via the external validator. The engine pipes the output JSON to the
# command's stdin and expects {valid, errors}; non-zero exit drives a RALPH retry.
validators:
  - name: classification
    command: "python -m email_classifier.stage2.validate_cli"
```

- [ ] **Step 6: Pipeline**

Create `.studio/pipelines/classify.pipeline.yaml`:

```yaml
name: classify
description: Classify grey-zone (escalated) emails under the frozen contract, validated by RALPH.
version: 1
stages:
  - name: classify
    kind: classification
    agent: classifier
    contract: classification   # REQUIRED — engine only loads/enforces a contract when the stage declares it
    ralph:
      max_attempts: 3
      retry_strategy: prompt_escalation
    context:
      include:
        - input
```

- [ ] **Step 7: Ignore Studio run artifacts**

Append to `.gitignore`:

```
.studio/runs/
```

- [ ] **Step 8: Structural smoke check**

Run:
```bash
studio list pipelines 2>&1 | head
studio list agents 2>&1 | head
```
Expected: `classify` under pipelines and `classifier` under agents (the engine parses the YAML without schema errors). If `list` is unavailable, confirm the files parse as YAML:
```bash
python -c "import yaml,glob; [yaml.safe_load(open(f)) for f in glob.glob('.studio/**/*.yaml', recursive=True)]; print('yaml ok')"
```
Expected: `yaml ok`.

- [ ] **Step 9: Commit**

```bash
git add .studio .gitignore
git commit -m "feat: .studio classify pipeline using the validators hook (STU-390)"
```

---

## Task 5: Manual eval runbook (LLM-dependent acceptance criteria)

**Files:**
- Create: `docs/superpowers/runbooks/2026-06-15-classify-manual-eval.md`

> Requires the Studio `validators` hook merged/installed, and `ANTHROPIC_API_KEY`.
> Verifies the criteria that need a live LLM: grey-zone classification, RALPH retry-on-fail,
> provider-neutrality equivalence.

- [ ] **Step 1: Write the runbook**

Create `docs/superpowers/runbooks/2026-06-15-classify-manual-eval.md`:

```markdown
# Manual eval — classify pipeline (STU-390)

Covers acceptance criteria needing a real LLM. Requires `ANTHROPIC_API_KEY` and the
Studio `validators` hook installed.

## 1. Grey-zone classification (5–10 cases)
1. Assemble a small input set of escalated emails (those stage 1 marks
   `escalate_to_llm` — e.g. `fixtures/bucket_a/human_real.json`,
   `fixtures/linkedin/inmail_recruiter.json`, plus a few ambiguous ones).
2. Run:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   studio run classify --input-file <your-input>.yaml --live
   ```
3. Confirm each output is a valid classification (bucket/action coherent) and the bucket
   assignment is reasonable per the taxonomy. Record results.

## 2. RALPH retry on validation fail (no crash)
1. Inspect a run where the agent first produced an invalid pair (or induce one via higher
   temperature).
2. In `studio logs <run-id>`, confirm an invalid attempt triggered a retry (up to 3) with
   the validator errors as feedback, and the run did not crash.

## 3. Provider-neutrality equivalence
1. Pick 2–3 grey-zone emails. Run each twice: with `provider_category` present, and with
   the field removed.
2. Confirm the resulting `bucket` is identical across both runs. Any divergence is a
   neutrality leak to fix in the agent/skill prompt.
```

- [ ] **Step 2: Final full test run**

Run: `python -m pytest -q`
Expected: PASS — all tests green.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/runbooks/2026-06-15-classify-manual-eval.md
git commit -m "docs: manual eval runbook for classify pipeline (STU-390)"
```

---

## Self-Review

**Spec coverage:**
- §2 contract core (`ALLOWED_ACTIONS`, `Classification`, `validate_classification`) → Tasks 1–2 ✓
- §3 `.studio/` configs + `validators` hook → Tasks 3–4 ✓
- §4 tests (`test_contract.py`, `test_validate_cli.py`) → Tasks 2–3 ✓
- §4 manual eval (grey-zone, RALPH retry, neutrality equivalence) → Task 5 ✓
- Provider neutrality (req #4) → invariants.md + taxonomy skill + agent systemMessage (Task 4) + `test_unexpected_field_is_invalid` guards the output contract ✓

**Acceptance criteria:** frozen schema → Task 1; forbidden-pair rejection → Task 2 (parametrized); bad confidence/missing/type → Task 2; binary `valid` → Task 2; mocked-JSON validation w/o LLM → Task 2; grey-zone sample + RALPH retry → Task 5 (manual); provider-neutral classification → Task 4 + 5; `pytest` passes → Tasks 2/5.

**Type consistency:** `ValidationResult{valid, errors}` (Task 2) returned by `validate_classification`; `validate_cli.main(stdin=None)` (Task 3) calls it; `enum_and_consistency_errors(bucket, confidence, action)` (Task 2) shared. Contract `validators[].command` matches the module path `email_classifier.stage2.validate_cli`. Model id `claude-opus-4-8` consistent with spec §3.

**Placeholder scan:** none.

**Open risks flagged for execution:** confirm `claude-opus-4-8` and `retry_strategy: prompt_escalation` against the installed Studio build during Task 4 Step 8 / Task 5; the `validators` hook must be merged/installed before Task 5 (pytest tasks 1–3 don't need it).
```
