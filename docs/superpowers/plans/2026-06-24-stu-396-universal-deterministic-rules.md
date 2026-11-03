# STU-396 Universal Deterministic Rules — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two universal, merchant-agnostic Stage 1 rules — strict One-Click `List-Unsubscribe` and known bulk-sender platforms — each gated by a reusable `not_transactional` guard, so unambiguous promos are decided deterministically instead of escalating to the LLM.

**Architecture:** Extend the declarative rule engine with three new condition keys (`header_value`, `from_domain_suffix`, `guard`). The guard is a named keyword corpus defined once in a new `guards.py` module and referenced by name from YAML — declarative rule structure, encapsulated reusable knowledge. Two new rules sit after all merchant rules and before `catch_all`. The trigger (strong positive signal) and the guard (absence of contradicting signal) are two independent safety barriers.

**Tech Stack:** Python 3.14, PyYAML, jsonschema, pytest.

## Global Constraints

- **Fail loud on config errors.** Unknown condition keys and unknown guard names must raise `ValueError` at `load_rules()` time, never silently no-op. (Consistent with existing unknown-condition-key validation.)
- **Precision over ratio.** Universal rules fire only on unambiguous signal. Under doubt, default to visible (escalate). A false archive (missed invoice/deadline/human mail) is unacceptable; a false escalate (a promo stays visible) is fine.
- **Provider-neutral.** No new field names or assumptions about a specific mail provider. Work only off the existing `EmailInput` contract.
- **No real personal data in fixtures.** Public repo. Use synthetic/`example-*` domains and invented content only.
- **Guard is biased to over-trigger.** Generous, bilingual (FR + EN) keyword detection. A guard that misses a signal causes a wrong archive; a guard that over-trips only keeps a promo visible.
- **Suffix matching is label-boundary anchored.** Never raw substring containment.

**Spec:** `docs/superpowers/specs/2026-06-24-stu-396-universal-deterministic-rules-design.md`

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `email_classifier/stage1/guards.py` | Named guard sets (keyword corpus) + `is_satisfied` | **Create** |
| `email_classifier/stage1/rule_engine.py` | Condition matching + load/validate | **Modify** |
| `rules/rules.yaml` | The two universal rules | **Modify** |
| `fixtures/bucket_c/generic_oneclick_promo.json` | One-Click happy-path fixture | **Create** |
| `fixtures/bucket_c/klaviyo_promo.json` | Bulk-sender happy-path fixture | **Create** |
| `fixtures/traps/oneclick_security_trap.json` | One-Click guard trap | **Create** |
| `fixtures/traps/customerio_security_trap.json` | Bulk-sender guard trap (canonical Supabase case) | **Create** |
| `tests/test_rule_engine.py` | Unit tests for new condition keys | **Modify** |
| `tests/test_guards.py` | Unit tests for the guard set | **Create** |
| `tests/test_universal_rules.py` | Integration tests for the two rules on fixtures | **Create** |

Run all tests with `pytest` (from repo root, venv active). Existing tests must stay green throughout.

---

### Task 1: `header_value` condition key

Matches when a named header is present **and** its value contains a substring (case-insensitive). This is the strict One-Click detector: `List-Unsubscribe-Post` value contains `One-Click`.

**Files:**
- Modify: `email_classifier/stage1/rule_engine.py`
- Test: `tests/test_rule_engine.py`

**Interfaces:**
- Consumes: existing `Rule`, `apply`, `_matches`, `_KNOWN_CONDITION_KEYS`.
- Produces: condition key `header_value` with YAML shape `{header_name: substring}` (a dict). Other tasks rely on this key being accepted by `load_rules` and matched by `apply`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_engine.py`:

```python
def test_header_value_matches_substring():
    rules = _rules(
        Rule(id="oneclick", conditions={"header_value": {"List-Unsubscribe-Post": "One-Click"}},
             bucket="C", confidence="high", action="label_archive"),
    )
    email = _make_email(headers={"List-Unsubscribe-Post": "List-Unsubscribe=One-Click"})
    assert apply(email, rules).rule_id == "oneclick"


def test_header_value_no_match_when_header_absent():
    rules = _rules(
        Rule(id="oneclick", conditions={"header_value": {"List-Unsubscribe-Post": "One-Click"}},
             bucket="C", confidence="high", action="label_archive"),
    )
    assert apply(_make_email(headers={}), rules).rule_id == "catch_all"


def test_header_value_no_match_when_value_differs():
    rules = _rules(
        Rule(id="oneclick", conditions={"header_value": {"List-Unsubscribe-Post": "One-Click"}},
             bucket="C", confidence="high", action="label_archive"),
    )
    email = _make_email(headers={"List-Unsubscribe-Post": "List-Unsubscribe=Mailto"})
    assert apply(email, rules).rule_id == "catch_all"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_engine.py::test_header_value_matches_substring -v`
Expected: FAIL — `header_value` is an unknown condition key, `load_rules`/`_matches` does not handle it (the `Rule` is built directly here, so failure is `_matches` not handling the key → rule never matches → `catch_all`, assertion fails).

- [ ] **Step 3: Add `header_value` to known keys and matching**

In `email_classifier/stage1/rule_engine.py`, add `"header_value"` to the frozenset:

```python
_KNOWN_CONDITION_KEYS: frozenset[str] = frozenset(
    {"from_address", "from_domain", "subject_contains", "header_present", "header_value"}
)
```

In `_matches`, add a branch (after the `header_present` branch):

```python
        elif key == "header_value":
            for header_name, needle in value.items():
                if needle.lower() not in email.headers.get(header_name, "").lower():
                    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_engine.py -k header_value -v`
Expected: 3 PASS.

- [ ] **Step 5: Run full suite (no regressions)**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add email_classifier/stage1/rule_engine.py tests/test_rule_engine.py
git commit -m "feat(STU-396): header_value condition key (substring match on header value)"
```

---

### Task 2: `from_domain_suffix` condition key (label-boundary anchored)

Matches when the sender domain equals a suffix or ends at a domain-label boundary of it. Accepts a single string or a list of suffixes.

**Files:**
- Modify: `email_classifier/stage1/rule_engine.py`
- Test: `tests/test_rule_engine.py`

**Interfaces:**
- Consumes: existing `_extract_domain`, `_matches`, `_KNOWN_CONDITION_KEYS`.
- Produces: helper `_domain_matches_suffix(domain: str, suffix: str) -> bool`; condition key `from_domain_suffix` accepting `str | list[str]`. Task 5 relies on the list form.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rule_engine.py`:

```python
def test_domain_matches_suffix_boundary():
    from email_classifier.stage1.rule_engine import _domain_matches_suffix
    assert _domain_matches_suffix("e.godaddy.com", "godaddy.com") is True
    assert _domain_matches_suffix("godaddy.com", "godaddy.com") is True
    assert _domain_matches_suffix("notgodaddy.com", "godaddy.com") is False
    assert _domain_matches_suffix("godaddy.com.evil.com", "godaddy.com") is False


def test_from_domain_suffix_list_matches():
    rules = _rules(
        Rule(id="bulk", conditions={"from_domain_suffix": ["kmail-lists.com", "customer.io"]},
             bucket="C", confidence="high", action="label_archive"),
    )
    assert apply(_make_email(from_address="x@email.kmail-lists.com"), rules).rule_id == "bulk"
    assert apply(_make_email(from_address="x@e.customer.io"), rules).rule_id == "bulk"
    assert apply(_make_email(from_address="x@other.com"), rules).rule_id == "catch_all"


def test_from_domain_suffix_string_value():
    rules = _rules(
        Rule(id="bulk", conditions={"from_domain_suffix": "kmail-lists.com"},
             bucket="C", confidence="high", action="label_archive"),
    )
    assert apply(_make_email(from_address="x@kmail-lists.com"), rules).rule_id == "bulk"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_rule_engine.py -k suffix -v`
Expected: FAIL — `_domain_matches_suffix` not defined / key not matched.

- [ ] **Step 3: Add the helper, known key, and matching branch**

In `email_classifier/stage1/rule_engine.py`, add the suffix matcher next to `_extract_domain`:

```python
def _domain_matches_suffix(domain: str, suffix: str) -> bool:
    domain, suffix = domain.lower(), suffix.lower()
    return domain == suffix or domain.endswith("." + suffix)
```

Add `"from_domain_suffix"` to `_KNOWN_CONDITION_KEYS`:

```python
_KNOWN_CONDITION_KEYS: frozenset[str] = frozenset(
    {"from_address", "from_domain", "subject_contains", "header_present",
     "header_value", "from_domain_suffix"}
)
```

In `_matches`, add a branch (after the `from_domain` branch):

```python
        elif key == "from_domain_suffix":
            suffixes = [value] if isinstance(value, str) else value
            domain = _extract_domain(email.from_address)
            if not any(_domain_matches_suffix(domain, s) for s in suffixes):
                return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_rule_engine.py -k suffix -v`
Expected: 3 PASS.

- [ ] **Step 5: Run full suite**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add email_classifier/stage1/rule_engine.py tests/test_rule_engine.py
git commit -m "feat(STU-396): from_domain_suffix condition key, label-boundary anchored"
```

---

### Task 3: Guard set module + `guard` condition key

Create the named guard corpus and wire `guard: not_transactional` into the engine, with fail-loud validation of unknown guard names.

> **Ariane owns the corpus.** The keyword lists below are a working starter set — concrete enough that the tests pass and the rule is safe. During/after this task, expand each category with the FR/EN words *you* know appear in the mailbox's important mail. The structure (six categories, `is_satisfied` semantics) is fixed; the word-lists are yours to grow. Bias generous: when unsure whether a word signals "this matters," include it.

**Files:**
- Create: `email_classifier/stage1/guards.py`
- Modify: `email_classifier/stage1/rule_engine.py`
- Test: `tests/test_guards.py`, `tests/test_rule_engine.py`

**Interfaces:**
- Consumes: `EmailInput` from `email_classifier.core.types`.
- Produces:
  - `GuardSet` dataclass with `name` and six `frozenset[str]` category fields (`money`, `order`, `deadline`, `security`, `appointment`, `human`) and `all_keywords() -> frozenset[str]`.
  - `GUARDS: dict[str, GuardSet]` registry keyed by guard name.
  - `is_satisfied(guard_name: str, email: EmailInput) -> bool` — `True` iff no keyword appears in lowercased `subject` + `body_snippet`.
  - condition key `guard` (string) consumed by `_matches`; unknown guard name raises `ValueError` in `load_rules`.

- [ ] **Step 1: Write the failing guard-unit tests**

Create `tests/test_guards.py`:

```python
from email_classifier.core.types import EmailInput
from email_classifier.stage1.guards import is_satisfied, GUARDS, NOT_TRANSACTIONAL


def _email(subject: str = "", body: str = "") -> EmailInput:
    return EmailInput(
        message_id="m", thread_id="t", from_address="x@y.com",
        subject=subject, headers={}, body_snippet=body,
        date="2026-06-10T00:00:00+00:00",
    )


def test_registry_contains_not_transactional():
    assert GUARDS["not_transactional"] is NOT_TRANSACTIONAL


def test_guard_satisfied_when_no_signal():
    email = _email(subject="Summer sale — new arrivals", body="Browse our new collection today")
    assert is_satisfied("not_transactional", email) is True


def test_guard_trips_on_money_fr():
    email = _email(subject="Votre facture", body="Montant total à payer")
    assert is_satisfied("not_transactional", email) is False


def test_guard_trips_on_security_en():
    email = _email(subject="New sign-in detected", body="Secure your account now")
    assert is_satisfied("not_transactional", email) is False


def test_guard_scans_body_not_just_subject():
    email = _email(subject="Hello", body="Your order has shipped")
    assert is_satisfied("not_transactional", email) is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/test_guards.py -v`
Expected: FAIL — `email_classifier.stage1.guards` does not exist (ImportError).

- [ ] **Step 3: Create the guard module**

Create `email_classifier/stage1/guards.py`:

```python
from dataclasses import dataclass

from email_classifier.core.types import EmailInput


@dataclass(frozen=True)
class GuardSet:
    """A named set of contradicting-signal keywords.

    The guard is a presence detector biased to OVER-trigger: a missed signal
    would let a transactional/security/human email be wrongly archived, so
    detection is deliberately generous and bilingual (FR + EN). A false trip
    only keeps a promo visible — the acceptable failure direction.
    """

    name: str
    money: frozenset[str]
    order: frozenset[str]
    deadline: frozenset[str]
    security: frozenset[str]
    appointment: frozenset[str]
    human: frozenset[str]

    def all_keywords(self) -> frozenset[str]:
        return (
            self.money | self.order | self.deadline
            | self.security | self.appointment | self.human
        )


# Starter corpus — generous, bilingual. Grow the word-lists with real signals
# from the mailbox it serves; keep the six-category structure.
NOT_TRANSACTIONAL = GuardSet(
    name="not_transactional",
    money=frozenset({
        "$", " cad", "dollars", "montant", "total", "solde", "prix",
        "facture", "invoice", "paiement", "payment", "remboursement", "refund",
    }),
    order=frozenset({
        "commande", "order", "suivi", "tracking", "expédié", "shipped",
        "livraison", "delivery", "colis",
    }),
    deadline=frozenset({
        "échéance", "due", "expire", "deadline", "dernier délai", "avant le",
    }),
    security=frozenset({
        "sécurité", "security", "mot de passe", "password", "connexion",
        "sign-in", "sign in", "vérification", "verify", "compte", "account",
    }),
    appointment=frozenset({
        "rendez-vous", "rdv", "appointment", "réservation", "booking",
    }),
    human=frozenset({
        "peux-tu", "can you", "merci de", "réponds", "reply please",
    }),
)

GUARDS: dict[str, GuardSet] = {NOT_TRANSACTIONAL.name: NOT_TRANSACTIONAL}


def is_satisfied(guard_name: str, email: EmailInput) -> bool:
    """True when NO contradicting signal appears in subject + body_snippet.

    Satisfied=True means a universal rule is ALLOWED to archive. Any keyword
    hit → False → the email stays visible (falls through to LLM escalation).
    """
    guard = GUARDS[guard_name]
    haystack = f"{email.subject}\n{email.body_snippet}".lower()
    return not any(keyword in haystack for keyword in guard.all_keywords())
```

> Note on `money`: `" cad"` and `"$"` are matched as substrings; the leading
> space on `" cad"` avoids tripping on words like "cadeau"/"cadence". Tune as
> you expand the corpus.

- [ ] **Step 4: Run guard-unit tests to verify they pass**

Run: `pytest tests/test_guards.py -v`
Expected: 5 PASS.

- [ ] **Step 5: Write the failing engine-wiring tests**

Append to `tests/test_rule_engine.py`:

```python
def test_guard_condition_keeps_email_visible_when_tripped():
    rules = _rules(
        Rule(id="uni", conditions={"header_present": "List-Unsubscribe", "guard": "not_transactional"},
             bucket="C", confidence="high", action="label_archive"),
    )
    # guard trips on "invoice" → falls through to catch_all
    tripped = _make_email(subject="Your invoice is ready", headers={"List-Unsubscribe": "<x>"})
    assert apply(tripped, rules).rule_id == "catch_all"
    # guard satisfied → rule matches
    clean = _make_email(subject="Weekly newsletter", headers={"List-Unsubscribe": "<x>"})
    assert apply(clean, rules).rule_id == "uni"


def test_load_rules_raises_on_unknown_guard(tmp_path: Path):
    yaml_content = """
rules:
  - id: bad
    conditions:
      guard: not_a_real_guard
    bucket: C
    confidence: high
    action: label_archive
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="unknown guard"):
        load_rules(rules_file)
```

Note: `_make_email`'s default `body_snippet="Test body"` contains no guard keyword, so the "clean" case stays satisfied.

- [ ] **Step 6: Run to verify failure**

Run: `pytest tests/test_rule_engine.py -k guard -v`
Expected: FAIL — `guard` not a known key / not matched / not validated.

- [ ] **Step 7: Wire `guard` into the engine**

In `email_classifier/stage1/rule_engine.py`:

Add the import near the top:

```python
from email_classifier.stage1.guards import GUARDS, is_satisfied
```

Add `"guard"` to `_KNOWN_CONDITION_KEYS`:

```python
_KNOWN_CONDITION_KEYS: frozenset[str] = frozenset(
    {"from_address", "from_domain", "subject_contains", "header_present",
     "header_value", "from_domain_suffix", "guard"}
)
```

In `_matches`, add a branch:

```python
        elif key == "guard":
            if not is_satisfied(value, email):
                return False
```

In `load_rules`, after the unknown-condition-key validation loop (before the catch-all check), add unknown-guard validation:

```python
    for rule in rules:
        guard_name = rule.conditions.get("guard")
        if guard_name is not None and guard_name not in GUARDS:
            raise ValueError(
                f"Rule '{rule.id}' references unknown guard '{guard_name}'. "
                f"Known guards: {sorted(GUARDS)}"
            )
```

- [ ] **Step 8: Run to verify pass**

Run: `pytest tests/test_rule_engine.py -k guard -v && pytest tests/test_guards.py -q`
Expected: all PASS.

- [ ] **Step 9: Run full suite**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 10: Commit**

```bash
git add email_classifier/stage1/guards.py email_classifier/stage1/rule_engine.py tests/test_guards.py tests/test_rule_engine.py
git commit -m "feat(STU-396): not_transactional guard set + guard condition key"
```

---

### Task 4: `universal_oneclick_archive` rule + One-Click fixtures

Add the strict One-Click rule and prove it both archives a legitimate promo and is held back by the guard on a security trap.

**Files:**
- Modify: `rules/rules.yaml`
- Create: `fixtures/bucket_c/generic_oneclick_promo.json`, `fixtures/traps/oneclick_security_trap.json`
- Test: `tests/test_universal_rules.py`

**Interfaces:**
- Consumes: `header_value` (Task 1), `guard` (Task 3), `load_rules`, `apply`, `core.schema.load`.
- Produces: rule id `universal_oneclick_archive` (bucket C, `label_archive`), placed after all merchant rules and immediately before `catch_all`.

- [ ] **Step 1: Create the two fixtures**

Create `fixtures/bucket_c/generic_oneclick_promo.json` (legitimate archive — no merchant rule, no transactional content, real One-Click headers):

```json
{
  "message_id": "msg_oneclick_promo_001",
  "thread_id": "thread_oneclick_promo_001",
  "from_address": "deals@news.example-shop.com",
  "subject": "Summer sale — 40% off our whole spring lineup",
  "headers": {
    "List-Unsubscribe": "<https://example-shop.com/unsub>, <mailto:unsub@example-shop.com>",
    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"
  },
  "body_snippet": "Our biggest seasonal event is here. Browse hundreds of new styles and save on every category.",
  "date": "2026-06-09T13:05:00+00:00",
  "provider_category": "promotions"
}
```

Create `fixtures/traps/oneclick_security_trap.json` (One-Click header present BUT security content → must stay visible):

```json
{
  "message_id": "msg_oneclick_trap_001",
  "thread_id": "thread_oneclick_trap_001",
  "from_address": "security@notifications.example-saas.com",
  "subject": "Security alert: new sign-in to your account",
  "headers": {
    "List-Unsubscribe": "<https://example-saas.com/unsub>",
    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"
  },
  "body_snippet": "We detected a new sign-in to your account from an unrecognized device. If this was not you, secure your account immediately.",
  "date": "2026-06-12T08:15:00+00:00",
  "provider_category": "updates"
}
```

(`security`/`sign in`/`account` all trip the guard.)

- [ ] **Step 2: Write the failing integration tests**

Create `tests/test_universal_rules.py`:

```python
from email_classifier.core.schema import load as load_fixture
from email_classifier.stage1.rule_engine import load_rules, apply
from tests.conftest import RULES_DIR, FIXTURES_DIR


def test_oneclick_promo_archived_deterministically():
    rules = load_rules(RULES_DIR / "rules.yaml")
    email = load_fixture(FIXTURES_DIR / "bucket_c" / "generic_oneclick_promo.json")
    match = apply(email, rules)
    assert match.rule_id == "universal_oneclick_archive"
    assert match.action == "label_archive"
    assert match.bucket == "C"


def test_oneclick_security_trap_stays_visible():
    rules = load_rules(RULES_DIR / "rules.yaml")
    email = load_fixture(FIXTURES_DIR / "traps" / "oneclick_security_trap.json")
    match = apply(email, rules)
    # One-Click trigger present, but guard trips on security content → not archived
    assert match.rule_id == "catch_all"
    assert match.action == "escalate_to_llm"
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_universal_rules.py -v`
Expected: `test_oneclick_promo_archived_deterministically` FAILS (matches `catch_all`, not the not-yet-existing rule). `test_oneclick_security_trap_stays_visible` may already pass (no rule yet → catch_all) — that is fine; it must stay passing after the rule is added.

- [ ] **Step 4: Add the rule to `rules/rules.yaml`**

Insert immediately **before** the `catch_all` rule (after the Bucket D section):

```yaml
  # ── Règles universelles — signaux structurels, après marchand, avant LLM ──
  - id: universal_oneclick_archive
    conditions:
      header_value:
        List-Unsubscribe-Post: One-Click   # RFC 8058 strict One-Click
      guard: not_transactional              # second safety barrier
    bucket: C
    confidence: high
    action: label_archive
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_universal_rules.py -v`
Expected: both PASS.

- [ ] **Step 6: Run full suite (the existing `test_all_fixtures_match_expected_bucket` now also covers the new bucket_c fixture)**

Run: `pytest -q`
Expected: all green. (The new `bucket_c/generic_oneclick_promo.json` resolves to bucket C via the new rule, satisfying the existing fixture-bucket test.)

- [ ] **Step 7: Commit**

```bash
git add rules/rules.yaml fixtures/bucket_c/generic_oneclick_promo.json fixtures/traps/oneclick_security_trap.json tests/test_universal_rules.py
git commit -m "feat(STU-396): universal_oneclick_archive rule + One-Click fixtures (promo + security trap)"
```

---

### Task 5: `universal_bulk_sender_archive` rule + bulk-sender fixtures

Add the bulk-sender platform rule and prove it archives a Klaviyo promo (no One-Click header) while the guard holds back the canonical Customer.io/Supabase security alert.

**Files:**
- Modify: `rules/rules.yaml`
- Create: `fixtures/bucket_c/klaviyo_promo.json`, `fixtures/traps/customerio_security_trap.json`
- Test: `tests/test_universal_rules.py`

**Interfaces:**
- Consumes: `from_domain_suffix` (Task 2), `guard` (Task 3), `universal_oneclick_archive` ordering (Task 4).
- Produces: rule id `universal_bulk_sender_archive` (bucket C, `label_archive`), placed after `universal_oneclick_archive` and immediately before `catch_all`.

- [ ] **Step 1: Create the two fixtures**

Create `fixtures/bucket_c/klaviyo_promo.json` (bulk-sender, no One-Click header — proves the suffix rule catches what the strict rule would not):

```json
{
  "message_id": "msg_klaviyo_promo_001",
  "thread_id": "thread_klaviyo_promo_001",
  "from_address": "hello@email.kmail-lists.com",
  "subject": "New arrivals just dropped",
  "headers": {
    "List-Unsubscribe": "<https://example-pets.ca/unsub>"
  },
  "body_snippet": "Fresh seasonal styles for your pup are here. Browse the new collection and treat your best friend.",
  "date": "2026-06-09T13:05:00+00:00",
  "provider_category": "promotions"
}
```

Create `fixtures/traps/customerio_security_trap.json` (sent via Customer.io BUT account/security content → must stay visible — the canonical case from the spec):

```json
{
  "message_id": "msg_customerio_trap_001",
  "thread_id": "thread_customerio_trap_001",
  "from_address": "noreply@e.customer.io",
  "subject": "Your project will be paused in 5 days",
  "headers": {
    "List-Unsubscribe": "<https://example-db.com/unsub>"
  },
  "body_snippet": "Your project has been inactive for 85 days. To avoid your account being paused, sign in to your dashboard.",
  "date": "2026-06-12T08:15:00+00:00",
  "provider_category": "updates"
}
```

(`account`/`sign in` trip the guard.)

- [ ] **Step 2: Write the failing integration tests**

Append to `tests/test_universal_rules.py`:

```python
def test_bulk_sender_promo_archived_deterministically():
    rules = load_rules(RULES_DIR / "rules.yaml")
    email = load_fixture(FIXTURES_DIR / "bucket_c" / "klaviyo_promo.json")
    match = apply(email, rules)
    assert match.rule_id == "universal_bulk_sender_archive"
    assert match.action == "label_archive"
    assert match.bucket == "C"


def test_customerio_security_trap_stays_visible():
    rules = load_rules(RULES_DIR / "rules.yaml")
    email = load_fixture(FIXTURES_DIR / "traps" / "customerio_security_trap.json")
    match = apply(email, rules)
    # Bulk-sender trigger present (customer.io), guard trips on account content → not archived
    assert match.rule_id == "catch_all"
    assert match.action == "escalate_to_llm"
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest tests/test_universal_rules.py -k bulk_sender_promo -v`
Expected: FAIL — `klaviyo_promo.json` resolves to `catch_all` (rule not added yet).

- [ ] **Step 4: Add the rule to `rules/rules.yaml`**

Insert immediately **after** `universal_oneclick_archive` and before `catch_all`:

```yaml
  - id: universal_bulk_sender_archive
    conditions:
      from_domain_suffix:
        - kmail-lists.com      # Klaviyo
        - customer.io          # Customer.io
        - godaddy.com          # e.godaddy.com & co (label-boundary anchored)
      guard: not_transactional
    bucket: C
    confidence: high
    action: label_archive
```

- [ ] **Step 5: Run to verify pass**

Run: `pytest tests/test_universal_rules.py -v`
Expected: all 4 PASS.

- [ ] **Step 6: Run full suite**

Run: `pytest -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add rules/rules.yaml fixtures/bucket_c/klaviyo_promo.json fixtures/traps/customerio_security_trap.json tests/test_universal_rules.py
git commit -m "feat(STU-396): universal_bulk_sender_archive rule + bulk-sender fixtures (Klaviyo promo + Customer.io trap)"
```

---

## Manual Verification (post-merge of STU-395 dedup)

Not a unit test — a run-time observation per the acceptance criteria:

- [ ] Re-run the classifier over the run `9fbf5306` corpus **after** STU-395 dedup has corrected the denominator.
- [ ] Confirm the listed cases (woofpacks/Klaviyo, Dell, Udemy, LinkedIn auto, A/MAZE, ChefsPlate, Ratehub, Fandom, GoDaddy) are now decided deterministically (`path` deterministic, `rule_id` ∈ {`universal_oneclick_archive`, `universal_bulk_sender_archive`}), not LLM.
- [ ] Confirm no security/account mail with a List-Unsubscribe (Supabase, Google privacy) was archived — they stay visible.
- [ ] Record the new `ratio_deterministic`. Target is to be *constated*, not forced. If short of 80/20, prefer adding positive merchant/platform rules over widening the guard.

---

## Self-Review

**Spec coverage:**
- One-Click rule conditioned on absence of transactional/security/human → Tasks 1, 3, 4. ✓
- Bulk-sender platforms (Klaviyo, Customer.io, marketing subdomains) → Tasks 2, 5. ✓
- Universal rules after merchant rules, before catch_all → Tasks 4 & 5 placement. ✓
- No false positive on security/account mail with List-Unsubscribe → trap fixtures, Tasks 4 & 5. ✓
- Regression fixtures (legitimate archive + security trap) → Tasks 4 & 5. ✓
- `ratio_deterministic` measured after dedup → Manual Verification section. ✓
- Named guard set (C) / `header_value` (A) / `from_domain_suffix` w/ boundary anchoring → Tasks 1–3. ✓
- Fail-loud on unknown guard → Task 3, Step 7 + test. ✓

**Placeholder scan:** The guard corpus is a concrete working starter set (tests pass against it); Ariane's expansion is an explicit owned enhancement, not a blocking placeholder. No "TBD"/"add error handling"/uncoded steps remain.

**Type consistency:** `_domain_matches_suffix(domain, suffix) -> bool`, `is_satisfied(guard_name, email) -> bool`, `GUARDS` dict, `GuardSet.all_keywords()`, rule ids `universal_oneclick_archive` / `universal_bulk_sender_archive` — used identically across tasks and tests. `conditions` values now span `str | list[str] | dict[str, str]`; every `_matches` branch handles its own shape. ✓
