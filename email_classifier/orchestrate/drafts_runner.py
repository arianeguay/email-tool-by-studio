"""Stage 2's sibling for the drafting half: run `drafts` out of process (STU-946).

The webhook classifies in-process but cannot draft there. Drafting is a Studio
fan-out with a QA group and live tool calls per mail, so the only way to reach it
is the CLI — the same `studio run drafts` that `mailbox-full` calls, handed the
same two things: a corpus and the classification report.

The corpus is a temp file, not `dumps/<run-id>.json`. A push run's mail never
touched disk and its provenance says so (STU-859); a dump named after the run id
would offer an audit a corpus the report does not claim to have read.

Output is not captured: nothing here parses it, and inheriting stdio puts the
drafting log in the service journal, which is the only place a human looks after
an unattended run.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from email_classifier.providers import HEADLESS_ENV

_PIPELINE = "drafts"

# An LLM draft plus a QA loop per pending mail, two at a time. Generous because the
# cost of cutting a run short is a mail left unanswered, where the cost of waiting is
# a worker thread draining a queue that survives a restart anyway.
_DEFAULT_TIMEOUT = 1800

_log = logging.getLogger("email_classifier.drafts")


class StudioCliDrafter:
    """Runs the `drafts` pipeline for one report. Raises on anything but success."""

    def __init__(
        self,
        *,
        provider: str | None = None,
        cwd: Path | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
    ) -> None:
        self.provider = provider
        self.cwd = cwd
        self.timeout = timeout

    def draft(self, report_path: Path, emails: list[dict]) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            corpus = Path(tmp) / "corpus.json"
            corpus.write_text(json.dumps(emails, ensure_ascii=False), encoding="utf-8")
            input_file = Path(tmp) / "input.yaml"
            input_file.write_text(
                yaml.safe_dump(
                    # `provider` is the same override the flag below carries: a script
                    # stage cannot see the CLI flag, and `drafts-pre` needs it to record
                    # which provider the leg ran on (STU-1145).
                    {"corpus": str(corpus), "report": str(report_path),
                     "provider": self.provider},
                    sort_keys=False, allow_unicode=True,
                ),
                encoding="utf-8",
            )

            cmd = ["studio", "run", _PIPELINE, "--input-file", str(input_file)]
            if self.provider:
                cmd += ["--provider", self.provider]

            _log.info("drafting from %s: %s", report_path, " ".join(cmd))
            result = subprocess.run(
                cmd,
                cwd=self.cwd,
                timeout=self.timeout,
                env={
                    # The drafting tools shell out to `${PYTHON:-.venv/bin/python}`
                    # (STU-856), which the Makefile exports and the systemd unit does
                    # not — under the service the fallback is a venv that need not be
                    # at the checkout root. The interpreter running us is the answer.
                    # Studio's own script stages ask a different variable, and the
                    # mailbox write is one of them now (STU-1030); without it they
                    # sniff a `.venv/` beside the cwd on the one host that drafts
                    # unattended.
                    "PYTHON": sys.executable,
                    "STUDIO_PYTHON_BIN": sys.executable,
                    **os.environ,
                    # The child asks the provider for its own grant, and no argument
                    # of ours reaches it: this is how it learns nobody can consent.
                    HEADLESS_ENV: "1",
                },
            )
            if result.returncode != 0:
                raise RuntimeError(f"`studio run {_PIPELINE}` exited {result.returncode}")
