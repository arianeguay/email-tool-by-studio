import json

from email_classifier.core.contract import load_taxonomy
from email_classifier.core.types import EmailInput
from email_classifier.eval.harness import (
    Label,
    evaluate,
    labels_from_report,
    load_corpus,
    load_golden,
    replay,
    write_golden,
)
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import Rule, load_rules
from email_classifier.eval.__main__ import main
from tests.conftest import (
    EVAL_DIR,
    FIXTURES_DIR,
    RULES_DIR,
)

TAXONOMY = load_taxonomy(RULES_DIR / "taxonomy.yaml")


def _replay(corpus_dir, rules=None):
    return replay(
        load_corpus(corpus_dir),
        rules if rules is not None else load_rules(RULES_DIR / "rules.yaml"),
        load_policy(RULES_DIR / "policy.yaml"),
    )


def _replay_fixtures(rules=None):
    return _replay(FIXTURES_DIR, rules)


def test_committed_golden_has_no_flip():
    """The rule-regression gate: a rules.yaml edit that changes a golden verdict fails here."""
    result = evaluate(_replay_fixtures(), load_golden(EVAL_DIR / "golden.json"))
    assert result.flips == []
    assert result.precision == 1.0


def test_every_fixture_is_covered_by_golden():
    result = evaluate(_replay_fixtures(), load_golden(EVAL_DIR / "golden.json"))
    assert result.uncovered == []
    assert result.absent == []


def test_catch_all_only_rules_flip_every_deterministic_fixture():
    catch_all = [Rule(id="catch_all", conditions={}, bucket=None,
                      confidence="low", action="escalate_to_llm")]
    golden = load_golden(EVAL_DIR / "golden.json")
    result = evaluate(_replay_fixtures(catch_all), golden)
    expected_flips = sum(1 for label in golden.values() if label.path == "deterministic")
    assert result.newly_escalated == expected_flips
    assert all(f.kind == "path" for f in result.flips)
    assert result.ratio_deterministic == 0.0


def test_bucket_change_is_a_verdict_flip():
    golden = {"m1": Label(path="deterministic", bucket="A", action="keep_visible")}
    actual = {"m1": Label(path="deterministic", bucket="C", action="label_archive")}
    result = evaluate(actual, golden)
    assert [f.kind for f in result.flips] == ["verdict"]
    assert result.precision == 0.0


def test_uncached_escalation_compares_path_only():
    golden = {"m1": Label(path="llm", bucket="C", action="label_archive")}
    actual = {"m1": Label(path="llm", bucket=None, action=None)}
    assert evaluate(actual, golden).flips == []


def test_stage2_cache_supplies_the_llm_verdict():
    golden = load_golden(EVAL_DIR / "golden.json")
    email = next(e for e in load_corpus(FIXTURES_DIR) if golden[e.message_id].path == "llm")
    cache = {email.message_id: Label(path="llm", bucket="A", action="keep_visible")}
    labels = replay([email], load_rules(RULES_DIR / "rules.yaml"),
                    load_policy(RULES_DIR / "policy.yaml"), cache)
    assert labels[email.message_id] == Label(path="llm", bucket="A", action="keep_visible")


def test_seed_from_report_skips_error_verdicts():
    report = {
        "verdicts": [
            {"message_id": "ok1", "path": "deterministic", "status": "ok",
             "bucket": "B", "suggested_action": "label_archive"},
            {"message_id": "err1", "path": "llm", "status": "error",
             "bucket": None, "suggested_action": None},
        ]
    }
    assert labels_from_report(report) == {
        "ok1": Label(path="deterministic", bucket="B", action="label_archive")
    }


def test_golden_roundtrip(tmp_path):
    labels = {"m1": Label(path="llm", bucket=None, action=None)}
    path = tmp_path / "golden.json"
    write_golden(path, labels)
    assert load_golden(path) == labels
    assert json.loads(path.read_text())["m1"]["path"] == "llm"
    assert "unit" not in json.loads(path.read_text())["m1"]  # the default stays implicit


def test_a_conversation_label_is_carried_when_there_is_no_corpus_to_reconcile_with():
    """Only the corpus says which messages share a thread and in what order. Without it a
    thread label is neither a flip nor missing from the corpus — `promote` evaluates that
    way, and a golden it never scores is one it must not report as absent either."""
    golden = {
        "m1": Label(path="llm", bucket=None, action=None),
        "t1": Label(path="deterministic", bucket="A", action="needs_reply", unit="thread"),
    }
    result = evaluate({"m1": Label(path="llm", bucket=None, action=None)}, golden)

    assert result.flips == []
    assert result.absent == []
    assert result.labeled == 1
    assert result.conversations == ["t1"]
    assert result.conversations_scored == 0


def test_a_thread_id_equal_to_a_message_id_does_not_swallow_the_message_label():
    """Gmail spells a lone message's `threadId` with its own message id (`fixtures/` is
    full of them), so the two units collide in one key space and must be scored apart."""
    email = EmailInput(
        message_id="same", thread_id="same", from_address="a@b.ca", subject="s",
        headers={}, body_snippet="", date="2026-06-15T12:00:00+00:00",
    )
    actual = {"same": Label(path="deterministic", bucket="C", action="label_archive")}
    golden = {
        "same": Label(path="deterministic", bucket="C", action="label_archive"),
        "thread_other": Label(path="llm", bucket="A", action="needs_reply", unit="thread"),
    }

    result = evaluate(actual, golden, [email], TAXONOMY)

    assert result.flips == []
    assert result.labeled == 1               # the message; its thread carries no label
    assert result.absent == ["thread_other"]


def test_conversation_roundtrip_keeps_the_unit(tmp_path):
    labels = {"t1": Label(path="llm", bucket="A", action="needs_reply", unit="thread")}
    path = tmp_path / "golden.json"
    write_golden(path, labels)
    assert load_golden(path) == labels


def test_a_refreeze_carries_the_conversation_labels(tmp_path):
    """`make eval-threads-seed` replays, and a replay produces no thread label — so a
    re-freeze that did not carry them across would delete the reviewed answers."""
    out = tmp_path / "golden.json"
    reviewed = Label(path="llm", bucket="A", action="needs_reply", unit="thread")
    write_golden(out, {"thread_reviewed_by_hand": reviewed})

    main(["seed", "--corpus", str(FIXTURES_DIR), "--out", str(out),
          "--rules", str(RULES_DIR / "rules.yaml"),
          "--policy", str(RULES_DIR / "policy.yaml")])

    refrozen = load_golden(out)
    assert refrozen["thread_reviewed_by_hand"] == reviewed
    assert len(refrozen) > 1
