# STU-396 — Universal deterministic rules (List-Unsubscribe One-Click + bulk-sender platforms)

- **Issue:** [STU-396](https://linear.app/studioag/issue/STU-396/regles-universelles-deterministes-list-unsubscribe-one-click)
- **Depends on:** STU-395 (dedup) — implement rules now, measure ratio after dedup lands
- **Date:** 2026-06-24
- **Status:** Approved design

## Problem

Run `9fbf5306` shows `ratio_deterministic: 0.5` (25 deterministic / 25 LLM), far from the 80/20 target. A large share of the LLM-escalated mail (`path: "llm"`, `rule_id: "catch_all"`) is newsletters/promos carrying a strong, unambiguous universal signal that Stage 1 should have decided without escalation. Each such email is needless LLM cost and PII surface.

The dominant missing signal is **`List-Unsubscribe` with One-Click** (RFC 8058). Corpus examples that all land in bucket C / `label_archive` via the LLM: woofpacks.ca via Klaviyo, Dell, Udemy, LinkedIn auto content/alerts, A/MAZE, ChefsPlate, Ratehub, Fandom, GoDaddy re-engagement. Shared signature: merchant sender + One-Click List-Unsubscribe + promotional content + zero transactional value.

**Guard-rail (non-negotiable):** universal rules must fire only on **unambiguous** signal. A `List-Unsubscribe` on a security/account notification (Supabase via Customer.io, Google privacy) must **not** archive — transactional/security/human content overrides the header's presence. Under doubt, default to visible.

## Design principles

1. **Precision over ratio.** The cost structure is asymmetric: a false negative (a promo stays visible) is minor and recoverable — Stage 2 LLM catches it. A false positive (a transactional/security/parent email wrongly archived) is a missed invoice/deadline/human message. Optimize ratio only insofar as it cuts LLM volume *without* degrading classification. A reliable 0.65 beats an 0.80 that sometimes archives a parent email.
2. **Defense in depth — two independent barriers.** The universal archive rule fires on (a) a *strong positive trigger* (strict One-Click, or a known bulk-send platform) **and** (b) a *guard* confirming the absence of contradicting signals. The trigger and the guard are independent layers; robustness comes from their independence. Resting all safety on the guard alone (a fallible keyword corpus) is rejected.
3. **Paradigm uniformity.** Every classification rule remains declarative YAML, readable one way. "Why was this archived?" always has the same answer-shape: read the rule. New tokens (`header_value`, `from_domain_suffix`, `guard`) are *named references* you look up, the same as `from_domain`.
4. **Declarative mechanism, encapsulated named knowledge.** Rule structure lives in YAML; large/reusable knowledge (the guard keyword corpus) lives in one named Python place referenced by name. Small/rule-specific knowledge (the bulk-platform list) stays inline in YAML where it is most transparent.

## Decisions (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Negative guard expression | **Named guard set** (YAML references `guard: not_transactional`; corpus defined once in Python) | The "no contradicting signal" condition is a whole bilingual corpus, not one or two `not_contains`. Pure-YAML would be a 50-condition negative monster, duplicated by any future aggressive-archive rule. A separate Python rules layer would fragment the model into two paradigms. The guard set keeps structure declarative and knowledge encapsulated/reusable. |
| One-Click detection | **Strict** — require `List-Unsubscribe-Post: List-Unsubscribe=One-Click` via new `header_value` key | Asymmetric cost: strict trigger keeps the guard as a *second* safety, not the only one. Bare-`List-Unsubscribe` (much transactional mail carries it) would put all load on the fallible guard. Senders with only a bare header fall to the bulk-sender rule or the LLM — not misclassified, just not decided by this rule. |
| Bulk-sender platforms | **In scope**, positive matching (same shape as a merchant rule) | Adds no new engine mechanic; lifts ratio on a reliable signal. |
| Bulk-sender guarding | **Uniformly guarded**, no "purely marketing" carve-outs | You cannot reliably know a platform is purely marketing. Customer.io carried a critical Supabase account alert in our own runs; an unconditional Customer.io rule would have wrongly archived it. Uniform guard is the barrier that protects that case. |
| Domain matching for platforms | New **`from_domain_suffix`** key, **label-boundary anchored** | `kmail-lists.com` matches exact, but `e.godaddy.com` / `email.*` are sending-subdomain conventions exact-match misses. Anchor on label boundary to avoid the classic suffix bypass (`notgodaddy.com`, `godaddy.com.evil.com`). |
| Two-tier signal strength (strong One-Click + weak bare header) | **Deferred** | Introduces signal-strength + combination logic the engine lacks; over-engineered for a first cut. Add later only if A is observed to leave too many bulk senders to the LLM. If ratio is short, prefer adding positive merchant/platform rules over a wider negative net. |
| Guard scan surface | `subject` + `body_snippet` (lowercased) | Transactional/security/human language lives in human-readable content. Headers are infrastructure — no keyword-exploitable semantic signal. |
| Guard categories | **All six** in v1: money, order, deadline, security/account, appointment, human | The guard is protective; every omitted category is a class of important email that could be wrongly archived. Lean to over-protection. |
| Guard language | **Bilingual** FR + EN | Real traffic mixes francophone QC mail and English SaaS mail. |
| Guard matching | **Keyword-substring, generous**; regex only per-category if genuinely needed (money likely doesn't need it with a good monetary-keyword set) | The dangerous failure is a guard *false negative* (misses a signal → transactional email wrongly archived). So the guard must be generous: under doubt it should believe it sees a contradicting signal and keep the email visible. A false guard-trip only costs "a promo stays visible." |

## Architecture

### 1. New condition vocabulary — `email_classifier/stage1/rule_engine.py`

Add to `_KNOWN_CONDITION_KEYS`: `header_value`, `from_domain_suffix`, `guard`.

| Key | Matches when | YAML shape |
|---|---|---|
| `header_value` | header key present **and** its value contains the substring | nested map `{header_name: substring}` |
| `from_domain_suffix` | sender domain `== suffix` or `endswith("." + suffix)` | string or list of strings |
| `guard` | named guard set is **satisfied** (no contradicting signal) | string (guard name) |

- `header_value` uses substring-on-value (`"One-Click" in headers[name]`) — robust to whitespace/casing.
- `guard` is the only negative condition but composes into existing AND / first-match-wins semantics: guard not satisfied → `_matches` returns `False` → email falls to the next rule.
- `conditions` type widens from `dict[str, str]` to `dict[str, str | list[str] | dict[str, str]]`.
- Validation: an unknown `guard` name raises at `load_rules()` time (fail-loud, consistent with the existing unknown-condition-key check).

Suffix matcher (the security boundary):

```python
def _domain_matches_suffix(domain: str, suffix: str) -> bool:
    domain, suffix = domain.lower(), suffix.lower()
    return domain == suffix or domain.endswith("." + suffix)
```

`e.godaddy.com` → matches `.godaddy.com` ✓ · `notgodaddy.com` → ✗ · `godaddy.com.evil.com` → ✗.

### 2. The guard set — `email_classifier/stage1/guards.py` (new)

- A `GuardSet` holding six named keyword categories (money, order, deadline, security, appointment, human), bilingual, substring keywords.
- `GUARDS = {"not_transactional": NOT_TRANSACTIONAL}` registry, resolved by name from YAML.
- `is_satisfied(guard_name, email) -> bool`: scans lowercased `subject` + `body_snippet`; returns `True` (rule allowed to archive) only when **no** keyword from any category appears. Any hit → `False` → email stays visible.
- Reusable: any future aggressive-archive rule references `guard: not_transactional` without redefining the corpus.
- The exact keyword corpus is owned/refined by Ariane during implementation (domain knowledge of the mailbox). The structure is fixed here; the word lists are filled in then.

### 3. Universal rules — `rules/rules.yaml`

Placed in a new section **after** bucket D merchant rules and **immediately before** `catch_all`:

```yaml
  # ── Règles universelles — signaux structurels, après marchand, avant LLM ──
  - id: universal_oneclick_archive
    conditions:
      header_value:
        List-Unsubscribe-Post: One-Click   # RFC 8058 strict One-Click
      guard: not_transactional              # second safety layer
    bucket: C
    confidence: high
    action: label_archive

  - id: universal_bulk_sender_archive
    conditions:
      from_domain_suffix:
        - kmail-lists.com      # Klaviyo
        - customer.io
        - godaddy.com          # e.godaddy.com & co (label-boundary anchored)
      guard: not_transactional
    bucket: C
    confidence: high
    action: label_archive
```

Platform list is a starting set, refined during implementation. It stays inline (small, rule-specific, transparent where used) — deliberately asymmetric with the guard corpus (large, reused → Python).

### 4. Ordering & flow

First-match-wins unchanged. Ordering invariant:

```
A/B merchant rules            ← brand-known, highest precedence
C/D merchant rules
universal_oneclick_archive    ┐ fire only on strong signal
universal_bulk_sender_archive ┘ AND guard-satisfied
catch_all → escalate_to_llm   ← everything else, incl. guard-tripped mail
```

A security email with a merchant rule (e.g. `github_security`) never reaches the universal rules — caught earlier as bucket A. A security email *without* a merchant rule (Supabase/Customer.io) reaches `universal_bulk_sender_archive`; the guard trips on `account`/`compte`/`security`; the rule skips; it falls to `catch_all → escalate_to_llm` — **not archived by Stage 1**, as required.

## Testing

1. **Legitimate archive fixture** — `fixtures/bucket_c/` newsletter with `List-Unsubscribe-Post: One-Click`, no transactional content → matched rule = `universal_oneclick_archive`, `action = label_archive`.
2. **The trap fixture (canonical)** — `List-Unsubscribe-Post: One-Click` **and** security/account content, from a domain with **no merchant rule** (Supabase-via-Customer.io shape) → the One-Click *trigger is present* but the email is **not** archived: matched rule = `catch_all`, `action = escalate_to_llm`. Isolates the guard as the protector.
3. **Bulk-sender happy-path fixture** — Klaviyo newsletter (`*.kmail-lists.com`), no transactional content → `universal_bulk_sender_archive`.
4. **Unit tests** — `is_satisfied` per category, FR + EN each trips it; `_domain_matches_suffix` for the three boundary cases; `load_rules()` raises on unknown guard name.

`ratio_deterministic` (acceptance criterion) is a **run-time observation** measured after STU-395 dedup lands, not a unit-test assertion. Documented as a manual verification step. Target is to be *constated*, not forced — if it falls short, add positive merchant/platform rules rather than widening the negative net.

## Branching

STU-396 stacks on the STU-395 work present on the current branch (`arianedguay/stu-395-...`, not yet merged), so the dedup fix is present and the ratio stays measurable per the ticket's sequencing note. New branch `arianedguay/stu-396-regles-universelles-deterministes-list-unsubscribe-one-click` off the current branch.

## Out of scope

- Two-tier weak/strong signal logic (deferred — add only if observed necessary).
- Regex extraction of amounts/order numbers (keyword presence detection suffices; regex only per-category if a category genuinely cannot be expressed as keywords).
- Forcing the ratio to 80/20.
