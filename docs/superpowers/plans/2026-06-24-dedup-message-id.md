# Déduplication par message_id Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the individual Gmail message — not the thread — the unit of processing and deduplication, so `totals.emails` and `ratio_deterministic` reflect unique messages.

**Architecture:** Add a required, provider-neutral `message_id` to the `EmailInput` contract, populated by the Gmail projection from the already-fetched `raw["id"]`. The orchestrator dedupes on `message_id` instead of `thread_id`; `thread_id` is kept as conversation-grouping context. Verdicts and the frontier log both carry `message_id`. Two multi-message threads become regression fixtures.

**Tech Stack:** Python 3, dataclasses, jsonschema, pytest.

## Global Constraints

- **Provider-neutral core:** `core/schema.py` and `core/types.py` must name no mail provider. The Gmail-specific fact (`message_id` ← `raw["id"]`) lives only in `fetch/gmail/projection.py`. (Copied from spec.)
- **`message_id` is required** in the input schema — no fallback to `thread_id`.
- **Keep `thread_id`** alongside `message_id` (grouping context).
- **Per-message classification:** each message is classified independently; never collapse distinct messages of a thread.
- **Fail loud** on contract violations (missing `message_id` raises `ValidationError`).

---

### Task 1: Thread `message_id` through the input contract

Introduces the field everywhere an `EmailInput` is built or validated, **without changing dedup/verdict/log behavior yet**. The suite stays green at the end.

**Files:**
- Modify: `email_classifier/core/types.py` (EmailInput)
- Modify: `email_classifier/core/schema.py:12-42`
- Modify: `email_classifier/fetch/gmail/projection.py:80-92`
- Modify: all 22 files under `fixtures/**/*.json`
- Test: `tests/test_schema.py`, `tests/test_gmail_projection.py`, `tests/test_gmail_fetch.py`
- Modify (helpers only): `tests/test_orchestrate_pipeline.py`, `tests/test_frontier_log.py`

**Interfaces:**
- Produces: `EmailInput(message_id: str, thread_id: str, from_address: str, subject: str, headers: dict[str,str], body_snippet: str, date: str, provider_category: str | None = None)`. `message_id` is the new **first** field and is required.
- Produces: `projection.message_to_input(raw)` dict now includes key `"message_id"` = `raw["id"]`.

- [ ] **Step 1: Write the failing schema test**

In `tests/test_schema.py`, add `message_id` to the valid dicts and add a required-field test. Replace `test_validate_returns_email_input`, `test_validate_accepts_optional_provider_category`, the two `load_many` dicts, and add `test_validate_raises_on_missing_message_id`:

```python
def test_validate_returns_email_input():
    data = {
        "message_id": "m001",
        "thread_id": "t001",
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {},
        "body_snippet": "Hello",
        "date": "2026-06-10T14:30:00+00:00",
    }
    result = validate(data)
    assert isinstance(result, EmailInput)
    assert result.message_id == "m001"
    assert result.thread_id == "t001"
    assert result.provider_category is None


def test_validate_raises_on_missing_message_id():
    data = {
        "thread_id": "t001",
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {},
        "body_snippet": "Hello",
        "date": "2026-06-10T14:30:00+00:00",
        # message_id missing
    }
    with pytest.raises(ValidationError):
        validate(data)
```

Also add `"message_id": "m002"` to the dict in `test_validate_accepts_optional_provider_category`, `"message_id": "m1"` / `"message_id": "m2"` to the two dicts in `test_load_many_reads_array_dump`, and assert `[e.message_id for e in result] == ["m1", "m2"]` there.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_schema.py -v`
Expected: FAIL — `validate` does not yet accept/require `message_id` (e.g. `additionalProperties` rejects `message_id`, or `EmailInput` has no such field).

- [ ] **Step 3: Add the field to the dataclass**

In `email_classifier/core/types.py`, make `message_id` the first field of `EmailInput`:

```python
@dataclass
class EmailInput:
    """Provider-neutral email contract — the boundary between any fetch and all
    downstream stages. No field may name or assume a specific mail provider.
    A Gmail fetch and a future Outlook/Graph fetch must both produce this shape.
    """

    message_id: str  # stable, unique per message — the unit of processing/dedup
    thread_id: str   # conversation-grouping context; NOT unique per message
    from_address: str
    subject: str
    headers: dict[str, str]
    body_snippet: str
    date: str  # ISO 8601 (e.g. "2026-06-15T12:00:00+00:00")
    # Provider's own coarse category, if any (Gmail tab, Outlook Focused/Other).
    # Low-weight, noisy hint only — never a structural classification feature.
    provider_category: str | None = None
```

- [ ] **Step 4: Add the field to the schema**

In `email_classifier/core/schema.py`, add `message_id` to `required` and `properties`, and to the `EmailInput(...)` construction:

```python
_SCHEMA: dict = {
    "type": "object",
    "required": ["message_id", "thread_id", "from_address", "subject", "headers", "body_snippet", "date"],
    "properties": {
        "message_id": {"type": "string"},
        "thread_id": {"type": "string"},
        "from_address": {"type": "string"},
        "subject": {"type": "string"},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "body_snippet": {"type": "string"},
        "date": {"type": "string"},
        # Provider's native category hint, if any. Nullable, low-weight signal.
        "provider_category": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


def validate(data: dict) -> EmailInput:
    jsonschema.validate(data, _SCHEMA)
    return EmailInput(
        message_id=data["message_id"],
        thread_id=data["thread_id"],
        from_address=data["from_address"],
        subject=data["subject"],
        headers=data["headers"],
        body_snippet=data["body_snippet"],
        date=data["date"],
        provider_category=data.get("provider_category"),
    )
```

- [ ] **Step 5: Populate message_id in the Gmail projection**

In `email_classifier/fetch/gmail/projection.py`, add `message_id` as the first key of the returned dict in `message_to_input`:

```python
def message_to_input(raw: dict) -> dict:
    """Project a Gmail `messages.get(format='metadata')` resource to EmailInput."""
    headers = raw.get("payload", {}).get("headers", [])
    by_name = {h["name"].lower(): h["value"] for h in headers}
    return {
        "message_id": raw["id"],
        "thread_id": raw["threadId"],
        "from_address": _extract_from_address(by_name.get("from", "")),
        "subject": by_name.get("subject", ""),
        "headers": _allowlisted_headers(headers),
        "body_snippet": html.unescape(raw.get("snippet", "")),
        "date": _iso_date(raw["internalDate"]),
        "provider_category": _category_from_labels(raw.get("labelIds", [])),
    }
```

- [ ] **Step 6: Add projection tests for message identity**

In `tests/test_gmail_projection.py`, add:

```python
def test_message_id_comes_from_raw_message_id():
    result = projection.message_to_input(_raw())
    assert result["message_id"] == "msg_1"


def test_same_thread_different_messages_get_distinct_message_ids():
    raw_a = {**_raw(thread_id="shared"), "id": "msg_a"}
    raw_b = {**_raw(thread_id="shared"), "id": "msg_b"}
    a = projection.message_to_input(raw_a)
    b = projection.message_to_input(raw_b)
    assert a["thread_id"] == b["thread_id"] == "shared"
    assert a["message_id"] != b["message_id"]
```

- [ ] **Step 7: Update the fetch-loop test to assert message_id is carried**

In `tests/test_gmail_fetch.py`, inside `test_fetch_inputs_paginates_until_max_results`, add after the existing asserts:

```python
    assert [r["message_id"] for r in result] == ["a", "b", "c"]
```

- [ ] **Step 8: Add message_id to all 22 fixtures**

Run this one-off from the repo root (adds a stable `message_id` = `"msg_" + thread_id` to every fixture, preserving 2-space JSON):

```bash
python - <<'PY'
import json, pathlib
for p in sorted(pathlib.Path("fixtures").rglob("*.json")):
    data = json.loads(p.read_text(encoding="utf-8"))
    if "message_id" not in data:
        data = {"message_id": "msg_" + data["thread_id"], **data}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("updated", p)
PY
```

- [ ] **Step 9: Keep existing EmailInput test helpers compiling**

`EmailInput` now requires `message_id`. Update the two test helpers to default `message_id` to `thread_id` (so existing call sites are unchanged), and fix the positional constructions.

In `tests/test_orchestrate_pipeline.py`, replace `_email` and the two raw `EmailInput(...)` calls:

```python
def _email(thread_id, from_address, message_id=None):
    return EmailInput(
        message_id=message_id or thread_id, thread_id=thread_id,
        from_address=from_address, subject="S", headers={}, body_snippet="b",
        date="2026-06-10T00:00:00+00:00",
    )
```

In `test_dedupe_keeps_latest_date_regardless_of_order` (will be rewritten in Task 2, but must compile now), give the positional `EmailInput`s a leading `message_id`:

```python
    old = EmailInput("t", "t", "a@b.com", "old", {}, "b", "2026-06-10T00:00:00+00:00")
    new = EmailInput("t", "t", "a@b.com", "new", {}, "b", "2026-06-12T00:00:00+00:00")
```

In `test_run_dedupes_duplicate_threads` (also rewritten in Task 2), add `message_id="grey1"` to the inline kwargs `EmailInput(...)`.

In `tests/test_frontier_log.py`, update `_make_email`:

```python
def _make_email(thread_id: str = "t001") -> EmailInput:
    return EmailInput(
        message_id=thread_id, thread_id=thread_id,
        from_address="sender@example.com", subject="Test", headers={},
        body_snippet="Hello", date="2026-06-10T14:30:00+00:00",
    )
```

- [ ] **Step 10: Run the full suite**

Run: `pytest -q`
Expected: PASS. (`test_all_fixtures_are_found` still asserts 22; behavior unchanged.)

- [ ] **Step 11: Commit**

```bash
git add email_classifier/core/types.py email_classifier/core/schema.py \
        email_classifier/fetch/gmail/projection.py fixtures \
        tests/test_schema.py tests/test_gmail_projection.py tests/test_gmail_fetch.py \
        tests/test_orchestrate_pipeline.py tests/test_frontier_log.py
git commit -m "feat(STU-395): add required message_id to EmailInput contract"
```

---

### Task 2: Deduplicate on message_id, not thread_id

Changes the orchestrator's dedup unit from thread to message. Distinct messages of one thread now all survive; only a true `message_id` duplicate (same message fetched twice) collapses.

**Files:**
- Modify: `email_classifier/orchestrate/pipeline.py:22-40,87-101`
- Test: `tests/test_orchestrate_pipeline.py`

**Interfaces:**
- Consumes: `EmailInput.message_id` (Task 1).
- Produces: `_dedupe_by_message(emails: list[EmailInput]) -> list[EmailInput]` (replaces `_dedupe_by_thread`). One entry per `message_id`, latest-by-date wins, first-appearance order preserved.

- [ ] **Step 1: Rewrite the dedup unit test**

In `tests/test_orchestrate_pipeline.py`, replace `test_dedupe_keeps_latest_date_regardless_of_order`:

```python
def test_dedupe_collapses_only_true_message_id_duplicates():
    from email_classifier.orchestrate.pipeline import _dedupe_by_message

    # Same message_id fetched twice -> collapse, keep latest date.
    old = EmailInput("m1", "t", "a@b.com", "old", {}, "b", "2026-06-10T00:00:00+00:00")
    new = EmailInput("m1", "t", "a@b.com", "new", {}, "b", "2026-06-12T00:00:00+00:00")
    out = _dedupe_by_message([old, new])
    assert len(out) == 1 and out[0].subject == "new"

    # Two distinct messages sharing a thread -> BOTH survive.
    a = EmailInput("m1", "shared", "a@b.com", "first", {}, "b", "2026-06-10T00:00:00+00:00")
    b = EmailInput("m2", "shared", "a@b.com", "second", {}, "b", "2026-06-11T00:00:00+00:00")
    both = _dedupe_by_message([a, b])
    assert [e.message_id for e in both] == ["m1", "m2"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_orchestrate_pipeline.py::test_dedupe_collapses_only_true_message_id_duplicates -v`
Expected: FAIL with `ImportError` / `cannot import name '_dedupe_by_message'`.

- [ ] **Step 3: Implement `_dedupe_by_message`**

In `email_classifier/orchestrate/pipeline.py`, replace `_dedupe_by_thread`:

```python
def _dedupe_by_message(emails: list[EmailInput]) -> list[EmailInput]:
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
```

- [ ] **Step 4: Point `run` at the new function and update its log line**

In `email_classifier/orchestrate/pipeline.py`, in `run`:

```python
    unique = _dedupe_by_message(emails)
    collapsed = len(emails) - len(unique)
    if collapsed:
        _log.info(
            "deduped %d emails -> %d unique messages (collapsed %d duplicate(s))",
            len(emails), len(unique), collapsed,
        )
```

- [ ] **Step 5: Rewrite the run-level dedup test for per-message behavior**

In `tests/test_orchestrate_pipeline.py`, replace `test_run_dedupes_duplicate_threads`. First, make `FakeClassifier` key on `message_id` (so distinct messages in a thread don't collide):

```python
class FakeClassifier:
    """Records the message_ids it is asked to classify and returns canned results."""

    def __init__(self, results=None):
        self.results = results or {}
        self.seen = []

    def classify(self, email):
        self.seen.append(email.message_id)
        return self.results.get(
            email.message_id,
            Stage2Result(classification=Classification(
                thread_id=email.thread_id, bucket="C", subtype="newsletter",
                confidence="medium", suggested_action="label_archive",
                suggested_label=None, reasoning="default fake",
            )),
        )
```

Then the new test (distinct messages survive; true duplicate collapses):

```python
def test_run_keeps_distinct_messages_and_collapses_true_duplicates(tmp_path):
    rules, policy = _load()
    emails = [
        # two distinct messages, same thread -> two verdicts
        _email("grey_a", "a@unknown.test", message_id="m_a"),
        EmailInput(message_id="m_b", thread_id="grey_a", from_address="a@unknown.test",
                   subject="second", headers={}, body_snippet="b2",
                   date="2026-06-12T00:00:00+00:00"),
        # true duplicate of m_a -> collapses
        EmailInput(message_id="m_a", thread_id="grey_a", from_address="a@unknown.test",
                   subject="dup", headers={}, body_snippet="b3",
                   date="2026-06-13T00:00:00+00:00"),
    ]
    fake = FakeClassifier()
    report = run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert report.totals["emails"] == 2            # m_a (deduped) + m_b
    assert sorted(fake.seen) == ["m_a", "m_b"]     # each unique message classified once
    # frontier log has one line per unique message
    assert len((tmp_path / "f.jsonl").read_text().strip().splitlines()) == 2
```

- [ ] **Step 6: Run the suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add email_classifier/orchestrate/pipeline.py tests/test_orchestrate_pipeline.py
git commit -m "feat(STU-395): dedupe on message_id; distinct thread messages survive"
```

---

### Task 3: Carry message_id into verdicts and the report

The verdict's identity becomes the message, so the report can guarantee no message appears twice and `totals.emails` counts unique messages.

**Files:**
- Modify: `email_classifier/orchestrate/report.py:11-23,84-92`
- Modify: `email_classifier/orchestrate/pipeline.py:43-84,102-110`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `EmailInput.message_id`.
- Produces: `EmailVerdict(message_id: str, thread_id: str, path: str, status: str, bucket, suggested_action, confidence, rule_id, subtype, reasoning, error)` — `message_id` is the new first field. `render_human` lines start with `message_id`.

- [ ] **Step 1: Update report tests to carry message_id**

In `tests/test_report.py`, give the three helpers a `message_id` (defaulting to the existing id arg, which doubles as thread_id here):

```python
def _det(id_, bucket="A", action="keep_visible"):
    return EmailVerdict(
        message_id=id_, thread_id=id_, path="deterministic", status="ok", bucket=bucket,
        suggested_action=action, confidence="high", rule_id="some_rule",
        subtype=None, reasoning=None, error=None,
    )


def _llm(id_, bucket="C", action="label_archive"):
    return EmailVerdict(
        message_id=id_, thread_id=id_, path="llm", status="ok", bucket=bucket,
        suggested_action=action, confidence="medium", rule_id="catch_all",
        subtype="newsletter", reasoning="looks promotional", error=None,
    )


def _llm_err(id_, error="studio_run_timeout"):
    return EmailVerdict(
        message_id=id_, thread_id=id_, path="llm", status="error", bucket=None,
        suggested_action=None, confidence=None, rule_id="catch_all",
        subtype=None, reasoning=None, error=error,
    )
```

Then update `test_render_human_shows_ratio_and_escalated_lines` to expect the message_id at the start of the escalated line:

```python
    assert "c   catch_all -> LLM -> C / label_archive" in text
```

(unchanged string — the id `"c"` is both message_id and thread_id here, so the assertion still holds once render_human prints message_id.)

- [ ] **Step 2: Run report tests to verify they fail**

Run: `pytest tests/test_report.py -v`
Expected: FAIL — `EmailVerdict.__init__` got an unexpected keyword argument `message_id`.

- [ ] **Step 3: Add message_id to EmailVerdict and render_human**

In `email_classifier/orchestrate/report.py`, add the field first and print it:

```python
@dataclass
class EmailVerdict:
    message_id: str         # stable per-message identity — unique within a report
    thread_id: str          # conversation-grouping context
    path: str               # "deterministic" | "llm"
    status: str             # "ok" | "error"
    bucket: str | None
    suggested_action: str | None
    confidence: str | None
    rule_id: str | None     # deterministic path, and catch_all for escalated ones
    subtype: str | None     # llm path only
    reasoning: str | None   # llm path only
    error: str | None       # error-taxonomy tag when status == "error"
```

In `render_human`, change the escalated lines to lead with `message_id`:

```python
    for v in escalated:
        if v.status == "ok":
            lines.append(f"  {v.message_id}   catch_all -> LLM -> {v.bucket} / {v.suggested_action}")
        else:
            lines.append(f"  {v.message_id}   catch_all -> LLM -> ERROR ({v.error})")
```

- [ ] **Step 4: Pass message_id when building verdicts**

In `email_classifier/orchestrate/pipeline.py`, add `message_id=email.message_id,` as the first argument in all three `EmailVerdict(...)` constructions (`_deterministic_verdict`, and both branches of `_llm_verdict`). For example:

```python
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
    )
```

Apply the same `message_id=email.message_id,` first line to both `EmailVerdict(...)` returns in `_llm_verdict`.

- [ ] **Step 5: Run the suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add email_classifier/orchestrate/report.py email_classifier/orchestrate/pipeline.py tests/test_report.py
git commit -m "feat(STU-395): verdicts carry message_id; report keyed per message"
```

---

### Task 4: Record message_id in the frontier log

The frontier log is the eval corpus and the future idempotency-marker substrate; it must record stable message identity.

**Files:**
- Modify: `email_classifier/stage1/frontier_log.py:17-27`
- Test: `tests/test_frontier_log.py`

**Interfaces:**
- Consumes: `EmailInput.message_id`.
- Produces: each JSONL entry gains a `"message_id"` key (keeps `"thread_id"`).

- [ ] **Step 1: Assert message_id in the log entry**

In `tests/test_frontier_log.py`, update `_make_email` to take a distinct message id, then assert it. Change the helper signature and `test_log_writes_valid_jsonl`:

```python
def _make_email(thread_id: str = "t001", message_id: str | None = None) -> EmailInput:
    return EmailInput(
        message_id=message_id or f"m_{thread_id}", thread_id=thread_id,
        from_address="sender@example.com", subject="Test", headers={},
        body_snippet="Hello", date="2026-06-10T14:30:00+00:00",
    )
```

In `test_log_writes_valid_jsonl`, after the existing asserts, add:

```python
    assert entry["message_id"] == "m_thread_001"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/test_frontier_log.py::test_log_writes_valid_jsonl -v`
Expected: FAIL with `KeyError: 'message_id'`.

- [ ] **Step 3: Add message_id to the log entry**

In `email_classifier/stage1/frontier_log.py`, in `log`, add the key first:

```python
        entry = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_id": email.message_id,
            "thread_id": email.thread_id,
            "rule_id": match.rule_id,
            "action": match.action,
            "action_original": match.action_original,
            "confidence": match.confidence,
            "bucket": match.bucket,
        }
```

- [ ] **Step 4: Run the suite**

Run: `pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add email_classifier/stage1/frontier_log.py tests/test_frontier_log.py
git commit -m "feat(STU-395): frontier log records message_id"
```

---

### Task 5: Regression fixtures for multi-message threads

Encodes the two named bug cases as fixtures and an end-to-end regression test proving each message yields its own verdict with no duplicate `message_id`.

**Files:**
- Create: `fixtures/multimsg/vet_invoice_chleo.json`, `fixtures/multimsg/vet_invoice_felix.json`, `fixtures/multimsg/rdv_proposal.json`, `fixtures/multimsg/rdv_confirm_1.json`, `fixtures/multimsg/rdv_confirm_2.json`
- Modify: `tests/test_schema.py:100-102` (fixture count 22 → 27)
- Test: `tests/test_orchestrate_pipeline.py`

**Interfaces:**
- Consumes: `schema.load`, `pipeline.run`, `EmailVerdict.message_id`, `_dedupe_by_message`.

- [ ] **Step 1: Create the two-message vet thread (Chléo + Félix)**

`fixtures/multimsg/vet_invoice_chleo.json`:

```json
{
  "message_id": "19ed1de17eaa17cf",
  "thread_id": "19ed1de17eaa17cf",
  "from_address": "noreply@vetup.io",
  "subject": "Facture vétérinaire",
  "headers": {},
  "body_snippet": "Facture pour Chléo — consultation du 18 juin.",
  "date": "2026-06-18T13:00:00+00:00",
  "provider_category": "updates"
}
```

`fixtures/multimsg/vet_invoice_felix.json`:

```json
{
  "message_id": "19ed2af033b1c8d9",
  "thread_id": "19ed1de17eaa17cf",
  "from_address": "noreply@vetup.io",
  "subject": "Facture vétérinaire",
  "headers": {},
  "body_snippet": "Facture pour Félix — consultation du 19 juin.",
  "date": "2026-06-19T13:00:00+00:00",
  "provider_category": "updates"
}
```

- [ ] **Step 2: Create the three-message RDV thread**

`fixtures/multimsg/rdv_proposal.json`:

```json
{
  "message_id": "19ec512eb15dd7af",
  "thread_id": "19ec512eb15dd7af",
  "from_address": "claudia.savignac@gmail.com",
  "subject": "Proposition de rendez-vous",
  "headers": {},
  "body_snippet": "Est-ce que mardi 14h vous conviendrait ?",
  "date": "2026-06-15T09:00:00+00:00",
  "provider_category": "primary"
}
```

`fixtures/multimsg/rdv_confirm_1.json`:

```json
{
  "message_id": "19ec5240aa01bb22",
  "thread_id": "19ec512eb15dd7af",
  "from_address": "claudia.savignac@gmail.com",
  "subject": "Re: Proposition de rendez-vous",
  "headers": {},
  "body_snippet": "Parfait, je confirme mardi 14h.",
  "date": "2026-06-15T10:30:00+00:00",
  "provider_category": "primary"
}
```

`fixtures/multimsg/rdv_confirm_2.json`:

```json
{
  "message_id": "19ec530b77e9cd41",
  "thread_id": "19ec512eb15dd7af",
  "from_address": "claudia.savignac@gmail.com",
  "subject": "Re: Proposition de rendez-vous",
  "headers": {},
  "body_snippet": "Je vous envoie l'adresse exacte plus tard.",
  "date": "2026-06-15T11:00:00+00:00",
  "provider_category": "primary"
}
```

- [ ] **Step 3: Bump the fixture-count assertion**

In `tests/test_schema.py`, update `test_all_fixtures_are_found`:

```python
def test_all_fixtures_are_found():
    paths = _all_fixture_paths()
    assert len(paths) == 27, f"Expected 27 fixtures, found {len(paths)}"
```

- [ ] **Step 4: Write the end-to-end regression test**

In `tests/test_orchestrate_pipeline.py`, add (uses the real fixtures via `schema.load`):

```python
def test_multimessage_threads_yield_one_verdict_per_message(tmp_path):
    from email_classifier.core import schema
    from tests.conftest import FIXTURES_DIR

    rules, policy = _load()
    paths = sorted((FIXTURES_DIR / "multimsg").glob("*.json"))
    emails = [schema.load(p) for p in paths]
    assert len(emails) == 5

    report = run(emails, rules, policy, FakeClassifier(),
                 FrontierLogger(log_path=tmp_path / "f.jsonl"))

    # Each message is its own verdict; totals reflect unique messages.
    assert report.totals["emails"] == 5
    ids = [v.message_id for v in report.verdicts]
    assert len(ids) == len(set(ids)) == 5            # no message_id appears twice

    # The two named threads keep all their distinct messages.
    by_thread = {}
    for v in report.verdicts:
        by_thread.setdefault(v.thread_id, []).append(v)
    assert len(by_thread["19ed1de17eaa17cf"]) == 2   # Chléo + Félix
    assert len(by_thread["19ec512eb15dd7af"]) == 3   # proposal + 2 confirmations
```

- [ ] **Step 5: Run the suite**

Run: `pytest -q`
Expected: PASS (27 fixtures validate; regression test green).

- [ ] **Step 6: Commit**

```bash
git add fixtures/multimsg tests/test_schema.py tests/test_orchestrate_pipeline.py
git commit -m "test(STU-395): regression fixtures for multi-message threads"
```

---

## Verification (after all tasks)

- [ ] Run `pytest -q` — full suite green.
- [ ] Confirm acceptance criteria against the spec:
  - `message_id` is required and distinct from `thread_id` (Task 1).
  - No `message_id` appears twice in `verdicts` (Tasks 2–3, proven in Task 5).
  - `totals.emails` = unique messages (Task 2, proven in Task 5).
  - Multi-message behavior defined (per-message) and tested via `19ec512eb15dd7af` (3) and `19ed1de17eaa17cf` (2) (Task 5).
  - `ratio_deterministic` recomputed on the corrected denominator (automatic via `build_report`, since `totals.emails` is now correct).
- [ ] Optional manual check: re-run `python run_email.py --dump <corpus>` and confirm `totals.emails` equals the number of unique message ids and the report lines lead with `message_id`.
