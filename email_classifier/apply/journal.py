"""Apply journal (STU-407): one JSONL line per mailbox operation.

Written to `logs/applied/<run_id>.jsonl` while the run happens — never after it
— so an interrupted run still leaves behind the trace `undo` needs. Same
non-negotiable status as the frontier log.

A dry-run writes the same shape with status "planned" to a separate
`.plan.jsonl` file; `undo` only ever inverts status "ok" lines.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class Operation:
    kind: str                 # "add_label" | "remove_label" | "trash" | "untrash" | "plan"
    label: str | None = None  # label NAME — ids are per-mailbox, names survive an undo


class ApplyJournal:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        message_id: str,
        rule_id: str | None,
        operation: Operation,
        status: str,
        error: str | None = None,
    ) -> None:
        entry = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message_id": message_id,
            "rule_id": rule_id,
            "op": operation.kind,
            "label": operation.label,
            "status": status,
            "error": error,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()


def journal_path(journal_dir: Path, run_id: str, *, dry_run: bool) -> Path:
    """Where a run's journal lives. `undo` only ever reads the non-plan file."""
    return journal_dir / f"{run_id}.{'plan.jsonl' if dry_run else 'jsonl'}"


def read_entries(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"no apply journal at {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]
