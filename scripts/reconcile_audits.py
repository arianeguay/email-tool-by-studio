#!/usr/bin/env python3
"""Clear the host's copy of a committed audit file when the pull already carries it (STU-1081).

`make audit-skip` runs on the host, because that is where the report lives, and nothing
commits it there; the same entry is then committed from the workstation. The host is left
holding an uncommitted change that is textually the merge's own content, and `git pull`
refuses it. `git checkout -- audits/skipped.json` clears it, and is exactly the command an
operator should not run on trust — so this asks the question first: the working copy is
discarded only when every entry it holds is already in the version being merged.

Refusing is the direction of failure everywhere here. An entry the incoming version does
not carry, a same-run entry whose text differs, a file that is not a readable entry list —
each stops the pull and names the file, rather than being read as nothing-to-lose.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from email_classifier.audit import is_unreviewed

# What `pull-webhook` merges after this runs, so it is what the comparison must be against:
# fetching a second time there would compare one upstream state and merge another.
FETCHED = "FETCH_HEAD"

AUDITS = "audits"


def _git(*args: str, repo: Path) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode:
        sys.exit(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


def _entries(text: str) -> list[dict] | None:
    """The entry list, or None when the file is not one."""
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, list) or not all(isinstance(e, dict) for e in loaded):
        return None
    return loaded


def _canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, ensure_ascii=False)


def lost_entries(local_text: str, incoming_text: str) -> list[dict] | None:
    """The local entries the incoming version does not carry, or None if unanswerable.

    Whole entries and not run ids: a host that wrote a reason the commit edited would
    otherwise read as carried, and the reason is the audit.

    An entry the run filed for itself and nobody has read is not lost work (STU-1084):
    it is derived from a report still on disk, and `audit-file` writes it back. Since
    filing landed the host holds one of those per notification batch, so counting them
    would make this refuse every pull whose merge also touches the log — which is every
    pull that carries the verdicts a reviewer just wrote, the routine this exists to
    let happen. What a human wrote still refuses; nothing regenerates that.
    """
    local, incoming = _entries(local_text), _entries(incoming_text)
    if local is None or incoming is None:
        return None
    carried = {_canonical(e) for e in incoming}
    return [e for e in local
            if _canonical(e) not in carried and not is_unreviewed(e)]


def _blocking_paths(repo: Path) -> list[str]:
    """Audit files dirty here that the merge also rewrites — the ones the pull aborts on.

    A dirty file the merge does not touch is a local change nothing threatens, and
    discarding it would be the blanket `checkout` this exists to avoid.
    """
    dirty = set(_git("diff", "--name-only", "HEAD", "--", AUDITS, repo=repo).splitlines())
    incoming = set(_git("diff", "--name-only", "HEAD", FETCHED, "--", AUDITS,
                        repo=repo).splitlines())
    return sorted(dirty & incoming)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    repo = parser.parse_args(argv).repo
    _git("fetch", repo=repo)

    refused = False
    for rel in _blocking_paths(repo):
        lost = lost_entries((repo / rel).read_text(encoding="utf-8"),
                            _git("show", f"{FETCHED}:{rel}", repo=repo))
        if lost is None:
            print(f"[ERROR] {rel} is not a readable entry list on one side — "
                  f"leaving it for a human", file=sys.stderr)
            refused = True
        elif lost:
            named = "\n".join(f"          {e.get('run_id') or '<no run_id>'}" for e in lost)
            print(f"[ERROR] {rel} holds entries the pull does not carry ({len(lost)}):\n"
                  f"{named}\n"
                  f"        Commit them before pulling — they would be lost.",
                  file=sys.stderr)
            refused = True
        else:
            _git("checkout", "--", rel, repo=repo)
            print(f"[reconcile] {rel}: every local entry is in the incoming version — "
                  f"discarded the working copy")
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
