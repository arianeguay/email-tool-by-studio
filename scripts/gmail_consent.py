#!/usr/bin/env python3
"""Consent to the Gmail scopes and cache the token (STU-948).

Usage:
    python scripts/gmail_consent.py           # gmail.modify, secrets/token.json
    python scripts/gmail_consent.py --sent    # gmail.readonly, secrets/sent-token.json

Opens the Google sign-in page and caches the resulting token in
secrets/token.json (overridable via GMAIL_TOKEN_FILE). This is the recovery a
`GrantLost` names: a headless caller — the webhook — refuses to open a consent
server itself, so restoring a revoked or expired grant is a human running this
on the host. Safe to re-run.

`--sent` grants the readonly token a second mailbox's Sent folder is read with
(STU-2108), cached in secrets/sent-token.json (overridable via
GMAIL_SENT_TOKEN_FILE).
"""

import sys

from email_classifier.fetch import gmail


def main() -> int:
    if "--sent" in sys.argv[1:]:
        creds, scopes = gmail.get_sent_credentials(), gmail.SENT_SCOPES
    else:
        creds, scopes = gmail.get_credentials(), gmail.SCOPES
    print(f"Granted: {', '.join(sorted(creds.scopes or scopes))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
