"""Which commit the host is actually running (STU-1017).

Nothing on the serving host said which code the webhook was serving, so `84241af` — the fix
that stopped the push path handing conversations to the LLM in cleartext (STU-1009) —
sat merged and undeployed for days while the host kept answering every health check
perfectly. `make check-webhook` is the only job that exists to notice something wrong
there, and it could not notice this: the payload it reads carried uptime, notification
age, subscription expiry and run counts — every axis except which code produced them.
The drift was found by hand, reading run logs for an unrelated bug.

So `/healthz` names the commit, and the check measures it against what the remote's
`main` holds. Two choices the module makes:

* **Sampled once, at boot.** A `git pull` under a running service moves `HEAD` without
  changing a line of the code being served, and it is the served code the question is
  about. Read per request, the payload would go quiet on exactly the deploy nobody
  restarted — the one case that looks fixed and is not.
* **An unfetchable remote is unmeasured, never "current".** Comparing against a
  `main` this clone last saw weeks ago answers "in sync" for a host that is behind,
  which is the same silence the whole issue is about.
"""

import dataclasses
import re
import subprocess
from pathlib import Path

from email_classifier import deployment

REPO_ROOT = deployment.ROOT

DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"

# The remote read is a network call on an hourly cron; the local ones are disk.
_FETCH_TIMEOUT = 30.0
_LOCAL_TIMEOUT = 5.0

# The sha arrives over HTTP from the webhook, and every use of it is a git argument.
# Anything that is not a plain object name never reaches argv.
_SHA = re.compile(r"\A[0-9a-f]{7,40}\Z")


@dataclasses.dataclass(frozen=True)
class Drift:
    """How far the deployed commit is behind the branch it should be on.

    `commits_behind` is None when the question could not be answered at all — no
    commit in the payload, no git, no reachable remote. That is distinct from 0,
    and the caller must not read one as the other.
    """

    deployed: str | None
    target: str | None
    commits_behind: int | None
    unmeasured: str | None = None
    branch: str = f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"

    @property
    def behind(self) -> bool:
        return bool(self.commits_behind)

    @property
    def unverified(self) -> bool:
        """The leg answered nothing — distinct from answering "not behind".

        Stated here rather than at each caller so "the deploy went unmeasured"
        has one definition, the way `behind` does.
        """
        return self.commits_behind is None

    def summary(self) -> str:
        if self.commits_behind is None:
            return f"deploy unverified: {self.unmeasured}"
        running = f"running {self.deployed[:12]}"
        if self.commits_behind == 0:
            return f"{running}, current with {self.branch}"
        commits = "commit" if self.commits_behind == 1 else "commits"
        return (
            f"{running}, {self.commits_behind} {commits} behind {self.branch} "
            f"({self.target[:12]})"
        )


def _git(repo_root: Path, *args: str, timeout: float = _LOCAL_TIMEOUT) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def head(repo_root: Path = REPO_ROOT) -> str | None:
    """The commit checked out at `repo_root`, or None when git cannot say.

    Never raises: a checkout without git, or a tarball deploy with no repo at all,
    degrades to a null the payload carries rather than an endpoint that 500s. A
    health check that dies on its own reporting is worse than one axis missing.
    """
    return _git(repo_root, "rev-parse", "HEAD")


def measure(
    deployed: str | None,
    repo_root: Path = REPO_ROOT,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    fetch: bool = True,
) -> Drift:
    """How far `deployed` is behind `remote/branch`, measured from `repo_root`."""
    tip = f"{remote}/{branch}"
    if not deployed:
        return Drift(None, None, None, "the health payload names no commit", tip)
    if not _SHA.match(deployed):
        return Drift(deployed, None, None, f"{deployed!r} is not a commit id", tip)
    # `FETCH_HEAD` rather than `refs/remotes/<remote>/<branch>`: it is written by the
    # fetch we just ran, so it holds the tip as of a second ago whatever refspec this
    # clone happens to be configured with.
    if fetch and _git(repo_root, "fetch", "--quiet", remote, branch,
                      timeout=_FETCH_TIMEOUT) is None:
        return Drift(deployed, None, None, f"{remote} is unreachable", tip)
    target = _git(repo_root, "rev-parse", "FETCH_HEAD")
    if target is None:
        return Drift(deployed, None, None, f"{tip} is unknown to this clone", tip)
    behind = _git(repo_root, "rev-list", "--count", f"{deployed}..{target}")
    if behind is None:
        # The reported commit is not an object here: a clone that has not fetched it,
        # or a host serving code from somewhere this repo cannot see.
        return Drift(deployed, target, None, f"{deployed[:12]} is not in this clone", tip)
    return Drift(deployed, target, int(behind), None, tip)
