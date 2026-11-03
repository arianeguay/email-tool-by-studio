"""Webhook state persisted across invocations, one small JSON file per fact.

The two counters of STU-959 are read-modify-write and take no lock: each lives in
its own file with a single writer — notifications on the request thread, runs on
the worker thread.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def load_history_id(path: Path) -> str | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["history_id"]


def save_history_id(path: Path, history_id: str) -> None:
    _write(path, {"history_id": str(history_id)})


def record_notification(path: Path) -> None:
    """Stamp the arrival of an authenticated notification (STU-931) and count it."""
    _write(path, {
        "at": datetime.now(timezone.utc).isoformat(),
        "count": load_notification_count(path) + 1,
    })


def load_last_notification(path: Path) -> datetime | None:
    at = _read(path).get("at")
    return datetime.fromisoformat(at) if at else None


def load_notification_count(path: Path) -> int:
    # A file written before the counter existed carries a stamp and no count, so the
    # total resumes from the notifications this version saw rather than from one.
    return _read(path).get("count", 0)


def record_run(path: Path) -> None:
    """Count a run the push worker completed (STU-959)."""
    _write(path, {"count": load_run_count(path) + 1})


def load_run_count(path: Path) -> int:
    return _read(path).get("count", 0)


def load_subscription(path: Path) -> dict:
    """The Graph subscription record, or {} when none has been created yet."""
    return _read(path)


def record_subscription(path: Path, response: dict) -> None:
    """Write the id and expiry a create or renew answered with.

    Merged into the record rather than replacing it: the cron owns these two
    fields and the webhook owns the watermark (STU-937), and neither writer
    knows the other's. The watermark is baselined the first time a subscription
    is recorded — a catch-up cannot reach mail that predates delivery.
    """
    record = load_subscription(path)
    record["subscription_id"] = response["id"]
    record["expires_at"] = response["expirationDateTime"]
    record.setdefault("watermark", datetime.now(timezone.utc).isoformat())
    _write(path, record)


def save_watermark(path: Path, watermark: str) -> None:
    """Advance where a `missed` catch-up resumes: the newest message classified."""
    _write(path, {**load_subscription(path), "watermark": watermark})


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    # Atomic: /healthz and the next notification both read these files while the server
    # is writing them, and a torn read is a 500 on the endpoint watching for silence.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)
