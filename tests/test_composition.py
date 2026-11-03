from email_classifier.core.types import EmailInput
from email_classifier.orchestrate import composition
from email_classifier.orchestrate.report import EmailVerdict


def _email(id_, sender, headers=None):
    return EmailInput(
        message_id=id_, thread_id=id_, from_address=sender, subject="s",
        headers=headers or {}, body_snippet="b", date="2026-06-15T12:00:00+00:00",
    )


def _verdict(id_, path):
    return EmailVerdict(
        message_id=id_, thread_id=id_, path=path, status="ok", bucket="A",
        suggested_action="keep_visible", confidence="high", rule_id="r",
        subtype=None, reasoning=None, error=None,
    )


def test_one_off_sender_without_unsubscribe_is_out_of_reach():
    emails = [_email("a", "someone@example.com")]
    assert composition.measure(emails, [_verdict("a", "llm")]) == {
        "unsubscribable": 0, "recurring": 0, "addressable": 0, "ratio_addressable": 0.0,
    }


def test_unsubscribable_and_recurring_are_addressable():
    emails = [
        _email("a", "promo@example.com", {"List-Unsubscribe": "<https://x>"}),
        _email("b", "bank@example.com"),
        _email("c", "bank@example.com"),
        _email("d", "stranger@example.com"),
    ]
    verdicts = [_verdict(id_, "llm") for id_ in ("a", "b", "c", "d")]
    result = composition.measure(emails, verdicts)
    assert result["unsubscribable"] == 1
    assert result["recurring"] == 2
    assert result["addressable"] == 3
    assert result["ratio_addressable"] == 0.0


def test_coverage_counts_deterministic_over_addressable():
    emails = [_email("a", "shop@example.com"), _email("b", "shop@example.com")]
    result = composition.measure(emails, [_verdict("a", "deterministic"), _verdict("b", "llm")])
    assert result["addressable"] == 2
    assert result["ratio_addressable"] == 0.5


def test_a_decided_one_off_sender_is_addressable():
    """A hand-written rule already reaches it, so it must not deflate the coverage."""
    emails = [_email("a", "alerts@bank.example")]
    result = composition.measure(emails, [_verdict("a", "deterministic")])
    assert result["addressable"] == 1
    assert result["ratio_addressable"] == 1.0


def test_unsubscribe_header_name_is_case_insensitive():
    emails = [_email("a", "promo@example.com", {"list-unsubscribe": "<https://x>"})]
    assert composition.measure(emails, [_verdict("a", "llm")])["unsubscribable"] == 1
