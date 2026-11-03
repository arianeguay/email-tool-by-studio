"""The failure no provider can retry its way out of (STU-930, STU-948).

Provider-neutral because the webhook catches it once, for whichever provider it
was configured with — see `email_classifier.webhook.server`.
"""


class GrantLost(RuntimeError):
    """The cached grant no longer authenticates and only a human can restore it.

    A Graph refresh token dies after 90 days of inactivity, on a password change
    or on an MFA reset; a Gmail one on revocation or on a test-mode app's 7-day
    expiry. Recovery is an interactive consent, so a headless caller cannot retry
    its way out: this is raised to be caught once, alerted on and stopped for,
    not swallowed alongside a transient error.
    """
