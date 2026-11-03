"""Which Studio version ran a QA measurement (STU-1241).

STU-1175 recorded which provider ran a measurement, by the run, never re-derived from
whatever tree an audit happens to be sitting in. The Studio version is the other half of
"what produced it," and no artifact carried it: `CLAUDE.md` stated this laptop had run
0.17.1 since 2026-09-01 while `cli/dist/index.js` had in fact been rebuilt on 0.18.0, and
measurements taken since named their version only in prose, or in a Linear comment
someone wrote after checking by hand.

Read from `studio --version` at run time, never from a pin or a doc — `CLAUDE.local.md`
is why: the installed `studio` is a pnpm shim onto a local checkout, so the version moves
with a `git pull` nobody announces. And `--version` reads `cli/package.json`, which says
nothing about whether `dist/` was actually built from it — recording what it printed is
still right, since that is what the run was told, but the field's name must not promise
more than that: it is what `studio --version` answered, not a verified build identity.

Not a fingerprinted `sources` leg, for the reason `qa_provider` gives its own field: a
leg is something a bare tree rebuilds, and the Studio version is a property of the run's
host, not of this repo. Making it a leg would turn every host difference into a red push
no tree carries the repair for. `check` reports it and never compares it.
"""

import re
import subprocess
from pathlib import Path

KEY = "studio_version"


def resolve(cwd: Path | None = None) -> str | None:
    """The `studio --version` string this run saw, or None when it could not be read.

    None only where the tree could not answer — no `studio` on PATH, a timeout, a
    non-zero exit, output with nothing that looks like a version. Recorded as null
    rather than guessed at, by STU-912's direction: a measurement nobody could take must
    not read as one that came back empty.
    """
    try:
        proc = subprocess.run(
            ["studio", "--version"], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    match = re.search(r"\d+\.\d+\.\d+", proc.stdout)
    return match.group(0) if match else None


def line(version: str | None) -> str:
    """The summary's Studio-version sentence, worded once for all five harnesses."""
    if version:
        return f"Ran on Studio `{version}` (from `studio --version`)."
    return (
        "Ran on a Studio version this tree cannot name: `studio --version` did not "
        "resolve. What ran is unrecorded, which is not the same as nothing having run."
    )
