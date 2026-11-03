import re
from dataclasses import replace

from email_classifier.core.types import Classification, EmailInput
from email_classifier.eval import ab
from email_classifier.eval.harness import load_corpus
from email_classifier.eval.__main__ import _ab, parse_args
from email_classifier.orchestrate import conversation
from email_classifier.orchestrate.stage2_client import Stage2Result
from email_classifier.stage1.policy import ANONYMIZATION_SCOPES, filter_action, load_policy
from email_classifier.stage1.rule_engine import apply, load_rules
from tests.conftest import FIXTURES_DIR, RULES_DIR


class ScriptedClassifier:
    """One canned result per (message_id, call index), so an arm can be made to disagree
    with itself across repeats."""

    def __init__(self, script):
        self.script = script  # message_id -> list of (bucket, action) tuples or error tags
        self.calls = {}
        self.histories = []

    def classify(self, email, thread_history=""):
        self.histories.append(thread_history)
        index = self.calls.get(email.message_id, 0)
        self.calls[email.message_id] = index + 1
        entry = self.script[email.message_id][index]
        if isinstance(entry, str):
            return Stage2Result(error=entry)
        bucket, action = entry
        return Stage2Result(classification=Classification(
            thread_id=email.thread_id, bucket=bucket, subtype="product_newsletter",
            confidence="medium", suggested_action=action,
            suggested_label=None, reasoning="scripted",
        ))


def _email(message_id):
    return EmailInput(
        message_id=message_id, thread_id=message_id, from_address="a@unknown.test",
        subject="S", headers={}, body_snippet="b", date="2026-06-10T00:00:00+00:00",
    )


def _factory(script_by_arm, spawned=None):
    def factory(fields):
        classifier = ScriptedClassifier(script_by_arm["anonymized" if fields else "cleartext"])
        if spawned is not None:
            spawned.append((tuple(fields), classifier))
        return classifier

    return factory


def _escalated_ids(corpus, rules, policy):
    return [mid for c in ab.escalations(corpus, rules, policy) for mid in c.message_ids]


def _solo(message_id):
    mail = _email(message_id)
    return conversation.Escalation(head=mail, history=[], message_ids=(message_id,))


def _compare(script_by_arm, message_ids, repeats=2):
    conversations = [_solo(mid) for mid in message_ids]
    factory = _factory(script_by_arm)
    left = ab.run_arm("cleartext", [], conversations, factory, repeats)
    right = ab.run_arm("anonymized", ["body_snippet"], conversations, factory, repeats)
    return ab.compare(left, right, message_ids)


def test_stable_agreement_is_no_divergence():
    result = _compare(
        {
            "cleartext": {"m1": [("C", "label_archive")] * 2},
            "anonymized": {"m1": [("C", "label_archive")] * 2},
        },
        ["m1"],
    )
    assert result.divergences == []
    assert result.unstable == []
    assert result.errored == []


def test_stable_disagreement_is_a_divergence():
    result = _compare(
        {
            "cleartext": {"m1": [("A", "keep_visible")] * 2},
            "anonymized": {"m1": [("C", "label_archive")] * 2},
        },
        ["m1"],
    )
    assert [d.message_id for d in result.divergences] == ["m1"]
    assert result.divergences[0].left == ab.Verdict("A", "keep_visible")
    assert result.divergences[0].right == ab.Verdict("C", "label_archive")


def test_a_message_flipping_inside_one_arm_is_noise_not_a_divergence():
    """The STU-393 manual-validation failure mode: one borderline item flipped between two
    cleartext runs. Model noise must never be reported as an anonymization regression."""
    result = _compare(
        {
            "cleartext": {"m1": [("A", "keep_visible"), ("C", "label_archive")]},
            "anonymized": {"m1": [("C", "label_archive")] * 2},
        },
        ["m1"],
    )
    assert result.divergences == []
    assert result.unstable == ["m1"]


def test_stage2_error_excludes_the_message_from_the_comparison():
    result = _compare(
        {
            "cleartext": {"m1": ["studio_run_timeout", ("C", "label_archive")]},
            "anonymized": {"m1": [("A", "keep_visible")] * 2},
        },
        ["m1"],
    )
    assert result.divergences == []
    assert result.unstable == []
    assert result.errored == ["m1"]


def test_arms_are_classified_with_and_without_the_anonymized_fields():
    spawned = []
    conversations = [_solo("m1")]
    script = {arm: {"m1": [("C", "label_archive")] * 2} for arm in ("cleartext", "anonymized")}
    factory = _factory(script, spawned)
    ab.run_arm("cleartext", [], conversations, factory, 2)
    ab.run_arm("anonymized", ANONYMIZATION_SCOPES["full"], conversations, factory, 2)
    assert [fields for fields, _ in spawned] == [(), tuple(ANONYMIZATION_SCOPES["full"])]


def test_only_escalated_messages_are_selected():
    """Stage 1 runs on cleartext in both arms, so a deterministic verdict can never
    diverge — classifying it would burn LLM calls for a guaranteed tie."""
    rules = load_rules(RULES_DIR / "rules.yaml")
    policy = load_policy(RULES_DIR / "policy.yaml")
    corpus = load_corpus(FIXTURES_DIR)
    selected = _escalated_ids(corpus, rules, policy)
    assert selected
    assert len(selected) < len(corpus)
    by_id = {e.message_id: e for e in corpus}
    assert all(
        filter_action(apply(by_id[mid], rules), policy).action == "escalate_to_llm"
        for mid in selected
    )


def test_the_arm_sends_the_conversation_the_run_would_send():
    """STU-944: a thread is one call carrying its history, and the A/B has to test the
    prompt production sends — `thread_history` is its widest PII surface."""
    older = _email("m1")
    newer = replace(_email("m2"), thread_id="m1", date="2026-06-11T00:00:00+00:00",
                    body_snippet="et pour samedi?")
    conversations = [conversation.Escalation(
        head=newer, history=[replace(older, thread_id="m1")], message_ids=("m1", "m2"),
    )]
    spawned = []
    script = {"cleartext": {"m2": [("C", "label_archive")] * 2}}
    ab.run_arm("cleartext", [], conversations, _factory(script, spawned), 2)

    classifier = spawned[0][1]
    assert classifier.histories and all("b" in h for h in classifier.histories)
    # One call per repeat, not one per message, and both messages carry its verdict.
    assert len(classifier.histories) == 2


def _ab_args(tmp_path, **overrides):
    argv = ["ab", "--corpus", str(FIXTURES_DIR), "--policy", str(RULES_DIR / "policy.yaml"),
            "--rules", str(RULES_DIR / "rules.yaml")]
    for flag, value in overrides.items():
        argv += [f"--{flag.replace('_', '-')}", str(value)]
    return parse_args(argv)


def _uniform(message_ids, verdict, repeats=2):
    return {mid: [verdict] * repeats for mid in message_ids}


def test_cli_exits_0_when_no_arm_diverges(tmp_path, capsys):
    rules = load_rules(RULES_DIR / "rules.yaml")
    policy = load_policy(RULES_DIR / "policy.yaml")
    ids = _escalated_ids(load_corpus(FIXTURES_DIR), rules, policy)
    script = {arm: _uniform(ids, ("C", "label_archive")) for arm in ("cleartext", "anonymized")}
    assert _ab(_ab_args(tmp_path), _factory(script)) == 0
    assert "0 stable divergence(s)" in capsys.readouterr().out


def test_cli_exits_1_on_a_stable_divergence(tmp_path, capsys):
    rules = load_rules(RULES_DIR / "rules.yaml")
    policy = load_policy(RULES_DIR / "policy.yaml")
    ids = _escalated_ids(load_corpus(FIXTURES_DIR), rules, policy)
    script = {
        "cleartext": _uniform(ids, ("C", "label_archive")),
        "anonymized": _uniform(ids, ("A", "keep_visible")),
    }
    assert _ab(_ab_args(tmp_path), _factory(script)) == 1
    assert "DIVERGE" in capsys.readouterr().out


def test_cli_refuses_a_single_repeat_per_arm(tmp_path, capsys):
    assert _ab(_ab_args(tmp_path, repeats=1), _factory({})) == 1
    assert "--repeats must be >= 2" in capsys.readouterr().err


def test_cli_refuses_a_policy_with_anonymization_off(tmp_path, capsys):
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        re.sub(
            r"(?m)^(\s*scope:).*$", r"\1",
            (RULES_DIR / "policy.yaml").read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )
    assert _ab(_ab_args(tmp_path, policy=policy), _factory({})) == 1
    assert "anonymization is off" in capsys.readouterr().err


def test_a_provider_refusal_during_an_ab_does_not_page(monkeypatch, tmp_path, capsys):
    """The alert says escalated mail is going unclassified — true of a run, false of an A/B
    over committed fixtures (STU-1050). Built by `_ab`'s own factory, not injected: a
    classifier handed the flag directly would pass this without the wiring."""
    import subprocess

    from email_classifier import alerts

    sent = []
    monkeypatch.setattr(alerts, "notify", lambda message: sent.append(message))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0] if a else k["args"], 1, "you have hit your session limit", ""))

    assert _ab(_ab_args(tmp_path)) == 0
    assert "stage-2 error (not compared)" in capsys.readouterr().out
    assert sent == []
