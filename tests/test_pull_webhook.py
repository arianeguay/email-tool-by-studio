"""`make pull-webhook` deploys the recipe it pulled, not the previous commit's (STU-1068).

Make parses the whole file before the first recipe line runs, so a deploy step added
by the pull is not in the recipe doing the pulling: the run reports success having
skipped it. That is the failure STU-1017 exists to make visible, one level up — a
deploy step that silently does not run.

So the shape is asserted twice: once as behaviour, over a throwaway repo whose
Makefile changes between the pulled commits, and once against the real target, since
a passing mechanism says nothing about whether this repo's target uses it.

`pull-webhook` itself is now the dispatch that picks the machine, and the recipe those
assertions are about is `pull-webhook-here`. The dispatch's own two claims are asserted
at the bottom: that it never hands the far side the name that would dispatch again, and
that it refuses a machine that does not serve the webhook rather than merging into it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

DELEGATING = """\
pull:
\tgit pull --ff-only
\t$(MAKE) deploy

deploy:
\t@echo RECIPE-{version}
"""

INLINE = """\
pull:
\tgit pull --ff-only
\t@echo RECIPE-{version}
"""


def _recipe(target: str) -> str:
    lines = MAKEFILE.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    body = []
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        body.append(line)
    return "\n".join(body)


def _without_the_privilege_drop(line: str) -> str:
    """Strip the `sudo -u` prefix STU-1116 put on every write into the checkout.

    Who runs a line is not what either caller asserts: one is about sub-make discipline
    after the merge, the other about which exit codes the deploy tolerates. Left in, the
    prefix would pin an unrelated detail in the first and actually invoke sudo in the
    second.
    """
    return line.replace("$(AS_SERVICE) ", "")


def _steps(target: str) -> list[str]:
    """`target`'s recipe as the commands it runs — comments and the privilege drop out."""
    return [_without_the_privilege_drop(line.strip())
            for line in _recipe(target).splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def _pull_across_a_makefile_change(tmp_path: Path, template: str) -> str:
    """Serve `template` at OLD, publish it at NEW, and return what `make pull` printed."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "Makefile").write_text(template.format(version="OLD"), encoding="utf-8")
    _git(origin, "add", "Makefile")
    _git(origin, "commit", "-m", "old")

    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))

    (origin / "Makefile").write_text(template.format(version="NEW"), encoding="utf-8")
    _git(origin, "commit", "-am", "new")

    done = subprocess.run(
        ["make", "pull"], cwd=clone, check=True, capture_output=True, text=True
    )
    return done.stdout


@pytest.mark.skipif(shutil.which("make") is None, reason="needs make")
def test_a_pull_that_delegates_to_a_sub_make_runs_the_recipe_it_pulled(tmp_path):
    # The sub-make is a second make process, so it reads the Makefile the pull wrote.
    assert "RECIPE-NEW" in _pull_across_a_makefile_change(tmp_path, DELEGATING)


@pytest.mark.skipif(shutil.which("make") is None, reason="needs make")
def test_a_pull_with_the_deploy_inline_runs_the_previous_commit_s_recipe(tmp_path):
    # The bug itself, so the test above is known to be measuring something: without
    # this, a harness that never reached the new commit would pass it too.
    assert "RECIPE-OLD" in _pull_across_a_makefile_change(tmp_path, INLINE)


def test_pull_webhook_runs_nothing_inline_after_the_pull():
    """Only a sub-make may follow the merge — an inline step there is the pulled-past one.

    The reconcile (STU-1081) runs before it and could not be anywhere else: it decides
    whether the merge may happen at all. It is the previous commit's version for the
    same reason `git pull` itself is, which is not the bug. The bug is a step stranded
    *after* the merge in a recipe make already parsed, so it silently runs the version
    the pull replaced (STU-1068).

    Stated as the invariant rather than as the list: `$(MAKE) <target>` is a second make
    process reading the Makefile the pull just wrote, so it is never stranded, and a
    recipe that gains one (STU-1084 added `audit-file`) must not have to edit this test
    to say so. Anything else after the merge must fail here.
    """
    lines = _steps("pull-webhook-here")
    # The fetch moved into the reconcile, so the merge is of what it compared against.
    assert lines[:2] == [
        "$(PYTHON) scripts/reconcile_audits.py",
        "git merge --ff-only FETCH_HEAD",
    ]
    assert lines[2:], "the pull must still hand off to a sub-make"
    assert all(line.startswith("$(MAKE) ") for line in lines[2:]), lines[2:]
    assert "$(MAKE) deploy-webhook" in lines[2:]


def test_the_deploy_steps_live_in_the_target_the_sub_make_re_reads():
    recipe = _recipe("deploy-webhook")
    assert "git submodule update --init" in recipe
    assert '-m pip install -e "$(KERNEL)[webhook,fetch-outlook,ner,home-assistant]"' in recipe
    assert "$(MAKE) install-webhook-service" in recipe
    assert "sudo systemctl restart email-classifier-webhook" in recipe
    # The step whose absence measured the bug on the serving host: WAIT is an argument of this
    # recipe, so a pull that introduces it must be the pull that runs it.
    assert "check-webhook WAIT=60" in recipe


# --- Which exit codes the deploy's verification step tolerates (STU-1095) -----
#
# The step exists to catch exit 3 -- a host still serving the old commit, which is
# what a skipped restart looks like (STU-1017). Everything else it lets through says
# nothing about whether the deploy landed. Both directions are silent when wrong:
# narrow the list and every deploy fails once the client secret enters its warning
# window; widen it to swallow 3 and drift stops blocking deploys at all, which is the
# guard itself going quiet.
#
# So the fragment is executed rather than matched. A string assertion on `0|1|6|7`
# would pass against a rewrite that reads the same and behaves differently, and the
# shell here is the whole of the behaviour.
#
# And it runs the real `check-webhook` recipe through make, with only the probe
# stubbed. Stubbing the sub-make itself hid STU-2044: make exits 2 whenever a recipe
# fails, so every tolerated code reached the `case` as 2 and every deploy failed.

_TOLERATED = (0, 1, 6, 7, 8, 10)
_REJECTED = (2, 3, 4, 5, 9)


def _verification_fragment(privilege_drop: str, probe: str) -> str:
    """The deploy's verification step as a runnable shell fragment.

    `probe` replaces the interpreter `check-webhook` runs, `privilege_drop` stands in
    for `sudo` itself; the sub-make is the real one, run against this Makefile.
    The step spans several physical lines joined by backslashes -- it has to, since
    the verdict has to travel from the sub-make's stdout into the `case` -- so the
    whole block is taken rather than one line.
    """
    lines = [raw.strip() for raw in _recipe("deploy-webhook").splitlines()]
    start = next(i for i, line in enumerate(lines) if "check-webhook WAIT=60" in line)
    fragment = "\n".join(lines[start:])
    fragment = fragment.replace(
        "$(MAKE)", f"make -C {REPO_ROOT} PYTHON='{probe}'"
    )
    fragment = fragment.replace("$(AS_SERVICE)", privilege_drop)
    # `$$` is make's escape for a literal `$`; the shell running it wants one.
    return fragment.replace("$$", "$")


def _stub_probe(tmp_path: Path, code: int) -> str:
    """A probe that says something on stdout, as the real one does, then exits `code`.

    Run through `sh` rather than marked executable: what is under test is the make
    and shell around it, not the stub.
    """
    stub = tmp_path / "probe"
    stub.write_text(f"echo probe output\nexit {code}\n", encoding="utf-8")
    return f"sh {stub}"


def _verification_status(tmp_path: Path, code: int) -> int:
    """Run the step with the privilege drop out of the way and the probe exiting `code`."""
    fragment = _verification_fragment("", _stub_probe(tmp_path, code))
    return subprocess.run(["sh", "-c", fragment]).returncode


@pytest.mark.parametrize("code", _TOLERATED)
def test_the_deploy_tolerates_the_codes_that_say_nothing_about_it(tmp_path, code):
    """A quiet mailbox, the two credential-expiry codes, a blind drift leg and an
    expiring audit entry: none of them is evidence the restart failed, and failing on
    6 would block the deploy carrying the fix."""
    assert _verification_status(tmp_path, code) == 0


@pytest.mark.parametrize("code", _REJECTED)
def test_the_deploy_fails_on_a_host_still_serving_the_old_commit(tmp_path, code):
    """3 above all: it is the one this step was added for, and 9 is the same drift
    standing longer. The others have never been tolerated either, and a rewrite that
    quietly starts letting one through is the same silence one level down."""
    assert _verification_status(tmp_path, code) != 0


def test_the_deploy_names_the_probe_s_own_code_when_it_fails(tmp_path, capfd):
    """The message quotes the probe's 3, not the 2 make exits with (STU-2044)."""
    assert _verification_status(tmp_path, 3) != 0
    assert "check-webhook exited 3," in capfd.readouterr().err


def test_the_deploy_fails_when_the_privilege_drop_itself_refuses(tmp_path, capfd):
    """sudo's own 1 is not the check's 1 (STU-1122).

    `sudo` exits 1 when sudo fails -- no sudoers entry, an expired timestamp with no
    tty, a password prompt it cannot ask -- which is the same 1 the list above tolerates
    as a quiet mailbox. Tolerated, a deploy whose verification never ran reports success:
    the silence STU-1017 added the exit-3 leg to break. `false` is the stub because it
    is what sudo refusing does -- it does not run the command and exits 1.

    Nothing fires this today: `sudo systemctl restart` runs earlier in the same recipe
    and is not tolerated, so a broken sudo fails there first. That is a real protection
    and an invariant nothing asserts, which is why it is asserted here instead of
    relying on the ordering.
    """
    assert _verification_status(tmp_path, 0) == 0, "the check exiting 0 is the tolerated case"

    assert subprocess.run(
        ["sh", "-c", _verification_fragment("false", _stub_probe(tmp_path, 0))]
    ).returncode != 0
    assert "never ran" in capfd.readouterr().err


def test_check_webhook_keeps_the_probe_s_exit_without_verdict():
    """The cron reads the probe's code from make's exit, so VERDICT stays opt-in."""
    recipe = _recipe("check-webhook")
    assert "$(if $(VERDICT),>&2; echo $$?)" in recipe


# --- Which machine the dispatch decides it is on ------------------------------
#
# `make pull-webhook` runs from a workstation as well as from the serving host. Both of
# its claims fail silently when wrong, which is why they are here rather than left to the
# next deploy: a dispatch that hands the far side `pull-webhook` loops through ssh until
# something gives up, and a local leg that trusts an unset `WEBHOOK_SSH_HOST` merges into
# whatever checkout it was run from and `pip install`s into that venv -- both of which
# succeed before the restart it cannot do fails.

DISPATCH = REPO_ROOT / "scripts" / "pull_webhook.sh"


def _dispatch(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, Path]:
    """Run the dispatch with a recording `ssh` and `make` ahead of the real ones."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ssh", "make"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\nprintf "{name} %s\\n" "$*" >> "$RECORD"\n',
                        encoding="utf-8")
        stub.chmod(0o755)
    record = tmp_path / "record"
    record.touch()
    return subprocess.run(
        [str(DISPATCH)],
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "RECORD": str(record), **env},
        capture_output=True,
        text=True,
        cwd=tmp_path,
    ), record


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a shell")
def test_the_far_side_is_handed_the_half_that_cannot_dispatch_again(tmp_path):
    """`pull-webhook-here`, never `pull-webhook`.

    The far side reads its own `.env`, and nothing here can see what that sets. Handed
    the dispatching name, a serving host that happens to set `WEBHOOK_SSH_HOST` would ssh
    onward -- to itself, or to a third machine. Handing it the half that only ever runs
    locally makes that unreachable by construction rather than by what the far side is
    configured to say.
    """
    done, record = _dispatch(tmp_path, WEBHOOK_SSH_HOST="serving-host")
    assert done.returncode == 0, done.stderr
    sent = record.read_text(encoding="utf-8")
    assert sent.startswith("ssh ") and "serving-host" in sent
    assert "pull-webhook-here" in sent
    # The name it must not carry is a prefix of the one it must, so it is only absent
    # once the good one is out of the way.
    assert "pull-webhook" not in sent.replace("pull-webhook-here", "")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a shell")
def test_the_checkout_on_the_far_side_is_nameable(tmp_path):
    """A host whose checkout is not /opt/email-classifier is a host, not a rewrite."""
    _, record = _dispatch(tmp_path, WEBHOOK_SSH_HOST="serving-host",
                          WEBHOOK_CHECKOUT="/srv/ec")
    assert "/srv/ec" in record.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a shell")
def test_a_machine_that_does_not_serve_this_checkout_is_refused(tmp_path):
    """Not "assume local": the merge and the pip install succeed on any machine.

    Run from a directory no unit serves, which is every machine including the serving
    host -- the guard is about this checkout, not about this host, so the test needs no
    skip and does not go quiet on the one machine where the local leg is real.

    Refusing names both repairs, because which one applies is the thing the machine
    cannot know: a workstation missing the variable, or a serving host asked from the
    wrong tree.
    """
    done, record = _dispatch(tmp_path)
    assert done.returncode != 0
    assert record.read_text(encoding="utf-8") == "", "nothing may run before the refusal"
    assert "WEBHOOK_SSH_HOST" in done.stderr
    assert "deploy-webhook" in done.stderr
    # Which machine it read, not just that it refused: a workstation carrying a stale
    # unit -- one from before the service account, still enabled and pointing a login
    # account at a dev checkout -- is the case this guard was rewritten for, and "not the
    # serving host" would send whoever reads it hunting a file that is right there.
    assert "email-classifier-webhook.service" in done.stderr


def test_pull_webhook_delegates_to_the_dispatch():
    """The target is the dispatch and nothing else, so neither machine gets a step the
    other does not. A line added here runs before the machine is decided."""
    assert _steps("pull-webhook") == ["@scripts/pull_webhook.sh"]
