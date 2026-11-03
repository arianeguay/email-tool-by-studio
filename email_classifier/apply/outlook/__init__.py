"""Outlook (Microsoft Graph) write provider (STU-847, drafts STU-850).

Public API of the provider, mirroring `apply/gmail/`. `writer` and `drafts` are
internal modules; consumers import from here.
"""

from email_classifier.apply.outlook.drafts import OutlookDrafter
from email_classifier.apply.outlook.writer import OutlookWriter

__all__ = ["OutlookWriter", "OutlookDrafter"]
