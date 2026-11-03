"""Classification contract — single source of truth (STU-390).

The action↔bucket table (INV-08) lives here once. Two callers use it:
pytest (via validate_classification) and the Studio engine (via stage2.validate_cli,
wired to the contract's `validators` hook).

Three layers used to live here as literals, and only one of them is the kernel's to
own (STU-1461). `ACTIONS` is the four things you can do to a mail plus the universal
escalation hatch — wired through `apply/executor.py`, `stage1/policy.py`,
`orchestrate/drafts.py` and `learn/promote.py`, not a domain opinion. The bucket
shape — how many buckets, their precedence, which actions and subtypes each allows —
is a deployment's own taxonomy, loaded from a file via `load_taxonomy` rather than
baked in. The kernel enforces the *shape* a taxonomy must have and never reasons
about a specific bucket's name.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

CONFIDENCES = ("high", "medium", "low")
ACTIONS = (
    "keep_visible",
    "label_archive",
    "mark_for_delete",
    "needs_reply",
    "escalate_to_llm",
)


@dataclass(frozen=True)
class Taxonomy:
    """One deployment's bucket shape (STU-1461): N totally-ordered buckets, each with
    its own allowed actions, subtype vocabulary and clamp fallback.

    `buckets` is ordered highest precedence first — `orchestrate.conversation`
    reconciles a multi-message thread onto the earliest-listed bucket any of its
    messages carries, so a deployment's tie-break rule is this list's own order,
    never a literal spelled a second time.
    """

    buckets: tuple[str, ...]
    allowed_actions: dict[str, frozenset[str]]
    allowed_subtypes: dict[str, frozenset[str]]
    clamp: dict[str, str]

    @property
    def subtypes(self) -> tuple[str, ...]:
        return tuple(sorted(frozenset().union(*self.allowed_subtypes.values())))


def load_taxonomy(path: Path) -> Taxonomy:
    """A deployment's own bucket shape, refused rather than guessed at when it is
    missing or malformed (STU-1461).

    No fallback taxonomy exists: a deployment that has not written this file is not
    accidentally classified under somebody else's buckets. `core/taxonomy.reference.yaml`
    is a template to copy, never read by this function.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"no taxonomy at {path}: this deployment has not defined its own bucket "
            "shape. Copy email_classifier/core/taxonomy.reference.yaml there and edit "
            "it — there is no default taxonomy to fall back to."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("buckets") or []
    if not entries:
        raise ValueError(f"{path}: `buckets` must be a non-empty ordered list")

    buckets: list[str] = []
    allowed_actions: dict[str, frozenset[str]] = {}
    allowed_subtypes: dict[str, frozenset[str]] = {}
    clamp: dict[str, str] = {}
    for entry in entries:
        name = entry["name"]
        if name in buckets:
            raise ValueError(f"{path}: bucket '{name}' listed more than once")
        buckets.append(name)

        actions = frozenset(entry.get("allowed_actions") or ())
        if not actions:
            raise ValueError(f"{path}: bucket '{name}' names no allowed_actions")
        unknown = actions - set(ACTIONS)
        if unknown:
            raise ValueError(
                f"{path}: bucket '{name}' allows {sorted(unknown)}, outside the "
                f"kernel action vocabulary {list(ACTIONS)}"
            )
        allowed_actions[name] = actions
        allowed_subtypes[name] = frozenset(entry.get("subtypes") or ())

        fallback = entry.get("clamp")
        if fallback is not None:
            if fallback not in actions:
                raise ValueError(
                    f"{path}: bucket '{name}' clamps to {fallback!r}, which is not in "
                    f"its own allowed_actions {sorted(actions)}"
                )
            clamp[name] = fallback

    return Taxonomy(
        buckets=tuple(buckets),
        allowed_actions=allowed_actions,
        allowed_subtypes=allowed_subtypes,
        clamp=clamp,
    )


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


_REQUIRED_STR_FIELDS = (
    "thread_id",
    "bucket",
    "subtype",
    "confidence",
    "suggested_action",
    "reasoning",
)
_ALL_FIELDS = _REQUIRED_STR_FIELDS + ("suggested_label",)


def enum_and_consistency_errors(
    bucket: str,
    confidence: str,
    action: str,
    taxonomy: Taxonomy,
    subtype: str | None = None,
) -> list[str]:
    """Enum membership + the action↔bucket and subtype↔bucket tables (INV-08). Factored
    out so the tables have exactly one definition shared by validate_classification (and
    exercised directly by the CLI through the full validator).

    `subtype` is None for a deterministic verdict, which carries none; the subtype
    checks are then skipped. The tables themselves come from `taxonomy`, not from a
    module literal (STU-1461): what counts as a valid bucket, action-per-bucket and
    subtype-per-bucket is this deployment's own data, never the kernel's.
    """
    errors: list[str] = []
    if bucket not in taxonomy.buckets:
        errors.append(f"bucket '{bucket}' not in {list(taxonomy.buckets)}")
    if confidence not in CONFIDENCES:
        errors.append(f"confidence '{confidence}' not in {list(CONFIDENCES)}")
    if action not in ACTIONS:
        errors.append(f"suggested_action '{action}' not in {list(ACTIONS)}")
    if subtype is not None and subtype not in taxonomy.subtypes:
        errors.append(f"subtype '{subtype}' not in {list(taxonomy.subtypes)}")
    if bucket in taxonomy.buckets and action in ACTIONS:
        allowed = taxonomy.allowed_actions[bucket] | {"escalate_to_llm"}
        if action not in allowed:
            errors.append(
                f"action '{action}' not allowed for bucket '{bucket}' "
                f"(allowed: {sorted(allowed)})"
            )
    if (
        bucket in taxonomy.buckets
        and subtype in taxonomy.subtypes
        and subtype not in taxonomy.allowed_subtypes[bucket]
    ):
        errors.append(
            f"subtype '{subtype}' not allowed for bucket '{bucket}' "
            f"(allowed: {sorted(taxonomy.allowed_subtypes[bucket])})"
        )
    return errors


def validate_classification(data: dict, taxonomy: Taxonomy) -> ValidationResult:
    """Full binary validator (INV-08). Collects ALL errors (never fail-fast) so the
    error list can serve as enriched RALPH retry feedback. Pure: no LLM, no I/O.

    `taxonomy` is required and never defaulted (STU-1461): a caller with no taxonomy
    to validate against has no business calling this at all, and a hidden default
    would be exactly the silent fallback to another deployment this module exists to refuse.
    """
    if not isinstance(data, dict):
        return ValidationResult(False, [f"expected object, got {type(data).__name__}"])

    errors: list[str] = []

    for key in data:
        if key not in _ALL_FIELDS:
            errors.append(f"unexpected field: {key}")

    for field_name in _REQUIRED_STR_FIELDS:
        if field_name not in data:
            errors.append(f"missing required field: {field_name}")
        elif not isinstance(data[field_name], str) or data[field_name] == "":
            errors.append(f"field '{field_name}' must be a non-empty string")

    if "suggested_label" not in data:
        errors.append("missing required field: suggested_label")
    elif data["suggested_label"] is not None and not isinstance(data["suggested_label"], str):
        errors.append("field 'suggested_label' must be a string or null")

    bucket, confidence, action, subtype = (
        data.get("bucket"),
        data.get("confidence"),
        data.get("suggested_action"),
        data.get("subtype"),
    )
    # Enum + table checks only when the four are present as strings (type/presence
    # errors above already cover the rest — avoids noisy duplicate messages).
    if all(isinstance(v, str) for v in (bucket, confidence, action, subtype)):
        errors.extend(enum_and_consistency_errors(bucket, confidence, action, taxonomy, subtype))

    return ValidationResult(valid=not errors, errors=errors)
