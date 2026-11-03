"""Durable FIFO of the work a push notification announced (STU-927).

The webhook acks a notification before classifying it, so the ack is a promise:
the provider will not redeliver. An item is on disk before the handler answers
and stays there until the worker is done with it, so a crash in between replays
the work at startup instead of dropping the mail it named.
"""

import json
import os
import threading
import time
from pathlib import Path


class WorkQueue:
    """JSONL-backed FIFO with one blocking consumer.

    `take_batch` leaves the items queued; only `done` removes one from disk.
    """

    def __init__(self, path: Path):
        self.path = path
        self._items = self._load()
        self._changed = threading.Condition()

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        body = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self._items)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            # A rename is atomic but says nothing about the bytes it renames:
            # without this, a host that loses power right after the ack can come
            # back to an entry naming no ids.
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def put(self, item: dict) -> None:
        """Record the work and return once it is durable."""
        with self._changed:
            self._items.append(item)
            self._flush()
            self._changed.notify_all()

    def take_batch(self, window: float = 0.0) -> list[dict]:
        """Block for the oldest item, then hold `window` open for its neighbours.

        The window is fixed from that first item rather than pushed back by each
        arrival (STU-939): a mailbox that keeps receiving must still be
        classified, not deferred for as long as the mail keeps coming.
        """
        with self._changed:
            self._changed.wait_for(lambda: bool(self._items))
            deadline = time.monotonic() + window
            while (remaining := deadline - time.monotonic()) > 0:
                self._changed.wait(remaining)
            return list(self._items)

    def attempt(self, item: dict) -> int:
        """Count one run of `item` and return how many it has now had.

        Counted before the run, not after it: a run that takes the process down
        never reaches an after-the-fact increment, and the replay at startup
        would retry it forever (STU-947).
        """
        with self._changed:
            item["attempts"] = item.get("attempts", 0) + 1
            self._flush()
            return item["attempts"]

    def done(self, item: dict) -> None:
        with self._changed:
            self._items.remove(item)
            self._flush()
            self._changed.notify_all()

    def wait_empty(self, timeout: float | None = None) -> bool:
        """Wait until every queued item has been processed."""
        with self._changed:
            return self._changed.wait_for(lambda: not self._items, timeout)
