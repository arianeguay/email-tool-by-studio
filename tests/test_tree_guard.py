"""The refusal a `--check` owes when its two halves come from two trees (STU-1109).

Pure path logic over committed files, which is what makes this the one half of the
family that needs no mailbox, no archive and no detector to cover.
"""

import pytest

from email_classifier import tree_guard
from scripts import qa_gate as qa_gate_script

CHECKS = (qa_gate_script.main,)


def _ids(main):
    return main.__module__.rsplit(".", 1)[-1]


def test_one_tree_is_not_a_mismatch(tmp_path):
    assert tree_guard.mismatch(tmp_path, tmp_path) is None


def test_the_refusal_names_both_trees_and_the_repair(tmp_path):
    script, package = tmp_path / "worktree", tmp_path / "checkout"
    script.mkdir()
    package.mkdir()

    message = tree_guard.mismatch(script, package)

    assert str(script) in message
    assert str(package) in message
    # The repair, not just the diagnosis: STU-1091 made the bare call the documented
    # way to read a 3 apart from a 2, so the reader who hits this was told to.
    assert f"PYTHONPATH={script}" in message


@pytest.mark.parametrize("main", CHECKS, ids=_ids)
def test_a_check_refuses_rather_than_answering_for_the_wrong_tree(main, tmp_path, monkeypatch):
    monkeypatch.setattr(tree_guard, "package_tree", lambda: tmp_path)

    assert main(["--check"]) == tree_guard.MIXED_TREES


def test_the_tree_the_package_came_from_is_never_a_mismatch():
    """The guard answers the mixed pair and nothing else.

    The way this goes wrong quietly is a guard that also fires on the ordinary case:
    three distinct answers collapse back into one, which is the flattening STU-1091
    undid.
    """
    assert tree_guard.mismatch(tree_guard.package_tree()) is None
