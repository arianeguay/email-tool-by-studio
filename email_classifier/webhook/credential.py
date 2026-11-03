"""When the app-only client secret dies, and whether anyone has been told (STU-1095).

The `Email-Drafter` secret STU-1072 put the webhook on expires on a date fixed a year
in advance. Nothing in the repo knew that date existed, so the first symptom would have
been token requests failing: no notifications, no classification, and the same silence
a deleted subscription produces. `scripts/check_webhook_health.py` is where that gets
announced, because it is the one job on the host whose purpose is to break a silence.

Two things separate this from a lost grant (STU-930) or a dead subscription (STU-931).
The date is known ahead, so it can be announced *before* it bites. And it cannot be
renewed unattended — it needs a person in the Entra console — so a warning that arrives
the morning it expires is a warning that arrived too late. Hence a window measured in
weeks rather than an alert on the failure.

Configuration, not Graph. The app-only token carries no secret expiry, and reading it
off the application object needs `Application.Read.All` — a tenant-wide permission this
app should not hold to learn one date.

Two rules the module keeps:

* **No secret, no expiry.** The public-client flow has no client secret to expire, so
  there is nothing here to know and nothing to warn about. Without that gate every
  staging host and every worktree — none of which set a secret — would alert forever
  about a credential they do not have.
* **An unread date is never a pass.** Unset, or set to something that is not a date, is
  reported under its own code rather than treated as "not expiring soon", for the reason
  STU-1087 refused to guess a port: a probe that was not told when the credential dies
  has to say so, not answer as though it knew.
"""

import dataclasses
import os
from datetime import date

WARN_WITHIN_DAYS = 30


@dataclasses.dataclass(frozen=True)
class Expiry:
    """When the client secret expires, or why that could not be read.

    `days_left` is None exactly when `unknown` is set, and the two must not be read as
    one: "nobody said" repairs by setting the variable, "expires in 12 days" repairs in
    the Entra console.
    """

    expires_on: date | None
    days_left: int | None
    unknown: str | None = None

    @property
    def soon(self) -> bool:
        """Whether the expiry is close enough that someone has to act on it now.

        Past counts: an expired secret is the outage itself, and the repair is the
        same visit to the console.
        """
        return self.days_left is not None and self.days_left <= WARN_WITHIN_DAYS

    def summary(self) -> str:
        if self.unknown:
            return f"credential expiry unknown: {self.unknown}"
        if self.days_left < 0:
            days = -self.days_left
            plural = "day" if days == 1 else "days"
            return f"the client secret expired {days} {plural} ago, on {self.expires_on}"
        if self.days_left == 0:
            return f"the client secret expires today, {self.expires_on}"
        plural = "day" if self.days_left == 1 else "days"
        return (
            f"the client secret expires in {self.days_left} {plural}, "
            f"on {self.expires_on}"
        )


def measure(today: date | None = None) -> Expiry | None:
    """The credential's expiry, or None when this environment has no secret at all.

    None rather than an `Expiry` carrying a flag: "there is no client secret" is not a
    state of the expiry, it is the absence of the question. The caller skips the leg.
    """
    if not os.environ.get("OUTLOOK_CLIENT_SECRET"):
        return None
    # Empty counts as unset the way it does in `address.configured_health_url`:
    # .env.example carries keys empty and Make exports them that way.
    raw = os.environ.get("OUTLOOK_SECRET_EXPIRES", "").strip()
    if not raw:
        return Expiry(None, None, (
            "OUTLOOK_CLIENT_SECRET is set but OUTLOOK_SECRET_EXPIRES is not, so "
            "nothing says when this credential dies. Set it to the expiry shown on "
            "the app registration's secret, as YYYY-MM-DD."
        ))
    try:
        expires_on = date.fromisoformat(raw)
    except ValueError:
        return Expiry(None, None, (
            f"OUTLOOK_SECRET_EXPIRES is {raw!r}, which is not an ISO date "
            "(YYYY-MM-DD), so the expiry could not be read."
        ))
    return Expiry(expires_on, (expires_on - (today or date.today())).days)
