"""The drafter's placeholder for a fact no source holds (STU-2002, STU-2049).

A draft is plain text, so the body marker is the only signal that it needs a human's
input before it goes out. `holds_placeholder` is the one test for it: the Outlook
drafter reads it to mark the draft, and `audit_run.py reviewed` to catch a sent reply
that still carries one.
"""

import unicodedata

from email_classifier import alerts

PLACEHOLDER_MARKER = "[À COMPLÉTER"
PLACEHOLDER_CATEGORY = "ec/à-compléter"


def holds_placeholder(body: str) -> bool:
    # NFC: a body that went through a mail client may carry the accents decomposed.
    return PLACEHOLDER_MARKER in unicodedata.normalize("NFC", body or "")


def flag_sent(sent_bodies: dict[str, str]) -> list[str]:
    """Conversations whose sent reply still holds a placeholder, alerted once for all."""
    flagged = sorted(t for t, body in sent_bodies.items() if holds_placeholder(body))
    if flagged:
        alerts.notify(f"{len(flagged)} sent reply(ies) still carry {PLACEHOLDER_MARKER}: "
                      f"{', '.join(flagged)}")
    return flagged
