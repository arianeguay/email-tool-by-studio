import os
import subprocess
import sys
import types

import pytest

from conftest import (
    ENV_EXAMPLE_KEYS,
    REPO_ROOT,
    _no_detector,
    pin_gliner,
    pytest_exception_interact,
    pytest_runtest_setup,
)

from email_classifier import anonymize


def _wont_load():
    raise RuntimeError("CUDA out of memory")


def _interact(error):
    report = types.SimpleNamespace(sections=[])
    pytest_exception_interact(report, types.SimpleNamespace(excinfo=types.SimpleNamespace(value=error)))
    return report.sections


def test_a_failing_subprocess_reports_what_the_child_printed():
    error = subprocess.CalledProcessError(1, ["tool"], output="", stderr="ImportError: gliner\n")
    assert _interact(error) == [("Captured stderr of the failing subprocess", "ImportError: gliner\n")]


def test_a_child_that_captured_bytes_is_decoded():
    error = subprocess.CalledProcessError(1, ["git"], stderr=b"fatal: not a repo\n")
    assert _interact(error) == [("Captured stderr of the failing subprocess", "fatal: not a repo\n")]


def test_an_ordinary_assertion_failure_adds_nothing():
    assert _interact(AssertionError("nope")) == []


class _Item:
    """A test item, reduced to the one question `pytest_runtest_setup` asks it."""

    def __init__(self, marked):
        self.marked = marked

    def get_closest_marker(self, name):
        return object() if self.marked and name == "requires_gliner" else None


@pytest.fixture
def unprobed():
    _no_detector.cache_clear()
    yield
    _no_detector.cache_clear()


def test_an_unloadable_model_skips_a_marked_test(monkeypatch, unprobed):
    """STU-992: the import probe alone passed these tests to a machine that cannot run them."""
    pin_gliner(monkeypatch)
    monkeypatch.setattr(anonymize, "_model", _wont_load)

    with pytest.raises(pytest.skip.Exception) as skipped:
        pytest_runtest_setup(_Item(marked=True))

    assert "CUDA out of memory" in str(skipped.value)


def test_an_absent_extra_is_named_as_the_extra_and_not_as_the_model(monkeypatch, unprobed):
    """A machine that was never set up and one that has broken since are different fixes."""
    pin_gliner(monkeypatch, present=False)
    monkeypatch.setattr(anonymize, "_model", _wont_load)

    with pytest.raises(pytest.skip.Exception) as skipped:
        pytest_runtest_setup(_Item(marked=True))

    assert "ner" in str(skipped.value) and "did not load" not in str(skipped.value)


def test_an_unmarked_test_never_pays_for_the_probe(monkeypatch, unprobed):
    """The hook exists instead of a `skipif` so a run that wants no model loads none."""
    pin_gliner(monkeypatch)
    monkeypatch.setattr(anonymize, "_model", _wont_load)

    assert pytest_runtest_setup(_Item(marked=False)) is None


# ── the environment a test runs in (STU-1076) ────────────────────────────────


def test_no_env_example_key_reaches_a_test():
    """On the machine that wrote the `.env`, `make test` exports every key in it."""
    assert [key for key in ENV_EXAMPLE_KEYS if key in os.environ] == []


def test_the_values_the_suite_acts_on_are_in_the_cleared_set():
    """A parse that returned comment lines, or nothing at all, would pass the one
    above too — on a machine that has no `.env` to begin with."""
    assert {
        "ALERT_WEBHOOK_URL",
        "GRAPH_CLIENT_STATE",
        "GRAPH_NOTIFICATION_URL",
        "WEBHOOK_HEALTH_URL",
        "WEBHOOK_HOST",
        "WEBHOOK_PORT",
        "WEBHOOK_SSH_HOST",
        "WEBHOOK_STALE_AFTER_SECONDS",
    } <= set(ENV_EXAMPLE_KEYS)


def test_an_ambient_value_does_not_reach_a_test():
    """The two above are green on a machine with no `.env` whether the fixture works
    or not, which is every CI runner. This pollutes the environment the way an
    exported `.env` does and asks a real run the same question."""
    env = {**os.environ, **{key: "ambient" for key in ENV_EXAMPLE_KEYS}}
    env["PYTHONPATH"] = str(REPO_ROOT)
    inner = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_conftest_hooks.py::test_no_env_example_key_reaches_a_test",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert inner.returncode == 0, inner.stdout + inner.stderr
