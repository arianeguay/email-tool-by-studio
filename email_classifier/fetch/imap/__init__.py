"""IMAP provider (STU-2054): any standards mailbox, Migadu first."""

from email_classifier.fetch.imap.client import ImapFetcher, Mailbox
from email_classifier.fetch.imap.session import ImapSession, build_session, configured_mailbox

__all__ = ["ImapFetcher", "ImapSession", "Mailbox", "build_session", "configured_mailbox"]
