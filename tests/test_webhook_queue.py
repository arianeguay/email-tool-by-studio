"""The durable notification queue behind the webhook's ack (STU-927)."""

import os
import threading
import time

import pytest

from email_classifier.webhook.queue import WorkQueue
from tests.conftest import BURST_WINDOW


def test_items_outlive_the_process_until_they_are_done(tmp_path):
    path = tmp_path / "notifications.jsonl"
    queue = WorkQueue(path)
    queue.put({"provider": "outlook", "message_ids": ["m1"]})

    reloaded = WorkQueue(path)  # what a restart sees
    (item,) = reloaded.take_batch()
    assert item == {"provider": "outlook", "message_ids": ["m1"]}

    reloaded.done(item)
    assert WorkQueue(path).wait_empty(timeout=0)


def test_a_batch_is_served_oldest_first(tmp_path):
    queue = WorkQueue(tmp_path / "notifications.jsonl")
    queue.put({"provider": "gmail", "history_id": "1"})
    queue.put({"provider": "gmail", "history_id": "2"})

    first, second = queue.take_batch()
    assert [first["history_id"], second["history_id"]] == ["1", "2"]
    queue.done(first)
    assert [item["history_id"] for item in queue.take_batch()] == ["2"]


def test_a_failed_flush_leaves_the_item_out_of_the_queue(monkeypatch, tmp_path):
    """The worker clears an item it is done with even when the disk refuses it —
    a raise there would end the drain thread and the routes would keep acking."""
    queue = WorkQueue(tmp_path / "notifications.jsonl")
    queue.put({"provider": "gmail", "history_id": "1"})
    (item,) = queue.take_batch()

    def refuse(*args):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        queue.done(item)

    monkeypatch.undo()
    assert queue.wait_empty(timeout=0)


def test_take_batch_blocks_until_an_item_arrives(tmp_path):
    queue = WorkQueue(tmp_path / "notifications.jsonl")
    taken = []

    consumer = threading.Thread(target=lambda: taken.extend(queue.take_batch()), daemon=True)
    consumer.start()
    queue.put({"provider": "gmail", "history_id": "7"})
    consumer.join(timeout=10)

    assert taken == [{"provider": "gmail", "history_id": "7"}]


def test_the_window_gathers_what_lands_after_the_first_item(tmp_path):
    """The debounce a burst is folded into one run by (STU-939)."""
    queue = WorkQueue(tmp_path / "notifications.jsonl")
    taken = []

    consumer = threading.Thread(
        target=lambda: taken.extend(queue.take_batch(BURST_WINDOW)), daemon=True
    )
    consumer.start()
    for history_id in ("1", "2", "3"):
        queue.put({"provider": "gmail", "history_id": history_id})
    consumer.join(timeout=30)

    assert [item["history_id"] for item in taken] == ["1", "2", "3"]


def test_the_window_is_fixed_from_the_first_item(tmp_path):
    """Not pushed back by each arrival: a mailbox that keeps receiving still gets
    classified rather than deferred for as long as the mail keeps coming."""
    queue = WorkQueue(tmp_path / "notifications.jsonl")
    queue.put({"provider": "gmail", "history_id": "1"})
    stop = threading.Event()

    def keep_arriving():
        while not stop.is_set():
            queue.put({"provider": "gmail", "history_id": "2"})
            time.sleep(0.02)

    noise = threading.Thread(target=keep_arriving, daemon=True)
    noise.start()
    started = time.monotonic()
    queue.take_batch(0.5)
    elapsed = time.monotonic() - started
    stop.set()
    noise.join(timeout=10)

    assert elapsed < 3.0


def test_attempts_are_durable_before_the_run(tmp_path):
    """The retry bound has to survive the restart a crashing run causes (STU-947)."""
    path = tmp_path / "notifications.jsonl"
    queue = WorkQueue(path)
    queue.put({"provider": "gmail", "history_id": "1"})

    assert queue.attempt(queue.take_batch()[0]) == 1
    # what a restart mid-run sees
    assert WorkQueue(path).take_batch()[0]["attempts"] == 1

    assert queue.attempt(queue.take_batch()[0]) == 2
