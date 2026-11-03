#!/usr/bin/env python3
"""Consent to the Graph read-write scopes for an Outlook mailbox (STU-844).

Usage:
    python scripts/outlook_consent.py

Opens the Microsoft sign-in page asking for `Mail.ReadWrite` explicitly and
caches the resulting token in secrets/outlook-token.json (overridable via
OUTLOOK_TOKEN_FILE), so `apply_email.py ... --apply` passes preflight instead of
403-ing partway through a batch. Safe to re-run: it re-consents an existing
read-only grant.

Delegated only. An app-only host is refused rather than walked through a sign-in
that grants it nothing: consent there is an admin action in Entra, and the token
this would cache is one no app-only call reads (STU-1072).
"""

import sys

from email_classifier.fetch import outlook


def main() -> int:
    if outlook.app_only():
        print(
            "[ERROR] OUTLOOK_CLIENT_SECRET is set, so this host authenticates as the "
            f"application against {outlook.configured_mailbox()}. There is nothing to "
            "consent to here: the application role is granted once by a tenant admin "
            "in Entra. Unset the secret to use the delegated flow.",
            file=sys.stderr,
        )
        return 1
    outlook.get_token_provider(scopes=outlook.WRITE_SCOPES)()
    refusal = outlook.preflight_write_scope()
    if refusal:
        print(f"[ERROR] {refusal}", file=sys.stderr)
        return 1
    print(f"Granted: {', '.join(sorted(outlook.granted_scopes()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
