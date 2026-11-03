from dataclasses import dataclass
from pathlib import Path

import yaml

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.stage1.guards import GUARDS, is_satisfied

_KNOWN_CONDITION_KEYS: frozenset[str] = frozenset(
    {"from_address", "from_domain", "subject_contains", "header_present",
     "header_value", "from_domain_suffix", "guard"}
)


@dataclass
class Rule:
    id: str
    conditions: dict[str, str]
    bucket: str | None
    confidence: str
    action: str


def _parse_rules(path: Path) -> list[Rule]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = [
        Rule(
            id=r["id"],
            conditions=r.get("conditions") or {},
            bucket=r.get("bucket"),
            confidence=r["confidence"],
            action=r["action"],
        )
        for r in data["rules"]
    ]
    for rule in rules:
        unknown = set(rule.conditions) - _KNOWN_CONDITION_KEYS
        if unknown:
            raise ValueError(
                f"Rule '{rule.id}' has unknown condition key(s): {sorted(unknown)}. "
                f"Known keys: {sorted(_KNOWN_CONDITION_KEYS)}"
            )
    for rule in rules:
        guard_name = rule.conditions.get("guard")
        if guard_name is not None and guard_name not in GUARDS:
            raise ValueError(
                f"Rule '{rule.id}' references unknown guard '{guard_name}'. "
                f"Known guards: {sorted(GUARDS)}"
            )
    for rule in rules:
        if "header_value" in rule.conditions:
            value = rule.conditions["header_value"]
            if not (
                isinstance(value, dict)
                and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
            ):
                raise ValueError(
                    f"Rule '{rule.id}': 'header_value' must be a mapping of header name to substring, "
                    f"got {type(value).__name__}"
                )
        for key in ("from_domain_suffix", "subject_contains"):
            if key not in rule.conditions:
                continue
            value = rule.conditions[key]
            if not (
                isinstance(value, str)
                or (isinstance(value, list) and all(isinstance(s, str) for s in value))
            ):
                raise ValueError(
                    f"Rule '{rule.id}': '{key}' must be a string or list of strings, "
                    f"got {type(value).__name__}"
                )
    return rules


def load_rules(path: Path, learned_path: Path | None = None) -> list[Rule]:
    """Hand-written rules, then the learned layer, then the catch-all (STU-822).

    First-match-wins, so a hand-written rule always beats a promoted one and the
    catch-all stays last — the learned file is machine-authored and never trusted with
    either position.
    """
    rules = _parse_rules(path)
    if not rules or rules[-1].conditions != {}:
        raise ValueError("rules.yaml must end with a catch-all rule (conditions: {})")
    if learned_path is None or not learned_path.exists():
        return rules

    learned = _parse_rules(learned_path)
    for rule in learned:
        if not rule.conditions:
            raise ValueError(
                f"Learned rule '{rule.id}' has no conditions — it would shadow the catch-all "
                f"and swallow every email. Delete {learned_path} and re-run `make promote`."
            )
    return rules[:-1] + learned + [rules[-1]]


def _extract_domain(address: str) -> str:
    return address.split("@")[-1].lower()


def _domain_matches_suffix(domain: str, suffix: str) -> bool:
    domain, suffix = domain.lower(), suffix.lower()
    return domain == suffix or domain.endswith("." + suffix)


def matches(email: EmailInput, conditions: dict[str, str]) -> bool:
    for key, value in conditions.items():
        if key == "from_address":
            if email.from_address.lower() != value.lower():
                return False
        elif key == "from_domain":
            if _extract_domain(email.from_address) != value.lower():
                return False
        elif key == "subject_contains":
            needles = [value] if isinstance(value, str) else value
            subject = email.subject.lower()
            if not any(needle.lower() in subject for needle in needles):
                return False
        elif key == "header_present":
            if value not in email.headers:
                return False
        elif key == "header_value":
            for header_name, needle in value.items():
                if needle.lower() not in email.headers.get(header_name, "").lower():
                    return False
        elif key == "from_domain_suffix":
            suffixes = [value] if isinstance(value, str) else value
            domain = _extract_domain(email.from_address)
            if not any(_domain_matches_suffix(domain, s) for s in suffixes):
                return False
        elif key == "guard":
            if not is_satisfied(value, email):
                return False
    return True


def apply(email: EmailInput, rules: list[Rule]) -> RuleMatch:
    for rule in rules:
        if matches(email, rule.conditions):
            return RuleMatch(
                rule_id=rule.id,
                action=rule.action,
                action_original=None,
                confidence=rule.confidence,
                bucket=rule.bucket,
            )
    raise RuntimeError("No catch-all rule — this should not happen after load_rules() validation")
