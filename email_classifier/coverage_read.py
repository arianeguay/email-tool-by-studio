"""Drive `draft-coverage` on one shipped body, and read the verdict back.

Extracted from the measurement script (STU-1140) when a second caller appeared
(STU-1151): `make qa-coverage` sweeps a finished run's journal, `make qa-coverage-gate`
replays the committed corpus of reads the reader must keep returning. Both ask the
pipeline the same question, so both ask it through here — the reason `qa_replay.py`
exists for the reviewer, and the same one: two callers reading a verdict two different
ways would be measuring two different things.

Live. Nothing is drafted and no mailbox is written — the reader has no tools — but the
pipeline runs, so this needs a provider.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from email_classifier import qa_coverage

PIPELINE = "draft-coverage"
STAGE = "draft-coverage"
MODEL_ENV = "COVERAGE_MODEL"
TIMEOUT = 300

# `--provider anthropic` by default, the way `draft_bakeoff.py` runs, and for the reason
# the failure here made concrete: under `defaults.provider: claude-code` every read is a
# fresh CLI process, and back-to-back reads returned the session limit as the agent's
# *prose* — RALPH reads a string where the contract wants an object, and the read records
# `error` (STU-1016). Measured 2026-08-30 on Studio 0.17.0: 3 of 4 reads unreadable that
# way, 4 of 4 readable on the API. Two standing reasons point the same way — that CLI
# builds its system prompt from the cwd, so before 0.17.1 each read carries this repo's
# `CLAUDE.md` into the prompt and the bill (STU-1013, STU-1115); and each read being a
# one-shot process is exactly the shape STU-1133 measured caching at ratio 0.0, which is
# what the agent's `prompt_cache: "on"` would otherwise buy nothing for.
DEFAULT_PROVIDER = "anthropic"


def output_of(stdout: str, stage_name: str = STAGE) -> dict:
    """The reader's output in a run record, or `{}` when it did not return one."""
    try:
        record = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    for stage in record.get("stages") or []:
        if isinstance(stage, dict) and stage.get("stage_name") == stage_name:
            output = stage.get("output")
            if isinstance(output, dict):
                return output
    return {}


def read(group: qa_coverage.Group, workdir: Path, run_id: str, repeat: int, label: str,
         cwd: Path, provider: str | None = DEFAULT_PROVIDER) -> qa_coverage.Read:
    """One coverage call: the sender's message and the shipped body, nothing else.

    Deliberately not the fan-out item. That carries the thread history, the memory index
    and the club's own earlier mail, and every one of them is material the reader could
    mistake for an answer the reply gave.
    """
    item_path = workdir / "coverage.yaml"
    item_path.write_text(
        yaml.safe_dump(
            {
                "thread_id": group.thread_id,
                "body_text": group.body_text,
                "draft_body": group.final_body,
                "run_id": run_id,
            },
            sort_keys=False, allow_unicode=True,
        ),
        encoding="utf-8",
    )
    output = run_stage(PIPELINE, STAGE, item_path, cwd, provider, label)
    parsed = qa_coverage.read_of(output, repeat)
    if parsed.verdict == qa_coverage.INCOHERENT:
        # The reader answered and contradicted itself. Printed rather than only counted:
        # what it actually said is the whole of what a human can act on, and the output
        # holds only the sender's words and the reply, both already on this disk.
        print(f"  {label}: incoherent | {json.dumps(output, ensure_ascii=False)}",
              file=sys.stderr)
    return parsed


def run_stage(pipeline: str, stage: str, item_path: Path, cwd: Path,
              provider: str | None, label: str) -> dict:
    """One `studio run` of a one-stage reader, and that stage's output or `{}`.

    Shared with `fact_read` (STU-1998): one way to call a reader, not two.
    """
    command = ["studio", "run", pipeline, "--input-file", str(item_path), "--json"]
    if provider:
        command += ["--provider", provider]
    try:
        result = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        print(f"  {label}: TIMEOUT after {TIMEOUT}s", file=sys.stderr)
        return {}
    output = output_of(result.stdout, stage)
    if not output:
        # stderr only: stdout is the run record and it echoes the input back (STU-1010).
        print(f"  {label}: no output | rc={result.returncode}", file=sys.stderr)
    return output
