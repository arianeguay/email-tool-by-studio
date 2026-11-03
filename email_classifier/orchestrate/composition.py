"""Inbox composition of a run (STU-404): how much of the mail rules can reach at all.

`ratio_deterministic` is bounded by the mailbox, not by rule quality. A message from a
one-off sender with no `List-Unsubscribe` carries no deterministic signal: it cannot be
*proven* important, only judged, so it escalates by design. Counting those separates a
rules gap from a mailbox that simply has little addressable mail, and makes a run's
ratio comparable to another mailbox's.

Pure module — no I/O, no LLM.
"""

from collections import Counter

from email_classifier.core.types import EmailInput
from email_classifier.orchestrate.report import EmailVerdict

RECURRING_MIN = 2  # a sender seen twice can carry a rule; a one-off cannot


def _has_unsubscribe(email: EmailInput) -> bool:
    return any(name.lower() == "list-unsubscribe" for name in email.headers)


def measure(emails: list[EmailInput], verdicts: list[EmailVerdict]) -> dict:
    """Split a run into what rules can reach and what they structurally cannot.

    `addressable` is the union of what rules already decided, what a universal
    archive rule could reach (`List-Unsubscribe`), and what a sender rule could reach
    (a recurring sender) — the ceiling `deterministic` can grow to on this run.
    """
    seen = Counter(email.from_address.lower() for email in emails)
    unsubscribable = {e.message_id for e in emails if _has_unsubscribe(e)}
    recurring = {e.message_id for e in emails if seen[e.from_address.lower()] >= RECURRING_MIN}
    decided = {v.message_id for v in verdicts if v.path == "deterministic"}
    addressable = decided | unsubscribable | recurring
    return {
        "unsubscribable": len(unsubscribable),
        "recurring": len(recurring),
        "addressable": len(addressable),
        "ratio_addressable": (len(decided) / len(addressable)) if addressable else 0.0,
    }
