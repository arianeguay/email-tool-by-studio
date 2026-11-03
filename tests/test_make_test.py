"""`make test` is the ner-free suite, and stays it (STU-1059).

CLAUDE.md called that suite the tight loop and the default at every commit for a
release before anything in the repo ran it: the rule lived in prose, and the two
documented commands were the extra-carrying interpreter and a venv narrower than CI.
So what is asserted here is the drift that prose could not stop — the target running
on `$(PYTHON)` again, the extras diverging from the job the target exists to mirror,
or `test-ner` letting the ~40 person-detection tests skip on an interpreter that
cannot run them.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")


def _recipe(target: str) -> str:
    lines = MAKEFILE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


def test_make_test_runs_on_the_test_venv_and_never_on_python():
    recipe = _recipe("test")
    assert "$(TEST_PYTHON) -m pytest" in recipe
    # The failure this target was filed against: `$(PYTHON)` resolves to a checkout
    # set up per the Setup section, so it carries the `ner` extra and every module
    # touching a name loads GLiNER.
    assert "$(PYTHON)" not in recipe


def test_make_test_runs_the_whole_suite_in_parallel():
    # A subset would be the hand-picked files STU-1005 stopped justifying, and serial
    # is what made picking them tempting.
    assert "-n auto" in _recipe("test")


def test_the_test_venv_installs_what_the_hermetic_job_installs():
    setup = _recipe("test-venv")
    # `webhook` is what pulls flask: without it the seven test_webhook_* modules skip
    # as a body, and a pass missing them looks exactly like a pass.
    assert '-e "$(KERNEL)[dev,webhook]"' in setup
    assert "hermetic" in WORKFLOW
    assert 'pip install -e ".[dev,webhook]"' in WORKFLOW
    # The extra is the whole cost, and the target's claim is that it is absent.
    assert "ner]" not in setup


def test_the_test_venv_is_rebuilt_when_the_dependencies_change():
    setup = _recipe("test-venv")
    # A venv created once and never refreshed reports a green pass against last
    # month's dependencies. The trigger is the file that declares them.
    assert "pyproject.toml" in setup


def test_make_test_ner_refuses_an_interpreter_without_the_extra():
    recipe = _recipe("test-ner")
    # It is the only target that runs those ~40 tests, so it is the one place where a
    # skip is indistinguishable from a pass. Refusing is the point.
    assert "import gliner" in recipe
    assert "exit 1" in recipe


def test_both_targets_are_phony():
    phony = re.search(r"^\.PHONY:(.*)$", MAKEFILE, re.MULTILINE).group(1).split()
    assert {"test", "test-venv", "test-ner"} <= set(phony)
