from pathlib import Path

import pytest

from email_classifier.core.types import EmailInput
from email_classifier.eval import parity
from email_classifier.eval.harness import load_corpus
from email_classifier.fetch.outlook import projection as outlook
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import load_rules
from tests.conftest import FIXTURES_DIR, REPO_ROOT, RULES_DIR


def _check(corpus: Path):
    return parity.check(
        load_corpus(corpus),
        load_rules(RULES_DIR / "rules.yaml"),
        load_policy(RULES_DIR / "policy.yaml"),
    )


@pytest.mark.parametrize("corpus", [FIXTURES_DIR])
def test_committed_corpora_project_identically_through_both_providers(corpus):
    """The provider-neutral contract's gate: a projection edit that changes what one
    provider produces, and not the other, fails here."""
    result = _check(corpus)
    assert result.divergences == []
    assert result.total > 0


def test_a_broken_projection_field_diverges(monkeypatch):
    """Graph stops reporting headers -> the bulk rules fire on the Gmail arm only, so
    parity reports both the lost field and the verdict it decided."""
    real = outlook.message_to_input
    monkeypatch.setattr(
        outlook, "message_to_input", lambda raw: {**real(raw), "headers": {}}
    )
    result = _check(FIXTURES_DIR)

    fields = {d.field for d in result.divergences}
    assert "headers" in fields
    assert "verdict" in fields


def test_parity_holds_on_normalization_the_corpus_never_exercises():
    """Every committed fixture already has a lowercase sender and canonically-cased
    headers, so the corpora alone cannot tell whether both projections still normalize.
    This one does not."""
    email = EmailInput(
        message_id="m1", thread_id="t1", from_address="News@Shop.Kobo.COM",
        subject="Soldes", headers={"LIST-UNSUBSCRIBE": "<mailto:u@shop.kobo.com>"},
        body_snippet="Rabais & nouveautés", date="2026-06-09T13:05:00+00:00",
    )
    via_gmail, via_outlook = parity.project_both(email)

    assert via_gmail.from_address == via_outlook.from_address == "news@shop.kobo.com"
    assert via_gmail.headers == via_outlook.headers
    assert list(via_gmail.headers) == ["List-Unsubscribe"]
    assert via_gmail.body_snippet == via_outlook.body_snippet == "Rabais & nouveautés"


def test_provider_category_is_not_a_parity_field():
    """Graph's Focused/Other split cannot spell `updates`, so the round trip drops it on
    the Outlook arm by design — a divergence the check must not report."""
    email = EmailInput(
        message_id="m1", thread_id="t1", from_address="a@example.com", subject="s",
        headers={}, body_snippet="b", date="2026-06-12T08:15:00+00:00",
        provider_category="updates",
    )
    via_gmail, via_outlook = parity.project_both(email)

    assert via_gmail.provider_category == "updates"
    assert via_outlook.provider_category is None
    assert "provider_category" not in parity.PARITY_FIELDS

    result = parity.check(
        [email], load_rules(RULES_DIR / "rules.yaml"), load_policy(RULES_DIR / "policy.yaml")
    )
    assert result.divergences == []
