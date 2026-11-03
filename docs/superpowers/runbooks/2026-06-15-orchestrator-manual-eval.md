# Manual eval — end-to-end orchestrator (STU-392)

Covers acceptance criteria that need a real Studio + LLM (and optionally real Gmail).
Requires `ANTHROPIC_API_KEY`, the `studio` CLI on PATH, and the Studio `validators` hook
installed (see STU-390 spec §6).

The pytest suite already covers the orchestrated path with the LLM mocked (`FakeClassifier`)
and the Stage 2 client with `subprocess` mocked. This runbook covers what pytest cannot:
a real `studio run` and a real read-only fetch.

## 0. Smoke test without an LLM (deterministic-only dump)
Verifies the wiring (load → stage 1 → frontier log → report) with zero LLM calls.
Note: `studio` must still be on PATH — `main()` preflights for it before processing and
exits 1 if absent — but no `studio` process is actually spawned when nothing escalates.
1. Build a dump whose senders all match deterministic rules (e.g. `payments.interac.ca`,
   `shipment-tracking@amazon.ca`, `monpetitpret.com`). None will escalate.
2. Run:
   ```bash
   python run_email.py --dump <dump>.json
   ```
3. Confirm: human report prints with `N deterministic (100%)`, `reports/<run-id>.json` is
   written, `logs/frontier.jsonl` has one line per email, and the run exits 0.

## 1. Full run on a frozen dump (with escalation)
1. Build a small dump that includes grey-zone senders (unknown domains) so some emails
   escalate to `escalate_to_llm`.
2. Run:
   ```bash
   make run DUMP=dumps/gmail-dump.json
   # or: python run_email.py --dump dumps/gmail-dump.json
   ```
3. Confirm: a human report prints, `reports/<run-id>.json` is written, and the
   deterministic/LLM ratio is shown. The run exits 0 (or 2 if any per-email LLM error).

## 2. Frontier invariant on a real run
1. Open `logs/frontier.jsonl` for the run (run-id matches the report's `run_id`).
2. Confirm every deterministically-decided email has its `rule_id` and is NOT in the LLM
   path of the report; only `action == "escalate_to_llm"` / `rule_id == "catch_all"`
   entries correspond to `path == "llm"` verdicts.

## 3. Real read-only fetch end-to-end (personal inbox)
1. Ensure `secrets/credentials.json` exists (see STU-391). First run opens a browser once.
2. Run:
   ```bash
   make run-fetch QUERY="newer_than:7d" MAX=50
   ```
3. Audit `reports/<run-id>.json` manually: spot-check bucket/action assignments against
   the taxonomy. Confirm ZERO mailbox changes occurred (the run is read-only — there is no
   label/trash/send code path).

## 4. Economy argument
1. On a representative inbox slice, confirm `totals.deterministic` >> `totals.llm`
   (the majority resolved without a token). Record the ratio.
