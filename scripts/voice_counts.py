"""The counts behind `club-voice.skill.md`, per reply and per typed occurrence (STU-1138).

`make voice-counts`. Reads the de-identified sent replies and prints one row per form the
skill counts, with the blocks that separate the two figures named underneath — a rule's
author should not have to dump contexts by hand to see that forty of forty-two replies
carry one pasted registration line.
"""

import argparse
import json
from pathlib import Path

from email_classifier.voice_counts import PASTE, counts

SHOWN_BLOCKS = 5


def table(measured: dict[str, dict], replies: int) -> str:
    lines = [f"{replies} non-empty sent replies, blocks joined at {PASTE} shared characters",
             "",
             f"{'form':26} {'replies':>8} {'occurrences':>12} {'distinct':>9} {'in replies':>11}"]
    for name, row in measured.items():
        lines.append(f"{name:26} {row['replies']:8} {row['occurrences']:12} "
                     f"{row['distinct']:9} {row['distinct_replies']:11}")
        # The largest few only: `vouvoiement` alone collapses forty-odd blocks, and a
        # table nobody reads to the end is the hand-dump this exists to replace. `--json`
        # carries all of them.
        for block in row["pasted"][:SHOWN_BLOCKS]:
            lines.append(f"    pasted x{block['occurrences']} in {block['replies']} replies: "
                         f"{block['sample']}")
        if len(row["pasted"]) > SHOWN_BLOCKS:
            lines.append(f"    ... and {len(row['pasted']) - SHOWN_BLOCKS} more blocks")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sent", type=Path,
                        default=Path("dumps/olm-corpus-elements-envoyes.json"))
    parser.add_argument("--json", action="store_true", help="the figures, unformatted")
    args = parser.parse_args()

    corpus = json.loads(args.sent.read_text(encoding="utf-8"))
    replies = [message["body_snippet"] for message in corpus
               if (message.get("body_snippet") or "").strip()]
    if not replies:
        raise SystemExit(f"no non-empty reply in {args.sent}")

    measured = counts(replies)
    if args.json:
        print(json.dumps({"replies": len(replies), "paste_threshold": PASTE,
                          "forms": measured}, ensure_ascii=False, indent=2))
    else:
        print(table(measured, len(replies)))


if __name__ == "__main__":
    main()
