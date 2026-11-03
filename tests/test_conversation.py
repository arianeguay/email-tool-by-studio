"""The verdict unit for a conversation (STU-940), and the collapse that feeds it (STU-944).

Each of the three parts of the reconciliation rule — bucket by taxonomy precedence,
action by recency, action clamped to the bucket — is pinned by a case the other two get
wrong, so dropping one fails here rather than in a mailbox. `collapse` is pinned the same
way: the head is the latest *escalated* message, and a decided sibling is context.
"""

from pathlib import Path

import pytest

from email_classifier.core.contract import load_taxonomy
from email_classifier.core.types import EmailInput
from email_classifier.orchestrate import conversation
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")


def verdict(message_id, date, bucket, action, thread_id="t1", status="ok", rule_id="r1"):
    return EmailVerdict(
        message_id=message_id, thread_id=thread_id, path="deterministic", status=status,
        bucket=bucket, suggested_action=action, confidence="high", rule_id=rule_id,
        subtype=None, reasoning=None, error=None, date=date,
    )


def reconcile(*verdicts):
    return conversation.reconcile(list(verdicts), TAXONOMY)


def test_group_keeps_report_order_within_and_across_conversations():
    verdicts = [
        verdict("m1", "2026-08-01", "A", "keep_visible", thread_id="t1"),
        verdict("m2", "2026-08-02", "B", "label_archive", thread_id="t2"),
        verdict("m3", "2026-08-03", "A", "keep_visible", thread_id="t1"),
    ]

    threads = conversation.group(verdicts)

    assert list(threads) == ["t1", "t2"]
    assert [v.message_id for v in threads["t1"]] == ["m1", "m3"]


def test_a_lone_message_reconciles_to_itself():
    decision = reconcile(verdict("m1", "2026-08-01", "C", "label_archive"))

    assert (decision.bucket, decision.suggested_action) == ("C", "label_archive")
    assert decision.latest_message_id == "m1"


def test_the_action_comes_from_the_most_recent_message():
    """`remerciement_selections`: a question, then a "merci". The thread is closed —
    reconciling the action by precedence would leave it owing a reply forever."""
    decision = reconcile(
        verdict("m1", "2026-08-18", "A", "needs_reply"),
        verdict("m2", "2026-08-19", "A", "keep_visible"),
    )

    assert decision.suggested_action == "keep_visible"
    assert decision.latest_message_id == "m2"


def test_recency_is_read_from_the_dates_and_not_the_report_order():
    decision = reconcile(
        verdict("m2", "2026-08-19", "A", "keep_visible"),
        verdict("m1", "2026-08-18", "A", "needs_reply"),
    )

    assert decision.suggested_action == "keep_visible"


def test_the_bucket_is_the_most_important_one_any_message_carries():
    """`infolettre_reponse_parent`: a newsletter opens the thread, a parent asks a real
    question in it. Reconciling the bucket by recency would file the question under C."""
    decision = reconcile(
        verdict("m1", "2026-08-18", "C", "label_archive"),
        verdict("m2", "2026-08-19", "A", "needs_reply"),
    )

    assert (decision.bucket, decision.suggested_action) == ("A", "needs_reply")


def test_a_bucket_a_conversation_is_never_archived_by_a_late_notification():
    """The clamp. Without it the latest message's `label_archive` buries the unanswered
    question above it."""
    decision = reconcile(
        verdict("m1", "2026-08-18", "A", "needs_reply"),
        verdict("m2", "2026-08-19", "C", "label_archive"),
    )

    assert (decision.bucket, decision.suggested_action) == ("A", "keep_visible")


def test_a_bucket_b_conversation_is_archived_rather_than_trashed():
    decision = reconcile(
        verdict("m1", "2026-08-18", "B", "label_archive"),
        verdict("m2", "2026-08-19", "D", "mark_for_delete"),
    )

    assert (decision.bucket, decision.suggested_action) == ("B", "label_archive")


def test_a_disposable_conversation_is_not_clamped():
    decision = reconcile(
        verdict("m1", "2026-08-18", "C", "label_archive"),
        verdict("m2", "2026-08-19", "D", "mark_for_delete"),
    )

    assert (decision.bucket, decision.suggested_action) == ("C", "mark_for_delete")


def test_the_rule_id_cited_is_the_one_whose_action_the_conversation_took():
    decision = reconcile(
        verdict("m1", "2026-08-18", "A", "needs_reply", rule_id="opened_it"),
        verdict("m2", "2026-08-19", "A", "keep_visible", rule_id="closed_it"),
    )

    assert decision.rule_id == "closed_it"


@pytest.mark.parametrize(
    "undecided",
    [
        verdict("m2", "2026-08-19", None, None, status="error"),
        verdict("m2", "2026-08-19", "A", "escalate_to_llm"),
    ],
    ids=["error", "unapplicable action"],
)
def test_one_undecided_message_leaves_the_whole_conversation_undecided(undecided):
    assert reconcile(verdict("m1", "2026-08-18", "A", "needs_reply"), undecided) is None


@pytest.mark.parametrize("bucket", [None, "newsletter"], ids=["none", "off-taxonomy"])
def test_an_action_with_no_taxonomy_bucket_raises_rather_than_holding_forever(bucket):
    """`ec/<bucket>` and the taxonomy are one vocabulary. Held for review instead, this
    would be held every run, and the run before it wrote no junk label to say why."""
    with pytest.raises(ValueError, match="is not one of A, B, C, D"):
        reconcile(verdict("m1", "2026-08-18", bucket, "label_archive"))


def test_the_clamp_only_names_actions_the_contract_allows():
    """The per-bucket action lists live in the deployment's own taxonomy (STU-1461),
    loaded from `rules/taxonomy.yaml`. The clamp is a fallback picked out of them, not
    a second copy of them — `load_taxonomy` itself refuses a config where it is not."""
    assert all(
        action in TAXONOMY.allowed_actions[bucket]
        for bucket, action in TAXONOMY.clamp.items()
    )
    # A bucket outside the table is one nothing can ask more of than it already allows.
    assert all(
        "mark_for_delete" in TAXONOMY.allowed_actions[bucket]
        for bucket in TAXONOMY.allowed_actions
        if bucket not in TAXONOMY.clamp
    )


def test_a_dateless_conversation_of_several_messages_is_left_alone():
    """A report predating the `date` field cannot say which message is the latest, and
    the list order is the fetch's. Declining beats guessing."""
    assert reconcile(
        verdict("m1", "", "A", "needs_reply"),
        verdict("m2", "", "A", "keep_visible"),
    ) is None


def test_a_dateless_lone_message_still_reconciles():
    assert reconcile(verdict("m1", "", "B", "label_archive")).suggested_action == (
        "label_archive"
    )


def mail(message_id, date, thread_id="t1", body="b"):
    return EmailInput(
        message_id=message_id, thread_id=thread_id, from_address="a@b.test",
        subject="s", headers={}, body_snippet=body, date=date,
    )


def test_each_conversation_escalates_once():
    emails = [mail("m1", "2026-08-01"), mail("m2", "2026-08-02"),
              mail("m3", "2026-08-03", thread_id="t2")]

    out = conversation.collapse(emails, {"m1", "m2", "m3"})

    assert [e.head.message_id for e in out] == ["m2", "m3"]
    assert out[0].message_ids == ("m1", "m2")


def test_the_head_is_the_latest_escalated_message_not_the_latest_message():
    emails = [mail("m1", "2026-08-01"), mail("m2", "2026-08-02")]

    out = conversation.collapse(emails, {"m1"})

    assert out[0].head.message_id == "m1"
    assert [m.message_id for m in out[0].history] == ["m2"]


def test_a_decided_sibling_is_history_not_an_escalation():
    emails = [mail("m1", "2026-08-01"), mail("m2", "2026-08-02")]

    out = conversation.collapse(emails, {"m2"})

    assert out[0].message_ids == ("m2",)
    assert [m.message_id for m in out[0].history] == ["m1"]


def test_a_conversation_nothing_escalated_never_reaches_stage_two():
    assert conversation.collapse([mail("m1", "2026-08-01")], set()) == []


def test_the_history_is_oldest_first_whatever_the_fetch_order():
    """Graph pages newest-first; a transcript read backwards is a different question."""
    emails = [mail("m3", "2026-08-03"), mail("m1", "2026-08-01"), mail("m2", "2026-08-02")]

    out = conversation.collapse(emails, {"m3"})

    assert [m.message_id for m in out[0].history] == ["m1", "m2"]
    text = conversation.transcript(out[0].history)
    assert text.index("2026-08-01") < text.index("2026-08-02")
