"""Append-only store of LLM verdicts, keyed by sender (STU-822).

The frontier log records what Stage 1 decided; this records what Stage 2 concluded,
with the sender attached — the corpus the promotion job aggregates over. Webhook mode
classifies one email per notification, so accumulation has to survive across process
invocations: a file appended from `pipeline.run`, not run-scoped state.

Contains real sender addresses — gitignored, never committed.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Observation:
    run_id: str
    timestamp: str
    message_id: str
    thread_id: str
    from_address: str
    bucket: str
    suggested_action: str
    confidence: str


class VerdictStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        run_id: str,
        message_id: str,
        thread_id: str,
        from_address: str,
        bucket: str,
        suggested_action: str,
        confidence: str,
    ) -> None:
        entry = {
            "run_id": run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_id": message_id,
            "thread_id": thread_id,
            "from_address": from_address.lower(),
            "bucket": bucket,
            "suggested_action": suggested_action,
            "confidence": confidence,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()


def load_observations(path: Path) -> list[Observation]:
    if not path.exists():
        return []
    return [
        Observation(
            run_id=d["run_id"],
            timestamp=d["timestamp"],
            message_id=d["message_id"],
            # A row written before STU-944 is its own conversation: it was one
            # judgement, which is what the field is counted for.
            thread_id=d.get("thread_id") or d["message_id"],
            from_address=d["from_address"].lower(),
            bucket=d["bucket"],
            suggested_action=d["suggested_action"],
            confidence=d["confidence"],
        )
        for d in (
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    ]
