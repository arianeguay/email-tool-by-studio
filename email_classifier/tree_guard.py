"""Which tree a `--check` answers for, and the refusal when that is two of them (STU-1109).

A check script derives a fingerprint now and compares it against a record committed in
its own tree. Run bare from a worktree the two halves come from different trees: the
script and the record are the worktree's, while `email_classifier` resolves through the
editable install to the main checkout, so everything derived from the package is main's.

Measured 2026-08-30 from the STU-1106 worktree, same tree, same second: bare,
`observe_spans.py --check` reported the curated keep-set moved and exited 3; under
`PYTHONPATH=$PWD` it reported unchanged. The drift was main's `_ROLES` (STU-1103, merged
mid-session) read against the worktree's record, and nothing had moved.

Both directions are reachable and the quiet one is worse. A false red costs a diagnostic
detour, and in a family of checks whose whole value is being believed, a red that is
routinely wrong is a red nobody reads. A false green — a worktree whose record is stale
but whose main checkout still agrees with it — is the silence STU-1047 exists to break.

So a check that cannot say which tree it is answering for refuses under its own code,
the way STU-1027 and STU-1087 refuse rather than composing a default and then blaming
what is not there. The message names both paths and the repair, because the interface
this repo documents is the one that fails: `make` exports `PYTHONPATH` (STU-867), and
STU-1091 made the bare call the way to read a 3 apart from a 2, which `make` flattens.

Living in the package is what makes this one statement rather than three, and it costs
one window: a worktree carrying these scripts over a main checkout that predates them
raises `ImportError: cannot import name 'tree_guard'` instead of exiting 5. That is the
same refusal spelled less well — it names both files, it is loud, and it closes the
first time the main checkout pulls. Three inlined copies of the message to close a
one-merge window is the worse trade.

`MIXED_TREES` is 5 for the reason every code in this family got its own number: 4 is
already `club_facts.py`'s unreadable page, and a condition folded into another is a
condition that goes unnoticed behind it.
"""

from pathlib import Path

MIXED_TREES = 5


def package_tree() -> Path:
    """The checkout `email_classifier` was imported from, whichever one that is.

    This module sits inside the package, so its own parents[1] is the same directory
    `Path(email_classifier.__file__).parents[1]` names, without the import.
    """
    return Path(__file__).resolve().parents[1]


def mismatch(script_tree: Path, package: Path | None = None) -> str | None:
    """The refusal when the script and the package come from two trees, else None."""
    script_tree = Path(script_tree).resolve()
    package = Path(package).resolve() if package else package_tree()
    if script_tree == package:
        return None
    return (
        f"refusing: this script is {script_tree}, but email_classifier is {package}. "
        "A --check would compare one tree's record against the other tree's code. "
        f"Re-run with PYTHONPATH={script_tree}"
    )
