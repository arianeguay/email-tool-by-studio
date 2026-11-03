"""Header allowlist shared by every fetch projection (STU-325).

Which raw headers survive into the neutral EmailInput is a property of the
classifier, not of a provider: Stage 1 rules match on these names, so Gmail and
Outlook must project exactly the same set.

Canonical casing is load-bearing: `header_present` does a case-sensitive key
lookup (rule_engine.py) and providers return header names with arbitrary
casing, so incoming names are canonicalized to these spellings.
"""

HEADER_ALLOWLIST = (
    "List-Unsubscribe",
    "List-Unsubscribe-Post",
    "Precedence",
    "Auto-Submitted",
    "X-Mailer",
)

_BY_LOWER = {name.lower(): name for name in HEADER_ALLOWLIST}


def allowlisted(headers: list[dict]) -> dict[str, str]:
    """Keep only allowlisted `{"name", "value"}` pairs, canonicalizing the names."""
    result: dict[str, str] = {}
    for header in headers:
        canonical = _BY_LOWER.get(header["name"].lower())
        if canonical is not None:
            result[canonical] = header["value"]
    return result
