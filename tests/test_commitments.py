"""Commitments a drafted reply makes (STU-1065): detected, listed, settled by hand."""

import io
import json
import sys

import pytest
import yaml

from email_classifier import commitments
from tests.test_drafts import FakeDrafter, load_script, write_policy

HANDOFF = ("Bonjour,\nMerci de votre disponibilité; j'ai transféré votre message à la "
           "personne qui pourra gérer le tout.\nCordialement,\nL'équipe")
NOTHING = ("Bonjour,\nLes cours reprennent le 8 septembre. N'hésitez pas à nous écrire "
           "si vous avez des questions.\nCordialement,\nL'équipe")


@pytest.mark.parametrize("sentence", [
    "J'ai transféré votre message à la personne responsable.",
    "Votre demande a été transmise à la direction.",
    "Nous vous reviendrons là-dessus.",
    "Nous allons nous informer et vous répondre.",
    "La réponse vous sera confirmée.",
    "I've forwarded your message to the coordinator.",
    "We will get back to you next week.",
])
def test_a_claimed_or_promised_action_is_a_commitment(sentence):
    assert [c["sentence"] for c in commitments.detect(sentence)] == [sentence]


@pytest.mark.parametrize("sentence", [
    "Nous n'avons pas encore la date exacte.",
    "Nous vous remercions de votre message.",
    "Merci de nous confirmer votre présence.",
    "N'hésitez pas à nous écrire.",
])
def test_a_plain_statement_or_a_request_to_the_sender_is_not(sentence):
    assert commitments.detect(sentence) == []


def test_only_the_promising_sentence_is_kept():
    found = commitments.detect(HANDOFF)

    assert [c["sentence"] for c in found] == [
        "j'ai transféré votre message à la personne qui pourra gérer le tout."]
    assert "forward" in found[0]["obliges"]


# ── written by the write stage ───────────────────────────────────────────────

class PerThreadDrafter(FakeDrafter):
    def upsert_reply_draft(self, message_id, thread_id, body, reply_to=None, reply_name=None):
        self.calls.append((message_id, thread_id, body, reply_to, reply_name))
        return {"draft_id": f"draft-{thread_id}", "updated": False}


@pytest.fixture
def write(monkeypatch, tmp_path, capsys):
    module = load_script("write_draft")
    monkeypatch.setattr(module, "preflight_write", lambda provider: None)
    monkeypatch.setattr(module, "get_drafter", lambda provider: PerThreadDrafter())
    policy = write_policy(tmp_path)

    def run(thread_id, body, run_id="run-1"):
        item = {"message_id": f"m-{thread_id}", "thread_id": thread_id,
                "keymap": str(tmp_path / "keymap.json"),
                "journal": str(tmp_path / "drafts" / "journal.jsonl"),
                "run_id": run_id, "prompt_fingerprint": "fp-1", "policy": str(policy),
                "reply_to": "", "reply_name": ""}
        draft = {"thread_id": thread_id, "draft_body": body, "confidence": "high",
                 "sources_used": ["org:facts"]}
        payload = {"additional_context": yaml.safe_dump(item),
                   "previous_outputs": {"generate-draft": draft}}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        module.main()
        capsys.readouterr()

    return run


def listed(tmp_path):
    return commitments.unsettled(tmp_path / "drafts" / "commitments.jsonl")


def test_a_round_lists_the_one_promise_on_its_own_conversation(write, tmp_path):
    """The write stage alone records it: nothing waits for `drafts-post`, so a run that
    dies after this write still leaves the entry (STU-917)."""
    write("t-handoff", HANDOFF)
    write("t-plain", NOTHING)

    entries = listed(tmp_path)
    assert [(e["thread_id"], e["draft_id"], e["run_id"]) for e in entries] == [
        ("t-handoff", "draft-t-handoff", "run-1")]


def test_a_rerun_of_the_same_round_does_not_duplicate_it(write, tmp_path):
    write("t-handoff", HANDOFF)
    write("t-plain", NOTHING)
    write("t-handoff", HANDOFF, run_id="run-2")
    write("t-plain", NOTHING, run_id="run-2")

    assert len(listed(tmp_path)) == 1
    assert len(commitments.read(tmp_path / "drafts" / "commitments.jsonl")) == 1


def test_a_rewrite_that_drops_the_sentence_withdraws_it(write, tmp_path):
    """The group's next attempt rewrites the same draft: the promise no longer exists."""
    write("t-handoff", HANDOFF)
    write("t-handoff", NOTHING)

    assert listed(tmp_path) == []


def test_a_settled_entry_stays_settled_when_the_draft_is_rewritten_alike(write, tmp_path):
    path = tmp_path / "drafts" / "commitments.jsonl"
    write("t-handoff", HANDOFF)
    commitments.settle(path, listed(tmp_path)[0]["id"], "forwarded to the coordinator")

    write("t-handoff", HANDOFF, run_id="run-2")

    assert listed(tmp_path) == []
    (entry,) = commitments.latest(commitments.read(path)).values()
    assert (entry["status"], entry["note"]) == ("settled", "forwarded to the coordinator")


def test_a_later_runs_silence_settles_nothing(write, tmp_path):
    """Another draft on the same conversation (the first one was sent) leaves it open."""
    write("t-handoff", HANDOFF)
    path = tmp_path / "drafts" / "commitments.jsonl"
    commitments.record_draft(path, "run-2", "t-handoff", "another-draft", NOTHING)

    assert len(listed(tmp_path)) == 1


# ── the command a human works from ───────────────────────────────────────────

def test_the_command_lists_then_settles(write, tmp_path, capsys):
    cli = load_script("commitments")
    path = tmp_path / "drafts" / "commitments.jsonl"
    write("t-handoff", HANDOFF)

    assert cli.main(["--file", str(path)]) == 0
    out = capsys.readouterr().out
    (eid,) = [e["id"] for e in listed(tmp_path)]
    assert eid in out and "t-handoff" in out

    assert cli.main(["--file", str(path), "settle", eid, "--note", "done by phone"]) == 0
    assert listed(tmp_path) == []


def test_the_command_refuses_an_unknown_id_and_an_empty_note(tmp_path, capsys):
    cli = load_script("commitments")
    path = tmp_path / "commitments.jsonl"
    commitments.record_draft(path, "run-1", "t1", "d1", HANDOFF)
    (eid,) = [e["id"] for e in commitments.unsettled(path)]

    assert cli.main(["--file", str(path), "settle", "nope", "--note", "x"]) == 1
    assert cli.main(["--file", str(path), "settle", eid, "--note", "  "]) == 1
    assert len(commitments.unsettled(path)) == 1
