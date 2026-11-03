#!/usr/bin/env python3
"""Refuse a commit that carries a deployment's real data (STU-2078).

    python scripts/check_real_data.py --staged          # the pre-commit hook
    python scripts/check_real_data.py --message FILE    # the commit-msg hook

This repository is public, and a pushed commit stays reachable by SHA after a squash
(STU-2077). The terms it guards cannot live here, since they are the data being
protected: they come from files the deployments own, listed one path per line in
`$EMAIL_CLASSIFIER_REAL_DATA` (default `~/.config/email-classifier/real-data`). A
`.json` source is a keymap and gives its values; any other file gives one term per
line, `#` lines ignored. A config line `!term` drops that term: a keymap also holds
detector false positives that name nobody (one held the model name every commit
trailer carries).

A hit names the file, the line and the source entry (a keymap token, or the line of a
list), never the term. Fails closed: an installed hook with no config, an unreadable
source or no term at all exits 2, since a guard that read nothing would pass anything.
Stdlib only, so the hook runs under any `python3`.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

MIN_LENGTH = 3
DEFAULT_CONFIG = Path.home() / ".config" / "email-classifier" / "real-data"


class Unusable(Exception):
    pass


def config_path() -> Path:
    return Path(os.environ.get("EMAIL_CLASSIFIER_REAL_DATA") or DEFAULT_CONFIG)


def load_terms(config: Path) -> dict[str, str]:
    """Map each term to the label a hit is reported under."""
    try:
        lines = config.read_text().splitlines()
    except OSError as e:
        raise Unusable(f"cannot read {config} ({e.strerror}); list the deployments' keymaps and term lists there") from e
    terms: dict[str, str] = {}
    ignored = {line.strip()[1:].strip() for line in lines if line.strip().startswith("!")}
    for raw in lines:
        entry = raw.strip()
        if not entry or entry.startswith(("#", "!")):
            continue
        source = Path(entry).expanduser()
        try:
            text = source.read_text()
        except OSError as e:
            raise Unusable(f"cannot read {source}, named in {config} ({e.strerror})") from e
        if source.suffix == ".json":
            try:
                pairs = [(str(v), f"{source.name}:{k}") for k, v in json.loads(text).items()]
            except (ValueError, AttributeError) as e:
                raise Unusable(f"{source} is not a keymap object") from e
        else:
            pairs = [
                (line.strip(), f"{source.name}:{n}")
                for n, line in enumerate(text.splitlines(), 1)
                if line.strip() and not line.strip().startswith("#")
            ]
        for term, label in pairs:
            if len(term.strip()) >= MIN_LENGTH and term.strip() not in ignored:
                terms.setdefault(term.strip(), label)
    if not terms:
        raise Unusable(f"no term found through {config}; a guard with nothing to match passes everything")
    return terms


def matcher(terms: dict[str, str]) -> re.Pattern:
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)")


def scan_lines(lines, pattern: re.Pattern, terms: dict[str, str]) -> list[tuple[str, int, str]]:
    """`lines` yields (path, line number, text); returns (path, line, label) per hit."""
    return [
        (path, number, terms[m.group(0)])
        for path, number, text in lines
        for m in pattern.finditer(text)
    ]


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def added_lines(diff: str):
    path, number = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif (m := _HUNK.match(line)):
            number = int(m.group(1))
        elif line.startswith("+") and path is not None:
            yield path, number, line[1:]
            number += 1


def staged_diff() -> str:
    return subprocess.run(
        ["git", "diff", "--cached", "-U0", "--no-color", "--no-ext-diff"],
        check=True, capture_output=True, text=True,
    ).stdout


def message_lines(path: Path):
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.startswith("#"):
            yield "commit message", n, line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true")
    group.add_argument("--message", type=Path)
    args = parser.parse_args(argv)
    try:
        terms = load_terms(config_path())
    except Unusable as e:
        print(f"check_real_data: {e}", file=sys.stderr)
        return 2
    lines = added_lines(staged_diff()) if args.staged else message_lines(args.message)
    hits = scan_lines(lines, matcher(terms), terms)
    for path, number, label in hits:
        print(f"{path}:{number}: carries real data ({label})", file=sys.stderr)
    if hits:
        print("check_real_data: replace it with an invented value; this repository is public.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
