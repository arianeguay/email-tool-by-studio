import json
from dataclasses import replace

from email_classifier.anonymize import Tokenizer
from email_classifier.core.types import Classification, EmailInput
from email_classifier.orchestrate.pipeline import run
from email_classifier.orchestrate.stage2_client import Stage2Result
from email_classifier.stage1.frontier_log import FrontierLogger
from email_classifier.stage1.policy import ANONYMIZATION_SCOPES, load_policy
from email_classifier.stage1.rule_engine import load_rules
from tests.conftest import RULES_DIR


class FakeClassifier:
    """Records what it is asked to classify and returns canned results.

    `inputs` is the whole EmailInput and not just its id, because what stage 2 is
    handed is the assertion STU-1009 needs: the report and the verdict store are
    restored to cleartext on purpose, so neither can show whether the prompt was.
    """

    def __init__(self, results=None):
        self.results = results or {}
        self.seen = []
        self.inputs = []
        self.histories = []

    def classify(self, email, thread_history=""):
        self.seen.append(email.message_id)
        self.inputs.append(email)
        self.histories.append(thread_history)
        return self.results.get(
            email.message_id,
            Stage2Result(classification=Classification(
                thread_id=email.thread_id, bucket="C", subtype="product_newsletter",
                confidence="medium", suggested_action="label_archive",
                suggested_label=None, reasoning="default fake",
            )),
        )


def _email(thread_id, from_address, message_id=None):
    return EmailInput(
        message_id=message_id or thread_id, thread_id=thread_id,
        from_address=from_address, subject="S", headers={}, body_snippet="b",
        date="2026-06-10T00:00:00+00:00",
    )


# The deployment's own rules decide nothing these tests can name in advance, so the
# rules are stated here: one sender each path, and the catch-all every rules file ends on.
RULES = """
rules:
  - id: interac_payment
    conditions:
      from_domain: payments.interac.ca
    bucket: A
    confidence: high
    action: keep_visible
  - id: shop_newsletter
    conditions:
      from_address: news@shop.test
    bucket: C
    confidence: high
    action: label_archive
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""


def _load(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(RULES, encoding="utf-8")
    return load_rules(path), load_policy(RULES_DIR / "policy.yaml")


def _run(emails, rules, policy, stage2, logger, provenance=None, store=None,
          tokenizer=None):
    """`pipeline.run` with a tokenizer, which the shipped policy now requires.

    `rules/policy.yaml` sets `scope: full`, so a run through it tokenizes before stage 2
    (STU-1009) and refuses to start without something to tokenize with. `persons=False`
    is the format half alone, which keeps these tests off the `ner` extra (STU-857).
    """
    return run(emails, rules, policy, stage2, logger, provenance, store,
               tokenizer or Tokenizer(persons=False))


def test_only_escalated_emails_reach_the_llm(tmp_path):
    rules, policy = _load(tmp_path)
    emails = [
        _email("det1", "noreply@payments.interac.ca"),   # interac_payment -> keep_visible
        _email("grey1", "stranger@unknown-domain.test"),  # catch_all -> escalate_to_llm
    ]
    fake = FakeClassifier()
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert fake.seen == ["grey1"]  # Stage-1-decided email NEVER classified by LLM
    det = next(v for v in report.verdicts if v.thread_id == "det1")
    assert det.path == "deterministic"
    assert det.suggested_action == "keep_visible"
    grey = next(v for v in report.verdicts if v.thread_id == "grey1")
    assert grey.path == "llm"


def test_frontier_log_records_escalation_reason(tmp_path):
    rules, policy = _load(tmp_path)
    log_path = tmp_path / "f.jsonl"
    _run([_email("grey1", "stranger@unknown-domain.test")], rules, policy,
         FakeClassifier(), FrontierLogger(log_path=log_path))
    entry = json.loads(log_path.read_text().strip().splitlines()[0])
    assert entry["action"] == "escalate_to_llm"
    assert entry["rule_id"] == "catch_all"


def test_stage2_error_is_recorded_and_run_continues(tmp_path):
    rules, policy = _load(tmp_path)
    emails = [
        _email("grey1", "a@unknown1.test"),
        _email("grey2", "b@unknown2.test"),
    ]
    fake = FakeClassifier({"grey1": Stage2Result(
        error="studio_run_timeout", error_detail="after 180s | stderr=boom")})
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert report.totals["errors"] == 1
    g1 = next(v for v in report.verdicts if v.thread_id == "grey1")
    assert g1.status == "error" and g1.error == "studio_run_timeout"
    assert g1.error_detail == "after 180s | stderr=boom"
    g2 = next(v for v in report.verdicts if v.thread_id == "grey2")
    assert g2.status == "ok"


def test_report_run_id_matches_logger(tmp_path):
    rules, policy = _load(tmp_path)
    logger = FrontierLogger(log_path=tmp_path / "f.jsonl")
    report = _run([_email("det1", "noreply@payments.interac.ca")], rules, policy,
                  FakeClassifier(), logger)
    assert report.run_id == logger.run_id
    assert report.totals == {
        "emails": 1, "deterministic": 1, "llm": 0, "llm_calls": 0, "errors": 0,
    }


def test_dedupe_collapses_only_true_message_id_duplicates():
    from email_classifier.orchestrate.pipeline import dedupe_by_message

    # Same message_id fetched twice -> collapse, keep latest date.
    old = EmailInput("m1", "t", "a@b.com", "old", {}, "b", "2026-06-10T00:00:00+00:00")
    new = EmailInput("m1", "t", "a@b.com", "new", {}, "b", "2026-06-12T00:00:00+00:00")
    out = dedupe_by_message([old, new])
    assert len(out) == 1 and out[0].subject == "new"

    # Two distinct messages sharing a thread -> BOTH survive.
    a = EmailInput("m1", "shared", "a@b.com", "first", {}, "b", "2026-06-10T00:00:00+00:00")
    b = EmailInput("m2", "shared", "a@b.com", "second", {}, "b", "2026-06-11T00:00:00+00:00")
    both = dedupe_by_message([a, b])
    assert [e.message_id for e in both] == ["m1", "m2"]


def test_run_keeps_distinct_messages_and_collapses_true_duplicates(tmp_path):
    rules, policy = _load(tmp_path)
    emails = [
        # two distinct messages, same thread -> two verdicts
        _email("grey_a", "a@unknown.test", message_id="m_a"),
        EmailInput(message_id="m_b", thread_id="grey_a", from_address="a@unknown.test",
                   subject="second", headers={}, body_snippet="b2",
                   date="2026-06-12T00:00:00+00:00"),
        # true duplicate of m_a -> collapses
        EmailInput(message_id="m_a", thread_id="grey_a", from_address="a@unknown.test",
                   subject="dup", headers={}, body_snippet="b3",
                   date="2026-06-13T00:00:00+00:00"),
    ]
    fake = FakeClassifier()
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert report.totals["emails"] == 2            # m_a (deduped) + m_b
    assert fake.seen == ["m_a"]                    # one thread, one call (STU-944)
    assert len(report.verdicts) == 2               # both messages still get a verdict
    # frontier log has one line per unique message
    assert len((tmp_path / "f.jsonl").read_text().strip().splitlines()) == 2


def test_three_messages_of_one_thread_are_one_escalation(tmp_path):
    """The issue's own criterion (STU-944): a fetch carrying three messages of one
    conversation costs one LLM call, and the verdict it returns is every message's."""
    rules, policy = _load(tmp_path)
    emails = [
        EmailInput(message_id=f"m_{n}", thread_id="grey_a", from_address="a@unknown.test",
                   subject=f"tour {n}", headers={}, body_snippet=f"b{n}",
                   date=f"2026-06-1{n}T00:00:00+00:00")
        for n in (1, 2, 3)
    ]
    fake = FakeClassifier()
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert fake.seen == ["m_3"]                    # the most recent escalated message
    assert "b1" in fake.histories[0] and "b2" in fake.histories[0]
    assert "b3" not in fake.histories[0]           # the head is not its own history
    assert [v.message_id for v in report.verdicts] == ["m_1", "m_2", "m_3"]
    assert {v.bucket for v in report.verdicts} == {"C"}  # one verdict, no disagreement
    # and the report says what it cost, not what it escalated (STU-989)
    assert report.totals["llm"] == 3
    assert report.totals["llm_calls"] == len(fake.seen) == 1


def _spend(out, attempts=1):
    """The child's per-stage record, as `stage2_client` derives it (STU-1244)."""
    return {"stages": [{
        "stage": "classify", "status": "success", "attempts": attempts,
        "token_usage": {"by_model": {"claude-opus-4-8": {"completion_tokens": out,
                                                         "total_tokens": out}}},
    }]}


def test_the_report_carries_what_the_run_s_stage_2_calls_spent(tmp_path):
    """STU-1012: the counts land in the report, which outlives the worktree the
    Studio log was written beside."""
    rules, policy = _load(tmp_path)
    emails = [_email("grey_a", "a@unknown.test"), _email("grey_b", "b@unknown.test")]
    fake = FakeClassifier({
        mid: Stage2Result(classification=Classification(
            thread_id=mid, bucket="C", subtype="product_newsletter", confidence="medium",
            suggested_action="label_archive", suggested_label=None, reasoning="r"),
            spend=_spend(out))
        for mid, out in (("grey_a", 100), ("grey_b", 200))
    })
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    spend = report.cost["classify"]
    assert spend["llm_calls"] == 2
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 300


def test_one_conversation_is_billed_once_however_many_messages_it_has(tmp_path):
    """Counting the shared verdict per message would multiply the bill by thread
    length — the run made one call (STU-944)."""
    rules, policy = _load(tmp_path)
    emails = [
        EmailInput(message_id=f"m_{n}", thread_id="grey_a", from_address="a@unknown.test",
                   subject=f"tour {n}", headers={}, body_snippet=f"b{n}",
                   date=f"2026-06-1{n}T00:00:00+00:00")
        for n in (1, 2, 3)
    ]
    fake = FakeClassifier({"m_3": Stage2Result(
        classification=Classification(
            thread_id="grey_a", bucket="C", subtype="product_newsletter",
            confidence="medium", suggested_action="label_archive",
            suggested_label=None, reasoning="r"),
        spend=_spend(100))})
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert report.totals["llm"] == 3
    assert report.cost["classify"]["llm_calls"] == 1
    assert report.cost["classify"]["by_model"]["claude-opus-4-8"]["completion_tokens"] == 100


def test_a_retried_escalation_reports_its_attempts_like_the_batch_path(tmp_path):
    """STU-1244: a call is a RALPH attempt on both orchestrations, so one corpus
    reports the same `llm_calls` whichever ran it. `unrecorded_items` is 0 because
    every child left a record — non-zero is what says part of the count is still in
    the old stage-execution unit."""
    rules, policy = _load(tmp_path)
    emails = [_email("grey_a", "a@unknown.test")]
    fake = FakeClassifier({"grey_a": Stage2Result(
        classification=Classification(
            thread_id="grey_a", bucket="C", subtype="product_newsletter",
            confidence="medium", suggested_action="label_archive",
            suggested_label=None, reasoning="r"),
        spend=_spend(100, attempts=3))})
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    spend = report.cost["classify"]
    assert (spend["llm_calls"], spend["unrecorded_items"]) == (3, 0)


def test_a_run_that_escalated_nothing_records_an_empty_leg(tmp_path):
    rules, policy = _load(tmp_path)
    report = _run([_email("det1", "noreply@payments.interac.ca")], rules, policy,
                  FakeClassifier(), FrontierLogger(log_path=tmp_path / "f.jsonl"))
    assert report.cost == {"classify": {"llm_calls": 0, "unpriced_calls": 0,
                                        "unrecorded_items": 0, "provider": None,
                                        "by_model": {}}}


def test_a_stage_2_that_died_before_reporting_is_still_a_call_the_run_made(tmp_path):
    """A provider limit answers every conversation identically (STU-1016), so a run
    priced on the calls that answered would read as the cheap one it is not."""
    rules, policy = _load(tmp_path)
    emails = [_email("grey_a", "a@unknown.test"), _email("grey_b", "b@unknown.test")]
    fake = FakeClassifier({"grey_b": Stage2Result(error="provider_unavailable")})
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    spend = report.cost["classify"]
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (2, 2)
    # Both fell to the floor: the fake carries no record, which is what a child that
    # died before printing one leaves. `unrecorded_items` is what says so.
    assert spend["unrecorded_items"] == 2


def test_a_decided_sibling_is_context_the_call_still_sees(tmp_path):
    """A reply is projected without its quote (STU-935), so the message that supplies
    its other half belongs in the call even when a rule already decided it."""
    rules, policy = _load(tmp_path)
    emails = [
        _email("grey_a", "news@shop.test", message_id="m_det"),
        EmailInput(message_id="m_esc", thread_id="grey_a", from_address="a@unknown.test",
                   subject="Re: promo", headers={}, body_snippet="et pour samedi?",
                   date="2026-06-12T00:00:00+00:00"),
    ]
    emails[0] = replace(emails[0], headers={"List-Unsubscribe": "<mailto:u@shop.test>"},
                        body_snippet="notre infolettre")
    fake = FakeClassifier()
    _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert fake.seen == ["m_esc"]
    assert "notre infolettre" in fake.histories[0]


def test_stage1_matches_on_cleartext_under_full_anonymization(tmp_path):
    """Anonymization scopes what Stage 2 sends to the LLM, never what Stage 1 reads:
    from_domain still matches with the widest scope active (STU-393)."""
    rules, policy = _load(tmp_path)
    policy = replace(policy, anonymization_fields=ANONYMIZATION_SCOPES["full"])
    fake = FakeClassifier()
    report = _run([_email("det1", "noreply@payments.interac.ca")], rules, policy, fake,
                  FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert fake.seen == []  # resolved deterministically, never escalated
    assert report.verdicts[0].path == "deterministic"
    assert report.verdicts[0].suggested_action == "keep_visible"


def test_every_verdict_carries_the_message_date(tmp_path):
    """`conversation.reconcile` reads recency off the verdict and holds a dateless thread
    rather than guessing (STU-940). A builder that stopped copying `date` would therefore
    stop reconciling conversations without failing anything — silently, on the hold side.
    """
    rules, policy = _load(tmp_path)
    emails = [
        _email("det1", "noreply@payments.interac.ca"),          # deterministic
        _email("grey1", "someone@unknown.test"),                # llm, ok
        _email("grey2", "other@unknown.test"),                  # llm, error
    ]
    fake = FakeClassifier({"grey2": Stage2Result(error="studio_run_timeout")})
    report = _run(emails, rules, policy, fake, FrontierLogger(log_path=tmp_path / "f.jsonl"))

    assert {v.path for v in report.verdicts} == {"deterministic", "llm"}
    assert {v.status for v in report.verdicts} == {"ok", "error"}
    assert [v.date for v in report.verdicts] == [e.date for e in emails]
