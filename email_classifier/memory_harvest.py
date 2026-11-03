"""Answer blocks pasted across sent replies, proposed as club-memory facts (STU-2001).

The reviewer answers a recurring question with the same block every time (a list of
dates, a set of instructions, a referral), and the drafter has none of them: the block
lives only in Sent Items. `voice_counts` already tells a pasted block from a typed habit
(STU-1138); here the mark it anchors on is the start of every line, so the groups it forms
are the blocks themselves.

A block's text is the longest run two of its replies share from that line on, cut back to
the last whole line. A paste somebody edited halfway (a date moved to next season) still
yields the half nobody changed, and never a line cut mid-date.

Nothing reaches the store unread. `harvest` writes candidates; a human marks each one
`approve: true` or `false`, once for the set, and `approve` writes the approved ones
through `club_memory.remember`. Every decision, rejections included, is recorded with the
text it was made on, so the next harvest proposes only blocks that are new or whose text
moved since: a signature rejected today is not proposed again tomorrow.
"""

import json
import re
import unicodedata
from pathlib import Path

from email_classifier import club_memory
from email_classifier.voice_counts import PASTE, _shared, blocks, occurrences

CANDIDATES_NAME = "harvest-candidates.json"
DECISIONS_NAME = "harvest-decisions.json"

LINE_START = re.compile(r"(?:\A|\n)\s*(?=\S)")
_SLUG_WORDS = 6
_DESCRIPTION = 100


def _lines(text: str) -> str:
    """Each line stripped and blank runs collapsed: the layout a paste keeps, minus the
    trailing spaces and blank lines a mail client adds or drops."""
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in text.splitlines())).strip()


def _common(one: str, other: str) -> str:
    shared = _shared(one, other)
    if shared in (len(one), len(other)):
        return one[:shared]
    return one[:one.rfind("\n", 0, shared) + 1].rstrip()


def _block_text(tails: list[str]) -> str:
    ordered = sorted(tails)
    return max((_common(a, b) for a, b in zip(ordered, ordered[1:])), key=len, default="")


def find(replies: list[dict]) -> list[dict]:
    """The pasted blocks of `replies` (`{date, body}`), largest reach first."""
    bodies = [reply["body"] for reply in replies]
    found = occurrences(bodies, LINE_START)
    # `occurrences` walks the same bodies with the same pattern in the same order, so
    # this lines up with it one to one and gives back where each anchor sits.
    tails = [_lines(body[match.end():]) for body in bodies for match in LINE_START.finditer(body)]

    proposed = []
    for members in blocks(found):
        if len(members) < 2:
            continue
        text = _block_text([tails[index] for index in members])
        if len(text) < PASTE:
            continue
        # A member joined on its left context alone may not carry the text at all.
        carrying = {found[i].reply for i in members if tails[i].startswith(text)}
        dates = sorted(replies[reply]["date"][:10] for reply in carrying)
        proposed.append({"text": text, "replies": len(carrying),
                         "first_sent": dates[0], "last_sent": dates[-1]})
    # Every line inside a block anchors a group of its own, whose text is the block's tail.
    texts = [block["text"] for block in proposed]
    return sorted(
        (block for block in proposed
         if not any(block["text"] in other and block["text"] != other for other in texts)),
        key=lambda block: (-block["replies"], block["first_sent"]),
    )


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    return "-".join(words[:_SLUG_WORDS]) or "block"


def _named(found: list[dict]) -> list[dict]:
    taken: dict[str, int] = {}
    named = []
    for block in found:
        base = _slug(block["text"])
        taken[base] = taken.get(base, 0) + 1
        name = base if taken[base] == 1 else f"{base}-{taken[base]}"
        named.append({"name": name, **block})
    return named


def _load(path: Path) -> dict | list:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def candidates(found: list[dict], decisions: dict[str, dict]) -> list[dict]:
    """The blocks a human has not yet decided on as they read today.

    A block is known by its name, the slug of its first words, so a paste whose dates
    moved comes back `changed` rather than as a stranger.
    """
    out = []
    for block in _named(found):
        decided = decisions.get(block["name"])
        if decided and decided["text"] == block["text"]:
            continue
        out.append({
            "name": block["name"],
            "status": "changed" if decided else "new",
            "approve": None,
            "description": block["text"].splitlines()[0][:_DESCRIPTION],
            "replies": block["replies"],
            "first_sent": block["first_sent"],
            "last_sent": block["last_sent"],
            "text": block["text"],
            "fact": block["text"],
        })
    return out


def harvest(replies: list[dict], directory: Path) -> list[dict]:
    proposed = candidates(find(replies), _load(directory / DECISIONS_NAME))
    path = directory / CANDIDATES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return proposed


def approve(directory: Path) -> list[str]:
    """Write the approved candidates into the store; record every decision.

    Refuses while any candidate is undecided: approving "the set" means a decision on
    every block in it, and a default either way would be a decision nobody made.
    """
    path = directory / CANDIDATES_NAME
    proposed = _load(path)
    if not proposed:
        raise ValueError(f"no candidates in {path}: run the harvest first")
    undecided = [c["name"] for c in proposed if not isinstance(c.get("approve"), bool)]
    if undecided:
        raise ValueError(f"set approve to true or false on: {', '.join(undecided)}")

    decisions = _load(directory / DECISIONS_NAME)
    written = []
    for candidate in proposed:
        if candidate["approve"]:
            club_memory.remember(
                directory, candidate["name"], candidate["description"],
                f"pasted in {candidate['replies']} sent replies, "
                f"{candidate['first_sent']} to {candidate['last_sent']}",
                candidate["fact"],
            )
            written.append(candidate["name"])
        decisions[candidate["name"]] = {
            key: candidate[key] for key in ("text", "approve", "first_sent", "last_sent")}
    (directory / DECISIONS_NAME).write_text(
        json.dumps(decisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.unlink()
    return written
