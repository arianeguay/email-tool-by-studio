from dataclasses import dataclass


@dataclass
class EmailInput:
    """Provider-neutral email contract — the boundary between any fetch and all
    downstream stages. No field may name or assume a specific mail provider.
    A Gmail fetch and a future Outlook/Graph fetch must both produce this shape.
    """

    message_id: str  # stable, unique per message — the unit of processing/dedup
    thread_id: str   # conversation-grouping context; NOT unique per message
    from_address: str
    subject: str
    headers: dict[str, str]
    body_snippet: str
    date: str  # ISO 8601 (e.g. "2026-06-15T12:00:00+00:00")
    # Provider's own coarse category, if any (Gmail tab, Outlook Focused/Other).
    # Low-weight, noisy hint only — never a structural classification feature.
    provider_category: str | None = None


@dataclass
class RuleMatch:
    rule_id: str
    action: str
    action_original: str | None
    confidence: str
    bucket: str | None
    # When the rule's `expires_after` runs out, counted from the message's date (STU-2070).
    expires_at: str = ""


@dataclass
class Classification:
    """Frozen classification contract (figé le 14 juin) — étage 2 output.

    The 7 fields are exactly the contract; do not add provider-specific fields.
    """

    thread_id: str
    bucket: str                  # "A" | "B" | "C" | "D"
    subtype: str
    confidence: str              # "high" | "medium" | "low"
    suggested_action: str        # keep_visible | label_archive | mark_for_delete | needs_reply | escalate_to_llm
    suggested_label: str | None
    reasoning: str
