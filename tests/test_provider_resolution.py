"""Both orchestrations resolve the classifier's provider the same way (STU-1128).

`STUDIO_PROVIDER` means "the provider this host runs". The push path read it and
`classify-batch` did not, so on a host set to `claude-code` with no `ANTHROPIC_API_KEY`
the push runs reported `provider: claude-code` and 0 errors while the hand-run
`mailbox-full` of the same hour reported `provider: anthropic` and `errors: 1` — the
one message it existed to classify, lost inside a run whose four stages were green.

These drive `make -n`, so what is asserted is the command each target would run rather
than a line copied out of the Makefile.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from tests.conftest import stub_detector
from tests.test_mailbox_full import EMAIL, load_script

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every entry point that hands real mail to an agent. The bake-offs are deliberately out
# and keep the plain `PROVIDER_FLAG`: a measurement taken under whatever a host happens
# to run is a measurement of the host, which is why `draft_bakeoff.py` names `anthropic`
# for itself rather than inheriting one.
LIVE_TARGETS = ("run", "run-batch", "run-fetch", "mailbox-full", "redraft")
MEASUREMENT_TARGETS = ("classify-bakeoff", "eval-ab")


def _recipe(target: str, **overrides: str) -> str:
    """What `make` would run for `target`, without running it.

    Every override goes on the **command line**, the only level that beats the rest:
    `-include .env` makes each key of an operator's `.env` a file variable, and a file
    assignment wins over the environment. Handed through the environment instead,
    `STUDIO_PROVIDER` here would lose to the `.env` of any host that sets it — the very
    hosts `.env.example` now tells to set it — and these assertions would be about that
    host's value rather than this test's.

    `PROVIDER` is dropped from the inherited environment rather than blanked, for the
    other half of the same rule: the suite runs under `make test`, whose `export` hands
    every make variable to pytest, and `?=` yields to a variable the environment merely
    *defines*, so an inherited empty one reads as a host that chose it.

    It runs from an empty directory, so no `.env` is read at all: a laptop's holds
    `op://` references, and the Makefile refuses those before printing a recipe
    (STU-2012).
    """
    inherited = {k: v for k, v in os.environ.items()
                 if k != "PROVIDER" and not k.startswith("MAKE")}
    with tempfile.TemporaryDirectory() as empty:
        proc = subprocess.run(
            ["make", "-n", "-f", str(REPO_ROOT / "Makefile"), target,
             *(f"{k}={v}" for k, v in overrides.items())],
            cwd=empty, capture_output=True, text=True,
            env={**inherited, "GIT_CEILING_DIRECTORIES": str(Path(empty).parent)},
        )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.mark.parametrize("target", LIVE_TARGETS)
def test_the_hosts_provider_reaches_every_studio_launched_path(target):
    """The half the push path already had: the flag beats the agent's own `provider`,
    so this is what stops `classify-batch` falling back to `classifier.agent.yaml`."""
    assert "--provider claude-code" in _recipe(target, STUDIO_PROVIDER="claude-code")


@pytest.mark.parametrize("target", LIVE_TARGETS)
def test_an_unset_variable_leaves_the_agents_own_pin_standing(target):
    """STU-1013: a host that names no provider gets no flag, and the classifier's own
    `anthropic` answers — forcing one here re-pinned every stage to the CLI."""
    assert "--provider" not in _recipe(target, STUDIO_PROVIDER="")


@pytest.mark.parametrize("target", LIVE_TARGETS)
def test_the_command_line_still_wins_for_a_deliberate_ab(target):
    recipe = _recipe(target, PROVIDER="mock", STUDIO_PROVIDER="claude-code")
    assert "--provider mock" in recipe
    assert "claude-code" not in recipe


@pytest.mark.parametrize("target", MEASUREMENT_TARGETS)
def test_a_measurement_does_not_inherit_the_host_provider(target):
    """A bake-off scored under whatever a host happens to run is a measurement of the
    host: `claude-code` builds its prompt from the cwd, so both arms would carry this
    repo's CLAUDE.md into the prompt and the bill (STU-1013). `draft_bakeoff.py` names
    `anthropic` for itself for the same reason."""
    assert "--provider" not in _recipe(target, STUDIO_PROVIDER="claude-code")
    # Still steerable by hand, which is what the flag is for.
    assert "--provider mock" in _recipe(target, PROVIDER="mock")


def test_the_batch_input_file_carries_what_the_flag_carries():
    """`provenance.provider` is written from the input file and the run happens under
    the flag, so a report only records what resolved while the two come from one
    variable. Both hand-run reports of 2026-08-30 recorded `anthropic` while their
    children died on it."""
    for target in ("run-batch", "mailbox-full"):
        recipe = _recipe(target, STUDIO_PROVIDER="claude-code")
        written = next(line for line in recipe.splitlines() if line.rstrip().endswith(
            ".input.yaml"))
        assert "'claude-code'" in written, recipe
        assert "--provider claude-code" in recipe


def test_classify_pre_refuses_before_the_fan_out(tmp_path, monkeypatch, capsys):
    """A dead provider fails every child identically, and `on_failure: continue` — the
    STU-392 bias that keeps one dead conversation from failing the batch — then hides
    all of them behind a green run. One dead email is the bias; a dead provider is not.
    """
    stub_detector(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    classify_pre = load_script("classify_pre")

    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps([EMAIL]), encoding="utf-8")
    # Named rather than left to `.studio/`: since STU-1133 dropped the classifier's pin
    # the default resolves to `claude-code`, which needs no credential — so this is the
    # host that asks for the provider that does.
    payload = json.dumps({"additional_context": yaml.safe_dump(
        {"corpus": str(dump), "run_id": "r1", "provider": "anthropic",
         "log": str(tmp_path / "frontier.jsonl"),
         "keymap": str(tmp_path / "keymap.json")})})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    with pytest.raises(SystemExit) as refusal:
        classify_pre.main()

    # Names the provider, which is the whole of what an operator acts on.
    assert "anthropic" in str(refusal.value)
    # And nothing was written: the refusal lands before the frontier log and the keymap.
    assert not (tmp_path / "frontier.jsonl").exists()
    assert not (tmp_path / "keymap.json").exists()


def test_the_host_provider_clears_the_same_refusal(tmp_path, monkeypatch, capsys):
    """The verification of the issue, in one assertion: with STUDIO_PROVIDER set and no
    key, the batch path classifies the mail the push path handles."""
    stub_detector(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    classify_pre = load_script("classify_pre")

    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps([EMAIL]), encoding="utf-8")
    payload = json.dumps({"additional_context": yaml.safe_dump(
        {"corpus": str(dump), "run_id": "r1", "provider": "claude-code",
         "log": str(tmp_path / "frontier.jsonl"),
         "keymap": str(tmp_path / "keymap.json")})})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    classify_pre.main()
    out = json.loads(capsys.readouterr().out)

    assert out["provenance"]["provider"] == "claude-code"
