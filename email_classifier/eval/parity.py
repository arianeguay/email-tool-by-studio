"""Provider parity (STU-848): one email must classify identically whichever
projection it arrives through.

Each corpus email is encoded into a native Gmail resource and a native Graph one
by the provider modules that own those vocabularies, then read back through the
real `message_to_input` projections. Two neutral inputs that disagree mean one
projection is wrong — never a classification judgement call, because stage 1 is
deterministic and both arms started from the same fixture.

Pure module: no mailbox, no network, no LLM. It measures the projections, not the
transport — a seeded Graph mailbox loses headers on the store-write (STU-858),
which is a fidelity question `seed/outlook.py:divergence` already answers.

`provider_category` is excluded, and is the only field that is. The neutral field
carries Gmail's tab vocabulary (`primary`, `updates`, `promotions`, ...) while
Graph offers only the Focused/Other split, so `updates` has no Graph spelling to
survive the round trip. No stage-1 rule reads the field.
"""

from dataclasses import dataclass

from email_classifier.core import schema
from email_classifier.core.types import EmailInput
from email_classifier.eval.harness import Label, format_label, replay
from email_classifier.fetch.gmail import projection as gmail
from email_classifier.fetch.outlook import projection as outlook
from email_classifier.stage1.policy import Policy
from email_classifier.stage1.rule_engine import Rule

PARITY_FIELDS = (
    "message_id",
    "thread_id",
    "from_address",
    "subject",
    "headers",
    "body_snippet",
    "date",
)


@dataclass
class Divergence:
    message_id: str
    field: str      # a neutral field name, or "verdict"
    gmail: str
    outlook: str


@dataclass
class ParityResult:
    total: int
    divergences: list[Divergence]


def project_both(email: EmailInput) -> tuple[EmailInput, EmailInput]:
    """The fixture as it comes back from a Gmail round trip, and from a Graph one."""
    return (
        schema.validate(gmail.message_to_input(gmail.input_to_message(email))),
        schema.validate(outlook.message_to_input(outlook.input_to_message(email))),
    )


def _verdict(email: EmailInput, rules: list[Rule], policy: Policy) -> Label:
    return replay([email], rules, policy)[email.message_id]


def check(emails: list[EmailInput], rules: list[Rule], policy: Policy) -> ParityResult:
    divergences: list[Divergence] = []
    for email in emails:
        via_gmail, via_outlook = project_both(email)
        divergences += [
            Divergence(email.message_id, field, repr(left), repr(right))
            for field in PARITY_FIELDS
            if (left := getattr(via_gmail, field)) != (right := getattr(via_outlook, field))
        ]
        left_verdict = _verdict(via_gmail, rules, policy)
        right_verdict = _verdict(via_outlook, rules, policy)
        if left_verdict != right_verdict:
            divergences.append(
                Divergence(
                    email.message_id,
                    "verdict",
                    format_label(left_verdict),
                    format_label(right_verdict),
                )
            )
    return ParityResult(total=len(emails), divergences=divergences)


def render(result: ParityResult) -> str:
    lines = [
        "=== Provider parity: gmail vs outlook projection ===",
        f"{result.total} message(s) | {len(result.divergences)} divergence(s)",
    ]
    lines += [
        f"  DIVERGE[{d.field}] {d.message_id}: gmail={d.gmail} outlook={d.outlook}"
        for d in result.divergences
    ]
    return "\n".join(lines)
