# Reference deployment: École de musique Cantabile

A complete, fictional deployment of this kernel. **École de musique Cantabile** is an
invented community music school, not a real organization, and nothing here is real mail.

It has two jobs:

- **It is the test instance.** `tests/conftest.py` points `$EMAIL_CLASSIFIER_DEPLOYMENT_ROOT`
  at this directory, so the whole suite reads its taxonomy, rules, policy, agents,
  fixtures and frozen records from here.
- **It is the template.** A new deployment starts as a copy of this directory
  (`cp -r reference-deployment/ my-deployment/`) and replaces the content, never the shape.

The kernel has no fallback: a deployment that lacks a taxonomy or a policy stops the run
instead of being classified under this one's buckets. Nothing in `email_classifier/` or
`scripts/` reads this directory unless the deployment root or a `--deployment-root` flag
names it.

## Layout

| Path | What it is |
| -- | -- |
| `rules/policy.yaml` | What may run unattended, the anonymization scope (`full`), `language: fr_ca`, `timezone`, `vouched_skill: org-facts` |
| `rules/taxonomy.yaml` | This deployment's buckets and subtypes, on the kernel's action vocabulary |
| `rules/rules.yaml` | A handful of deterministic stage-1 rules, enough to exercise the rule engine |
| `rules/web_sources.yaml` | One public page the drafter's site search may read, with no extractor |
| `.studio/agents/` | The four agents: `classifier`, `drafter`, `draft-reviewer`, `coverage-reader` |
| `.studio/skills/` | `taxonomy`, `org-facts` (what the school publishes) and `org-voice` (how it writes) |
| `.studio/config.yaml` | Provider defaults (`claude-code`, no API key needed) |
| `.studio/contracts`, `pipelines`, `tools`, `invariants.md`, `mock.yaml` | Symlinks to the kernel's own `.studio/` |
| `scripts` | Symlink to the kernel's `scripts/`, for Studio script stages that resolve from the deployment |
| `fixtures/` | 13 synthetic emails, one directory per bucket (`bucket_a` to `bucket_d`) |
| `eval/golden.json` | Stage-1 replay labels for every fixture |
| `eval/stage2-golden.json` | Hand-reviewed LLM verdicts for the 10 fixtures stage 1 escalates |
| `eval/draft-qa-rejections.json` | Two drafts the reviewer must keep rejecting |
| `eval/draft-qa-gate.json` | The record of the last reviewer pass over them, digest-stamped |
| `eval/draft-coverage-cases.json` | A defect and a control the coverage reader must keep telling apart |
| `eval/draft-coverage-gate.json` | The record of the last coverage-reader pass over them |

The Studio pieces under `.studio/` are links rather than copies so this deployment and
the kernel cannot drift apart: editing a contract edits it for both. `agents/` and
`skills/` are real files, because they are where a deployment's own judgement lives.

The `org-facts` and `org-voice` skills are fingerprinted sources of the two `*-gate.json`
records. Editing either one turns the matching `--check` red until the gate is rerun.

## What it deliberately does not have

- **No `rules/voice.yaml`.** The voice baselines are counts measured over a real
  sent-mail archive, and a fictional school has none. `voice_baseline.load()` returns
  `None` and every reader treats the baseline as absent, which is the honest answer;
  `org-voice.skill.md` states plain style rules instead and says they are unmeasured.
- **No archive measurements.** The detector false-positive and recall harnesses need a
  real de-identified mailbox export, and the site-freshness check needs a live
  organization site. Both belong to a real deployment, so neither ships here.
- **No pilot artifacts.** `pilot_gate.py` and `draft_bakeoff.py` read reports and run
  journals from a live mailbox; there is none for a fictional school.

## Running the harnesses against it

From the kernel's root, with an interpreter that has the package installed (the one
`make test` builds will do):

```bash
export EMAIL_CLASSIFIER_DEPLOYMENT_ROOT=$PWD/reference-deployment

# Stage 1: replay the fixtures through the rules and diff against eval/golden.json
python -m email_classifier.eval run

# The two QA gates: has the reviewer or the coverage reader changed since its record?
# Seconds, no model call.
python scripts/qa_gate.py --deployment-root reference-deployment --check
python scripts/qa_coverage_gate.py --deployment-root reference-deployment --check
```

All three exit 0 on a clean tree. Rerunning a gate for real, which calls a model and
rewrites its record, drops `--check`. The agents' tools run `${PYTHON:-.venv/bin/python}`,
so export the same interpreter first; without it every tool call fails and the gate
exits 4 without stamping:

```bash
export PYTHON=$(command -v python) STUDIO_PYTHON_BIN=$(command -v python)
python scripts/qa_gate.py --deployment-root reference-deployment
python scripts/qa_coverage_gate.py --deployment-root reference-deployment --provider claude-code
```

## Origin

The kernel was extracted from the deployment of a volleyball club's mailbox, which now
lives in its own repository. This directory was written alongside it to prove the
kernel ran on a second deployment, and shares none of that club's content.
