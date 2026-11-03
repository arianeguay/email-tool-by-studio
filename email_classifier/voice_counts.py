"""What the club typed, as against what it pasted (STU-1138).

Every rule in `club-voice.skill.md` is a count over the sent replies, and the counting
unit was the reply: 42 replies put a non-breaking space before a colon, so the inbox
looked to have a minority habit worth a rule. It does not. 40 of those 42 carry the same
registration line, pasted; the form was chosen once, somewhere outside the archive, and
per-reply counting turned it into forty decisions.

So a count that claims a habit counts *occurrences nobody pasted*. Two occurrences in two
different replies are one block when the text around them repeats verbatim — not a fixed
window, however far the repetition actually runs, which is the only version of "the same
block" that survives a paste carrying a different link or a different season.

A block contributes nothing to the distinct count rather than one. Somebody typed it once,
but not in any of these replies, and the archive cannot say when or whether they chose it:
counting it as one occurrence puts boilerplate on the same footing as a form somebody
reached for here. The per-reply figure is reported beside it, so a rule's author reads
`42 replies, 2 distinct` and decides.

Pure by design: the corpus is read by the caller, so every judgement here is testable
without the archive.
"""

import re
from typing import NamedTuple

from email_classifier import voice_baseline

# Kept per side of an occurrence. Comfortably above PASTE, so two contexts stop agreeing
# because the texts diverge and never because the window ran out.
CONTEXT = 120

# Characters of identical surrounding text, both sides together, that make two
# occurrences in two replies one block. Seven words or so: French this close to identical
# around the same mark is a block, not two people reaching for the same phrasing.
#
# The finding does not rest on the number. Measured 2026-08-30 over the 831 non-empty sent
# replies, the non-breaking space before a colon reads 2 distinct against 42 replies at
# every threshold from 25 to 80; what the threshold moves is how much of the vouvoiement
# count sits inside pasted blocks (356 distinct at 25, 571 at 80), and no rule turns on
# that figure.
PASTE = 40

_WHITESPACE = re.compile(r"\s+")


def _compile_rules(raw: dict[str, str] | None) -> dict[str, re.Pattern]:
    """`RULES`, compiled from what the deployment states in `rules/voice.yaml`.

    An `(?i)` prefix is how a source string carries the case-insensitive flag across
    the YAML boundary -- Python only honours an inline flag placed at the very start
    of a pattern, which is where `rules/voice.yaml`'s comment says each one puts it.
    """
    return {name: re.compile(pattern) for name, pattern in (raw or {}).items()}


# The forms `club-voice.skill.md` counts as mechanics, moved out of this file into
# `rules/voice.yaml` (STU-1472): the patterns measure this deployment's own writing --
# French, this club's own closing forms -- so a different deployment replaces the file
# rather than editing the kernel. Empty rather than `None` on one with no baseline yet:
# `count`/`counts` then have nothing to count, which prints as a bare table, never as a
# claim of zero occurrences of a form nobody defined.
RULES = _compile_rules((voice_baseline.load() or {}).get("rules"))


class Occurrence(NamedTuple):
    """One match, with the context that decides whether anybody typed it.

    `left` is reversed so both sides are compared the same way — as a common prefix,
    outward from the mark.
    """

    reply: int
    left: str
    right: str


def _shared(one: str, other: str) -> int:
    for index, (a, b) in enumerate(zip(one, other)):
        if a != b:
            return index
    return min(len(one), len(other))


def occurrences(replies: list[str], pattern: re.Pattern) -> list[Occurrence]:
    # Twice CONTEXT of raw text per side, cut to CONTEXT after collapsing whitespace: a
    # reply laid out with a blank line between every sentence would otherwise hand the
    # comparison a context shorter than the window says.
    found = []
    for index, body in enumerate(replies):
        for match in pattern.finditer(body):
            left = _WHITESPACE.sub(" ", body[max(0, match.start() - 2 * CONTEXT):match.start()])
            right = _WHITESPACE.sub(" ", body[match.end():match.end() + 2 * CONTEXT])
            found.append(Occurrence(index, left[-CONTEXT:][::-1], right[:CONTEXT]))
    return found


def blocks(found: list[Occurrence]) -> list[list[int]]:
    """Occurrences grouped into the blocks they share, singletons included.

    Two occurrences in the *same* reply are never a block: a list repeating a form down
    one message is that message's writing, whoever composed it, and pairing them would
    read every enumeration as boilerplate.
    """
    parent = list(range(len(found)))

    def root(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for a in range(len(found)):
        for b in range(a + 1, len(found)):
            if found[a].reply == found[b].reply:
                continue
            if (_shared(found[a].left, found[b].left)
                    + _shared(found[a].right, found[b].right)) >= PASTE:
                parent[root(a)] = root(b)

    grouped: dict[int, list[int]] = {}
    for index in range(len(found)):
        grouped.setdefault(root(index), []).append(index)
    return sorted(grouped.values(), key=lambda members: (-len(members), members[0]))


def count(replies: list[str], pattern: re.Pattern) -> dict:
    """Both figures for one form, and the blocks that separate them.

    `pasted` carries a sample of each block so the reason is readable: STU-1131 caught the
    colon only by dumping the contexts and reading them, and a count that hides what it
    collapsed asks its reader to do that again.
    """
    found = occurrences(replies, pattern)
    grouped = blocks(found)
    typed = [members[0] for members in grouped if len(members) == 1]
    return {
        "replies": len({item.reply for item in found}),
        "occurrences": len(found),
        "distinct": len(typed),
        "distinct_replies": len({found[index].reply for index in typed}),
        "pasted": [
            {
                "occurrences": len(members),
                "replies": len({found[index].reply for index in members}),
                "sample": _sample(found[members[0]]),
            }
            for members in grouped
            if len(members) > 1
        ],
    }


def _sample(item: Occurrence) -> str:
    return f"{item.left[:60][::-1]}[…]{item.right[:60]}"


def counts(replies: list[str]) -> dict[str, dict]:
    return {name: count(replies, pattern) for name, pattern in RULES.items()}
