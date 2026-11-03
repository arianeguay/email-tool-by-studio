# email-tool-by-studio

An email classifier and reply drafter built on Studio (`@studio-foundation/cli`).
Stage 1 is a deterministic rule engine; what it cannot decide escalates to an LLM (stage 2)
through Studio pipelines, with PII tokenized before anything leaves the machine. A third
stage drafts replies for the conversations that need one, reviewed by a second agent.
Nothing is ever sent: drafts land in the mailbox for a human to read.

This repository is the **kernel**. It names no organization. A deployment supplies
everything that is about one mailbox: the taxonomy, the rules, the policy, the agents'
prose and skills, the fixtures and the frozen evaluation records.
`reference-deployment/` is a complete, fictional one (a community music school) and is
what the test suite runs against.

## Layout

| Path | What |
| -- | -- |
| `email_classifier/` | The Python package: fetch (Gmail, Microsoft Graph), stage 1, stage 2 client, anonymization, apply/undo, drafts, webhook, audit, cost, retention |
| `email_classifier/lang_packs/` | Detection vocabulary per language (`fr_ca`), see `docs/lang-packs.md` |
| `scripts/` | Studio script stages and operator commands |
| `.studio/contracts`, `.studio/pipelines`, `.studio/tools` | The kernel's Studio pipelines, output contracts and tool plugins |
| `.studio/invariants.md`, `.studio/mock.yaml` | Invariants injected into every agent; the mock provider's fixtures |
| `reference-deployment/` | A fictional deployment, and the suite's test instance |
| `tests/` | The kernel's tests |

## Pointing the kernel at a deployment

The kernel reads every deployment path (`rules/`, `.studio/agents`, `.studio/skills`,
`.studio/config.yaml`, `fixtures/`, `eval/`, `audits/`, `state/`, `logs/`, `reports/`,
`.env`) from `$EMAIL_CLASSIFIER_DEPLOYMENT_ROOT`, a process variable the deployment's
Makefile or service unit sets (not a `.env` key). Unset, it is the package's parent
directory, which only works when a deployment and the kernel share one checkout.

A deployment directory holds:

- `rules/policy.yaml`: what may run unattended, the anonymization scope, `language`
  (a lang pack), `timezone`, `vouched_skill` (the skill whose published names are never
  redacted) and `envelope_rewriters` (modules that register a reply-target rewriter).
- `rules/taxonomy.yaml`, `rules/rules.yaml`, `rules/web_sources.yaml`, and optionally
  `rules/voice.yaml` (archive-measured voice baselines).
- `.studio/agents/`, `.studio/skills/`, `.studio/config.yaml`, with `contracts`,
  `pipelines`, `tools`, `invariants.md` and `mock.yaml` linked from the kernel, as
  `reference-deployment/.studio/` does.
- `fixtures/` and `eval/` for its own goldens and frozen QA records.

There is no fallback deployment: a missing taxonomy or policy stops the run rather than
classifying one mailbox under another's buckets.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,webhook]"
pip install -e ".[ner]"   # name detection (GLiNER + torch), required once a policy sets an anonymization scope
```

## Operating it

The `Makefile` is the operational interface: runs, apply and undo, drafts, the webhook,
audits, the QA gates. Its targets read deployment paths relative to the working
directory, so a deployment runs them from its own root, typically by including this
Makefile from its own.

## Tests

```bash
make test       # the suite CI gates on: [dev,webhook], no `ner`, -n auto
make test-ner   # the same suite on the interpreter carrying the `ner` extra
```

`tests/conftest.py` points `$EMAIL_CLASSIFIER_DEPLOYMENT_ROOT` at `reference-deployment/`
unless it is already set, so a bare `pytest` runs against the same deployment as `make`.

## Test text and the first deployment

The kernel was extracted from a volleyball club's deployment, which lives in its own
repository. No test carries that club's mail. Where a test reproduces a shape the name
detector got wrong on live mail (a signature a full name suppresses, a child named beside
a parent's signature, a sentence whose phrases were minted as names), the text is
synthetic and was re-measured against the detector so it still exercises the case: see
the scores in the comment above `_SUPPRESSED` in `tests/test_anonymize.py`.

`reference-deployment/` names the first deployment in its README and skills to explain
what it mirrors; it is fictional data and shares none of that deployment's content.
