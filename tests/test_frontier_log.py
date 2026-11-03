import json
from pathlib import Path

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.stage1.frontier_log import FrontierLogger


def _make_email(thread_id: str = "t001", message_id: str | None = None) -> EmailInput:
    return EmailInput(
        message_id=message_id or f"m_{thread_id}", thread_id=thread_id,
        from_address="sender@example.com", subject="Test", headers={},
        body_snippet="Hello", date="2026-06-10T14:30:00+00:00",
    )


def _make_match(rule_id: str = "test_rule", action: str = "keep_visible", action_original: str | None = None) -> RuleMatch:
    return RuleMatch(rule_id=rule_id, action=action, action_original=action_original, confidence="high", bucket="A")


def test_log_creates_file_on_first_entry(tmp_path: Path):
    log_path = tmp_path / "logs" / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    assert not log_path.exists()
    logger.log(_make_email(), _make_match())
    assert log_path.exists()


def test_log_writes_valid_jsonl(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    logger.log(_make_email("thread_001"), _make_match())
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["thread_id"] == "thread_001"
    assert entry["rule_id"] == "test_rule"
    assert entry["action"] == "keep_visible"
    assert entry["action_original"] is None
    assert entry["confidence"] == "high"
    assert entry["bucket"] == "A"
    assert "run_id" in entry
    assert "timestamp" in entry
    assert entry["message_id"] == "m_thread_001"


def test_log_appends_multiple_entries(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    logger.log(_make_email("t001"), _make_match())
    logger.log(_make_email("t002"), _make_match(rule_id="catch_all", action="escalate_to_llm"))
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["thread_id"] == "t001"
    assert json.loads(lines[1])["thread_id"] == "t002"


def test_run_id_is_consistent_within_logger(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    logger.log(_make_email("t001"), _make_match())
    logger.log(_make_email("t002"), _make_match())
    lines = log_path.read_text().strip().splitlines()
    run_ids = [json.loads(l)["run_id"] for l in lines]
    assert run_ids[0] == run_ids[1]


def test_two_loggers_have_different_run_ids(tmp_path: Path):
    log_a = tmp_path / "a.jsonl"
    log_b = tmp_path / "b.jsonl"
    logger_a = FrontierLogger(log_path=log_a)
    logger_b = FrontierLogger(log_path=log_b)
    logger_a.log(_make_email(), _make_match())
    logger_b.log(_make_email(), _make_match())
    run_a = json.loads(log_a.read_text().strip())["run_id"]
    run_b = json.loads(log_b.read_text().strip())["run_id"]
    assert run_a != run_b


def test_action_original_logged_when_policy_revised(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    match = _make_match(action="label_archive", action_original="mark_for_delete")
    logger.log(_make_email(), match)
    entry = json.loads(log_path.read_text().strip())
    assert entry["action"] == "label_archive"
    assert entry["action_original"] == "mark_for_delete"


def test_stdout_false_does_not_print(tmp_path: Path, capsys):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path, stdout=False)
    logger.log(_make_email(), _make_match())
    captured = capsys.readouterr()
    assert captured.out == ""


def test_stdout_true_prints_json(tmp_path: Path, capsys):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path, stdout=True)
    logger.log(_make_email("t_stdout"), _make_match())
    captured = capsys.readouterr()
    assert captured.out.strip()
    entry = json.loads(captured.out.strip())
    assert entry["thread_id"] == "t_stdout"


def test_make_logger_reads_stdout_from_policy(tmp_path: Path):
    from email_classifier.stage1.frontier_log import make_logger
    from email_classifier.stage1.policy import Policy

    policy = Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=[],
        frontier_log_file=True,
        frontier_log_stdout=True,
    )
    logger = make_logger(policy, tmp_path / "frontier.jsonl")
    assert logger.stdout is True
