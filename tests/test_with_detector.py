"""The batch entry points hold one detector too (STU-1020), not just the webhook."""

import json
import os
import sys
from pathlib import Path

import pytest

from email_classifier import anonymize, detector

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import with_detector  # noqa: E402

# Stands in for a process a run spawns — a `studio` child, or the tool subprocess it
# spawns in turn. It asks for spans the way `anonymize._person_spans` does, and writes
# what it got where the test can read it.
CHILD = """
import json, os, sys
from email_classifier.detector import SOCKET_ENV, remote_spans
open(sys.argv[1], "w").write(json.dumps({
    "socket": os.environ.get(SOCKET_ENV),
    "spans": remote_spans("Caroline"),
}))
"""


def _recipe(target: str) -> str:
    lines = (REPO_ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


@pytest.fixture
def one_span(monkeypatch):
    """The server half, stubbed: the claim under test is who loaded it, not what it found."""
    monkeypatch.delenv(detector.SOCKET_ENV, raising=False)
    monkeypatch.setattr(
        anonymize, "_local_person_spans",
        lambda text, from_address=None: [(0, len(text), "person")],
    )


def test_the_child_is_served_by_the_detector_the_wrapper_holds(one_span, tmp_path):
    out = tmp_path / "child.json"
    assert with_detector.main([sys.executable, "-c", CHILD, str(out)]) == 0
    answer = json.loads(out.read_text())
    # The variable reached the child, and the spans came back over it — so the child
    # never loaded weights of its own, which is the whole saving.
    assert answer["socket"] is not None
    assert answer["spans"] == [[0, 8, "person"]]


def test_the_wrapper_exits_with_the_command_s_status(one_span):
    assert with_detector.main([sys.executable, "-c", "raise SystemExit(3)"]) == 3


def test_the_service_is_gone_once_the_command_returns(one_span):
    assert with_detector.main([sys.executable, "-c", ""]) == 0
    assert detector.SOCKET_ENV not in os.environ


def test_no_command_is_a_usage_error_and_starts_nothing(one_span):
    assert with_detector.main([]) == 2
    assert detector.SOCKET_ENV not in os.environ


@pytest.mark.parametrize(
    "target", ["run", "run-fetch", "run-batch", "mailbox-full", "redraft"]
)
def test_the_batch_entry_points_carry_the_wrapper(target):
    """A wrapper nothing invokes is the gap STU-1020 closed, reopened silently."""
    assert "$(WITH_DETECTOR)" in _recipe(target)
