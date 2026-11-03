import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from email_classifier.core.types import RuleMatch

_DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({"mark_for_delete"})

# Scope → fields sent to Stage 2 that must be tokenized before the LLM sees them.
# This mapping is app knowledge: Studio receives opaque field names and never learns
# what `body_only` or `full` mean. `body_only` fits a personal mailbox (implicit
# consent); `full` is the gate for third-party mailboxes (club members, parents,
# minors) whose PII never consented to reaching an external LLM.
# `body_text` is the drafting subset's full body (STU-852); it rides in the same
# scopes as the preview, so reading more of a mail never tokenizes less of it.
# `memory_index` is not the email but reaches the same prompt (STU-879), and a
# human editing a memory writes real names; it is in both scopes because a club
# fact is third-party either way. A stage whose item has no such field is
# unaffected — `anonymize_fields` skips a field the item does not carry.
ANONYMIZATION_SCOPES: dict[str, list[str]] = {
    "body_only": ["body_snippet", "body_text", "memory_index"],
    "full": ["from_address", "subject", "headers", "body_snippet", "body_text",
             "memory_index"],
}


SENT_SOURCES = {"gmail"}


@dataclass
class Policy:
    deterministic_can_finalize: bool
    destructive_actions_allowed: list[str]
    frontier_log_file: bool
    frontier_log_stdout: bool
    email_provider: str = "gmail"
    # Push-driven apply (STU-849). False keeps the webhook read-only: it classifies and
    # dry-run-plans. True only removes the manual trigger — what may be applied stays
    # bounded by deterministic_can_finalize and destructive_actions_allowed.
    auto_apply: bool = False
    # One status category per conversation on its latest inbound message (STU-2007),
    # written even while `auto_apply` is false: it moves nothing, and it is what tells
    # the reviewer a deliberate silence from a failure. Outlook only (see `status`).
    status_labels: bool = False
    # One-click unsubscribe from bulk senders the owner never reads (STU-2071). IMAP
    # only: it is the one writer that reads a sender's `\Seen` history.
    auto_unsubscribe: bool = False
    # Empty = anonymization off (Stage 2 sees cleartext). Stage 1 always runs on
    # cleartext, before any escalation, whatever the scope.
    anonymization_fields: list[str] = field(default_factory=list)
    # How far back this deployment is responsible for (STU-1123). None means today,
    # and it stays None here rather than being resolved at load: the webhook loads
    # the policy once and serves for days, so a date frozen at startup would keep a
    # run reaching back to whenever the service was last restarted.
    start_date: date | None = None
    # The mailbox's own clock (STU-1462): the boundary above is "today" in *this*
    # timezone, and a reply's salutation follows it too (`orchestrate.drafts`).
    # Unattended and pinned rather than read from the host, whose timezone is
    # nobody's decision. The first deployment's value is the default, kept for every caller that
    # does not yet thread a policy through.
    timezone: str = "America/Toronto"
    # Which `email_classifier/lang_packs/<language>.json` this deployment's person
    # detection and archive measurement run on (STU-1723). Read through
    # `lang.deployment_config`, the one place that resolves it.
    language: str = "fr_ca"
    # The skill whose capitalized words `anonymize.vouched()` refuses to redact: the
    # organization's own published names (STU-1995). None vouches nothing beyond the
    # lang pack, which over-redacts rather than letting a name through.
    vouched_skill: str | None = None
    # Modules that register an `envelope` rewriter on import (STU-1995): the kernel
    # imports what the deployment names and never names one itself.
    envelope_rewriters: list[str] = field(default_factory=list)
    # Other mailboxes whose Sent folder also answers a thread (STU-2082): a deployment
    # mid-move still replies from the old one, and its own Sent folder cannot see it.
    sent_sources: list[str] = field(default_factory=list)


def _start_date(value, path: Path) -> date | None:
    """The configured floor, or None for "today, whenever the run asks".

    Absent and the literal `today` are the same answer, spelled two ways: the key
    is what an operator widens, so the file may as well say out loud what it does
    when nobody has. A bare `2026-08-19` is already a `date` by the time YAML is
    done with it; a quoted one is not, and both are how someone writes a date.

    A time of day is kept as a `datetime` (STU-1897): `2026-09-29T17:40` floors a
    sweep at the moment an outage began, read in `timezone` unless it carries an
    offset. YAML parses one with seconds itself and leaves one without as a string.
    """
    if value is None or value == "today":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        raise ValueError(
            f"start_date must be an ISO date (2026-08-19), an ISO instant "
            f"(2026-09-29T17:40) or `today` in {path}, "
            f"got {value!r}: an unreadable floor would silently read as today and "
            "leave every message before it out of scope"
        ) from None


def _timezone(value, path: Path) -> str:
    """The configured IANA name, refused early rather than failing at `ZoneInfo(...)`
    the first time some run tries to resolve a floor with it."""
    name = value if value is not None else "America/Toronto"
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise ValueError(
            f"timezone must be an IANA name (e.g. America/Toronto) in {path}, "
            f"got {name!r}: an unresolvable zone would fail the first run that "
            "needs today's date instead of at load"
        ) from None
    return name


_LANGUAGE_CODE = re.compile(r"[A-Za-z0-9_-]+")


def _language(value, path: Path) -> str:
    """The configured pack name, refused early: it becomes a filename under
    `lang_packs/`, so anything that is not a plain code must not reach the loader."""
    name = value if value is not None else "fr_ca"
    if not isinstance(name, str) or not _LANGUAGE_CODE.fullmatch(name):
        raise ValueError(
            f"language must name a lang pack (e.g. fr_ca) in {path}, got {name!r}"
        )
    return name


def _vouched_skill(value, path: Path) -> str | None:
    """The skill name, refused like `language`: it becomes a filename under `skills/`."""
    if value is None:
        return None
    if not isinstance(value, str) or not _LANGUAGE_CODE.fullmatch(value):
        raise ValueError(
            f"vouched_skill must name a skill (e.g. org-facts) in {path}, got {value!r}"
        )
    return value


_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")


def _envelope_rewriters(value, path: Path) -> list[str]:
    """Dotted module names, refused early: each one is imported."""
    names = value if value is not None else []
    if not isinstance(names, list) or not all(
        isinstance(n, str) and _MODULE_NAME.fullmatch(n) for n in names
    ):
        raise ValueError(
            f"envelope_rewriters must list module names in {path}, got {value!r}"
        )
    return names


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    log_cfg = data.get("frontier_log", {})
    scope = data.get("anonymization", {}).get("scope")
    if scope is not None and scope not in ANONYMIZATION_SCOPES:
        raise ValueError(
            f"unknown anonymization.scope {scope!r} in {path}: "
            f"expected one of {sorted(ANONYMIZATION_SCOPES)}"
        )
    start_date = _start_date(data.get("start_date"), path)
    timezone = _timezone(data.get("timezone"), path)
    language = _language(data.get("language"), path)
    auto_apply = data.get("auto_apply", False)
    if not isinstance(auto_apply, bool):
        raise ValueError(
            f"auto_apply must be true or false in {path}, got {auto_apply!r}: "
            "a quoted 'false' would read as true and mutate the mailbox"
        )
    status_labels = data.get("status_labels", False)
    if not isinstance(status_labels, bool):
        raise ValueError(f"status_labels must be true or false in {path}, got {status_labels!r}")
    email_provider = data.get("email", {}).get("provider", "gmail")
    if status_labels and email_provider != "outlook":
        raise ValueError(
            f"status_labels needs email.provider outlook in {path}, got {email_provider!r}: "
            "the status names are accented, which an IMAP keyword cannot carry, and "
            "Gmail has no thread reader to tell a sent reply from a waiting one"
        )
    auto_unsubscribe = data.get("auto_unsubscribe", False)
    if not isinstance(auto_unsubscribe, bool):
        raise ValueError(
            f"auto_unsubscribe must be true or false in {path}, got {auto_unsubscribe!r}"
        )
    if auto_unsubscribe and email_provider != "imap":
        raise ValueError(
            f"auto_unsubscribe needs email.provider imap in {path}, got {email_provider!r}"
        )
    sent_sources = data.get("email", {}).get("sent_sources", [])
    if not isinstance(sent_sources, list) or set(sent_sources) - SENT_SOURCES:
        raise ValueError(
            f"email.sent_sources must be a list drawn from {sorted(SENT_SOURCES)} in "
            f"{path}, got {sent_sources!r}")
    return Policy(
        deterministic_can_finalize=data["deterministic_can_finalize"],
        destructive_actions_allowed=data.get("destructive_actions_allowed", []),
        frontier_log_file=log_cfg.get("file", True),
        frontier_log_stdout=log_cfg.get("stdout", False),
        email_provider=email_provider,
        auto_apply=auto_apply,
        status_labels=status_labels,
        auto_unsubscribe=auto_unsubscribe,
        anonymization_fields=ANONYMIZATION_SCOPES.get(scope, []),
        start_date=start_date,
        timezone=timezone,
        language=language,
        vouched_skill=_vouched_skill(data.get("vouched_skill"), path),
        envelope_rewriters=_envelope_rewriters(data.get("envelope_rewriters"), path),
        sent_sources=sent_sources,
    )


def filter_action(match: RuleMatch, policy: Policy) -> RuleMatch:
    if match.action in _DESTRUCTIVE_ACTIONS and match.action not in policy.destructive_actions_allowed:
        return RuleMatch(
            rule_id=match.rule_id,
            action="label_archive",
            action_original=match.action,
            confidence=match.confidence,
            bucket=match.bucket,
            expires_at=match.expires_at,
        )
    return match
