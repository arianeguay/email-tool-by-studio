#!/usr/bin/env python3
"""Consent to the Gmail scopes and cache the token (STU-948).

Usage:
    python scripts/gmail_consent.py

Opens the Google sign-in page and caches the resulting token in
secrets/token.json (overridable via GMAIL_TOKEN_FILE). This is the recovery a
`GrantLost` names: a headless caller — the webhook — refuses to open a consent
server itself, so restoring a revoked or expired grant is a human running this
on the host. Safe to re-run.
"""

from email_classifier.fetch import gmail


def main() -> int:
    creds = gmail.get_credentials()
    print(f"Granted: {', '.join(sorted(creds.scopes or gmail.SCOPES))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
