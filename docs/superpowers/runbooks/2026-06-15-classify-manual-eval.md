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
