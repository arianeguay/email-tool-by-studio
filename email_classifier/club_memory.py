"""`org_memory-*` (STU-873): what the club learns by mail, kept between runs.

The deployment's facts skill is the public site frozen on a day, and
`org_web-search_site` is that site right now. Neither holds a fact that only ever
travelled by mail — a gym finally assigned, a fee the board settled on, a date
confirmed in a meeting. Without somewhere to put it, the drafter re-derives it from
the next email that mentions it, or answers "we will confirm" forever.

Shaped like Claude Code's own memories: one fact per Markdown file, and a
`MEMORY.md` of one line each. The index is what loads into the prompt; a file is
read only when its line looks worth opening.

The skill and the live site outrank a memory. A memory that contradicts them is
flagged, not overwritten and not deleted — deletion is a human's call, and a
flagged line stays visible in the index until someone makes it.

Memories are stored exactly as the agent wrote them, tokens included: under
`scope: full` a source reads `PERSON_1`, and the keymap restores it at the mailbox
boundary like any other token. The directory is gitignored all the same.

Read back, the text *between* a memory's tokens goes through the tokenizer first
(STU-879). This is a store a human corrects, and a human writes real names —
nothing there may be trusted to already be anonymized just because an agent wrote
most of it. The tokens themselves are left alone: running the detector over one
re-mints it under a number the keymap does not know (STU-981).
"""

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import yaml

from email_classifier import deployment
from email_classifier.anonymize import Tokenizer, load_keymap, stay_on_the_cpu
from email_classifier.stage1.policy import load_policy

REPO_ROOT = deployment.ROOT
# The tool plugin passes no path — where memories live is the repo's, not the
# agent's. CLUB_MEMORY_DIR is how a test points the plugin somewhere disposable.
MEMORY_DIR = Path(os.environ.get("CLUB_MEMORY_DIR") or REPO_ROOT / "club-memory")
INDEX_NAME = "MEMORY.md"

# The agent picks the name, so it also picks the path — a slug is the whole guard.
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Grouped so `split` hands back the tokens along with the text between them.
_INDEXED_TOKEN = re.compile(r"(\b[A-Z]+_\d+\b)")


def _path(directory: Path, name: str) -> Path:
    if not _NAME.fullmatch(name):
        raise ValueError(f"a memory name is kebab-case letters and digits, not {name!r}")
    return directory / f"{name}.md"


def _parse(path: Path) -> tuple[dict, str]:
    _, frontmatter, body = path.read_text(encoding="utf-8").split("---\n", 2)
    return yaml.safe_load(frontmatter), body.strip()


def _write(path: Path, front: dict, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{header}---\n\n{body.strip()}\n", encoding="utf-8")


def entries(directory: Path) -> list[dict]:
    """The frontmatter of every memory, name order — what the index is built from."""
    if not directory.exists():
        return []
    return [_parse(path)[0]
            for path in sorted(directory.glob("*.md")) if path.name != INDEX_NAME]


def _line(front: dict) -> str:
    contradiction = front.get("contradicted")
    mark = "[contradicted] " if contradiction else ""
    note = f"; contradicted: {contradiction}" if contradiction else ""
    return (f"- {mark}[{front['name']}]({front['name']}.md) — {front['description']} "
            f"(learned {front['learned_on']} from {front['source']}{note})")


def write_index(directory: Path) -> str:
    index = "".join(f"{_line(front)}\n" for front in entries(directory))
    (directory / INDEX_NAME).write_text(index, encoding="utf-8")
    return index


def load_index(directory: Path) -> str:
    path = directory / INDEX_NAME
    return path.read_text(encoding="utf-8") if path.exists() else ""


def read(directory: Path, name: str) -> str:
    return _path(directory, name).read_text(encoding="utf-8")


def _rendered(text: str, keymap: dict[str, str]) -> str:
    """`text` with every token the batch keymap does not know stripped of its index.

    Minted in a fan-out child, `PERSON_5` would come back from restoration as the
    batch's PERSON_5, a different person, inside a parent's reply. Index-free, it
    survives restoration and reads as the placeholder it is.
    """
    return _INDEXED_TOKEN.sub(
        lambda m: m[0] if m[0] in keymap else f"{m[0].rsplit('_', 1)[0]}_X", text)


def for_prompt(text: str, keymap: dict[str, str]) -> str:
    """A memory as the LLM may see it: values tokenized against the batch keymap.

    Only the text *between* the tokens is tokenized. A memory the drafter wrote
    already reads `PERSON_1`, and running the detector over it reads that token as
    a name and mints a second one for it — which the keymap then does not know, so
    the source of the fact came back `PERSON_X` and named nobody (STU-981). A
    memory a human edited holds the real value in clear (STU-879), and that is what
    the pass over the gaps is still there to catch.
    """
    tokenizer = Tokenizer(keymap)
    return "".join(
        _rendered(piece if index % 2 else tokenizer.anonymize(piece), keymap)
        for index, piece in enumerate(_INDEXED_TOKEN.split(text))
    )


def remember(
    directory: Path,
    name: str,
    description: str,
    source: str,
    fact: str,
    learned_on: str | None = None,
) -> str:
    """Store one fact and rebuild the index. Writing a name twice replaces it."""
    _write(_path(directory, name), {
        "name": name,
        "description": description,
        "source": source,
        "learned_on": learned_on or date.today().isoformat(),
    }, fact)
    return write_index(directory)


def flag_contradicted(directory: Path, name: str, reason: str) -> str:
    """Mark a memory the skill or the site disagrees with, for a human to delete."""
    path = _path(directory, name)
    front, body = _parse(path)
    _write(path, {**front, "contradicted": reason}, body)
    return write_index(directory)


def main() -> None:
    # One tool call, one file, then exit: the weights never earn the trip to the GPU,
    # and the run that spawned this already has a copy of them there (STU-984).
    stay_on_the_cpu()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("read", "remember", "flag"))
    parser.add_argument("--directory", default=str(MEMORY_DIR))
    # The batch keymap, from the fan-out item — a read is redacted against it.
    parser.add_argument("--keymap", default="")
    parser.add_argument("--policy", default=str(REPO_ROOT / "rules/policy.yaml"))
    args = parser.parse_args()

    # Everything the agent wrote arrives as one JSON object on stdin: Studio
    # substitutes `{{param}}` into the shell command verbatim, so prose passed as
    # an argument would be read by the shell before it ever reached Python.
    payload = json.loads(sys.stdin.read() or "{}")
    directory = Path(args.directory)
    name = payload.get("name", "")

    if args.command == "read":
        content = read(directory, name)
        if load_policy(Path(args.policy)).anonymization_fields:
            content = for_prompt(content, load_keymap(Path(args.keymap)))
        result = {"name": name, "content": content}
    elif args.command == "flag":
        reason = payload["reason"]
        result = {"name": name, "contradicted": reason,
                  "index": flag_contradicted(directory, name, reason)}
    else:
        fact = (payload.get("fact") or "").strip()
        if not fact:
            raise SystemExit("[ERROR] refusing to store an empty memory")
        result = {"name": name,
                  "index": remember(directory, name, payload["description"],
                                    payload["source"], fact)}

    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
