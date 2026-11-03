import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.stage1.policy import Policy


class FrontierLogger:
    def __init__(self, log_path: Path, stdout: bool = False, run_id: str | None = None) -> None:
        self.log_path = log_path
        self.stdout = stdout
        # The run id names the report and ties the applied journal to it, so a pipeline
        # that logs and reports from two separate processes has to be able to pass it in.
        self.run_id = run_id or str(uuid.uuid4())
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, email: EmailInput, match: RuleMatch) -> None:
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
        line = json.dumps(entry, ensure_ascii=False)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        if self.stdout:
            print(line, flush=True)


def make_logger(policy: Policy, log_path: Path, run_id: str | None = None) -> FrontierLogger:
    return FrontierLogger(
        log_path=log_path, stdout=policy.frontier_log_stdout, run_id=run_id
    )
