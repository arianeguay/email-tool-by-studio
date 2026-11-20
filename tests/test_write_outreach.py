"""The write stage of `outreach-draft` (STU-2121)."""

import pytest
import yaml

from tests.test_drafts import load_script, run_stage, write_policy


class FakeDrafter:
    def __init__(self):
        self.calls = []

    def create_draft(self, key, to, subject, body):
        self.calls.append((key, to, subject, body))
        return {"draft_id": f"outreach-{key}@example.ca", "created": True}


def _payload(tmp_path, provider="imap", **draft):
    item = {"key": "TASK-1", "policy": str(write_policy(tmp_path, provider=provider))}
    composed = {"to": "", "subject": " Soumission ", "draft_body": "Bonjour",
                "sources_used": ["task"], **draft}
    return {"additional_context": yaml.safe_dump(item),
            "previous_outputs": {"compose-outreach": composed}}


def test_the_draft_is_keyed_on_the_item_and_an_empty_to_means_no_recipient(
    tmp_path, monkeypatch, capsys
):
    module = load_script("write_outreach")
    drafter = FakeDrafter()
    monkeypatch.setattr(module, "get_drafter", lambda provider: drafter)

    out, _ = run_stage(module, _payload(tmp_path), monkeypatch, capsys)

    assert drafter.calls == [("TASK-1", None, "Soumission", "Bonjour")]
    assert (out["key"], out["draft_id"], out["created"]) == (
        "TASK-1", "outreach-TASK-1@example.ca", True)


def test_a_provider_without_new_message_drafts_is_refused(tmp_path, monkeypatch, capsys):
    module = load_script("write_outreach")

    with pytest.raises(SystemExit, match="only on imap"):
        run_stage(module, _payload(tmp_path, provider="outlook"), monkeypatch, capsys)


def test_an_empty_body_is_refused(tmp_path, monkeypatch, capsys):
    module = load_script("write_outreach")

    with pytest.raises(SystemExit, match="empty draft"):
        run_stage(module, _payload(tmp_path, draft_body="  "), monkeypatch, capsys)
