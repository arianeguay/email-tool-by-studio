"""Gmail write provider (STU-405, drafts STU-853).

Public API of the provider, mirroring `apply/outlook/`. `writer` and `drafts`
are internal modules; consumers import from here.
"""

from email_classifier.apply.gmail.drafts import GmailDrafter
from email_classifier.apply.gmail.writer import GmailWriter

__all__ = ["GmailWriter", "GmailDrafter"]
