"""`mail_precedents-search_mail` (STU-872): how the club answered this before.

The site says what the club does; the mailbox says how the club says it. The same
question has been answered before, and that earlier reply is a better template
than anything a model invents — but only for the wording. A precedent never
licenses a fact: what is true is still the skill's and the site's to say.

The hits are third-party mail from parents and about minors, so nothing leaves
this module in the clear. Every body goes through `anonymize.py` first, and what
its detector misses is caught by the second pass: a capitalized word the club
skill does not use is redacted on sight. Both passes end in an index-free
placeholder, because a precedent is read and never restored: `write_draft.py` would
give an indexed token back the wrong person's real name.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from email_classifier import deployment
from email_classifier.anonymize import CAPITALIZED_WORD, Tokenizer, vouched
from email_classifier.providers import get_precedent_searcher
from email_classifier.stage1.policy import load_policy

REPO_ROOT = deployment.ROOT

MATCH_LIMIT = 5
BODY_LIMIT = 1500

# `write_draft.py` restores the batch keymap's tokens on the way to the mailbox.
# A precedent's `PERSON_1` is a different person from the batch's `PERSON_1`, so
# an indexed token copied out of a precedent would come back as someone else's
# real name in a parent's reply. Flattened, it survives restoration untouched and
# reads as the placeholder it is.
_INDEXED_TOKEN = re.compile(r"\b([A-Z]+)_\d+\b")

# GLiNER catches the people; this pass catches what a person-detector is not asked
# to find — an organisation, a venue, a platform the club has never mentioned. The
# rule is inverted from the batch detector's: a capitalized word is redacted unless
# `anonymize.vouched()` accounts for it, so a gap costs phrasing and never a name —
# the safe direction for third-party mail about minors.
def redact(
    text: str, tokenizer: Tokenizer, vouched_words: frozenset[str] | None = None
) -> str:
    """The body with every value and every unvouched proper noun replaced.

    `NAME_X` is a proper noun the redactor could not vouch for — usually a person,
    sometimes an organisation it has never been told about. Either way it is not a
    word the draft may reuse.
    """
    words = vouched() if vouched_words is None else vouched_words
    text = _INDEXED_TOKEN.sub(r"\1_X", tokenizer.anonymize(text))
    return CAPITALIZED_WORD.sub(
        lambda m: m[0] if m[0].casefold() in words else "NAME_X", text
    )


def search(query: str, searcher, limit: int = MATCH_LIMIT) -> dict:
    """Prior mail on `query`, redacted before any of it can reach the LLM."""
    tokenizer = Tokenizer()
    words = vouched()
    return {
        "query": query,
        "matches": [
            {"date": hit["date"],
             "direction": hit["direction"],
             "body": redact(hit["body"], tokenizer, words)[:BODY_LIMIT]}
            for hit in searcher.search_messages(query, limit)
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--policy", default=str(REPO_ROOT / "rules/policy.yaml"))
    args = parser.parse_args()

    policy = load_policy(Path(args.policy))
    result = search(args.query, get_precedent_searcher(policy.email_provider))
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
