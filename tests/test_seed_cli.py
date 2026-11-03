"""Tests for the `seed` command's pre-flight and artifact merge (STU-893).

No mailbox: the Graph side is faked at the seams the command calls, so what is
under test is which rounds it agrees to write and what the golden and the dump
describe afterwards.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from email_classifier.seed import outlook

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture(name):
    return {
        "message_id": name,
        "thread_id": f"t_{name}",
        "from_address": f"{name}@example.ca",
        "subject": name,
        "headers": {},
        "body_snippet": name,
        "date": "2026-06-01T00:00:00+00:00",
    }


def _corpus(tmp_path, count):
    bucket = tmp_path / "corpus" / "bucket_a"
    bucket.mkdir(parents=True)
    for i in range(count):
        (bucket / f"a{i}.json").write_text(json.dumps(_fixture(f"a{i}")), encoding="utf-8")
    return tmp_path / "corpus"


def _thread_corpus(tmp_path, conversations):
    for name, length in conversations.items():
        directory = tmp_path / "threads" / name
        directory.mkdir(parents=True)
        for index in range(1, length + 1):
            fixture = _fixture(f"{name}_{index:02d}") | {
                "thread_id": f"t_{name}",
                "date": f"2026-06-0{index}T00:00:00+00:00",
            }
            (directory / f"{index:02d}.json").write_text(
                json.dumps(fixture), encoding="utf-8"
            )
    return tmp_path / "threads"


class _Seeder:
    """Records the round it was asked to write and hands back fresh mailbox ids."""

    def __init__(self):
        self.written = []

    def __call__(self, session, emails, run_id, mailbox):
        self.written = list(emails)
        return [f"new_{i}" for i in range(len(emails))]


class _ThreadSeeder:
    """Records each chain separately — the whole point is that they stay apart."""

    def __init__(self):
        self.chains = []

    def __call__(self, session, messages, run_id, mailbox):
        self.chains.append([m.message_id for m in messages])
        return [f"new_{len(self.chains)}_{i}" for i in range(len(messages))]


def _run(script, monkeypatch, tmp_path, argv, standing=(), corpus_dir=None):
    seeder = _Seeder()
    monkeypatch.setattr(outlook, "assert_test_mailbox", lambda: "test@outlook.com")
    monkeypatch.setattr(outlook, "build_session", lambda: object())
    monkeypatch.setattr(outlook, "find_standing", lambda session: list(standing))
    monkeypatch.setattr(outlook, "seed", seeder)
    monkeypatch.setattr(
        script, "OutlookFetcher",
        lambda session: type("F", (), {"fetch_inputs_by_ids": staticmethod(
            lambda ids: [_fixture(message_id) for message_id in ids]
        )})(),
    )
    script.main([
        "seed", "--corpus", str(corpus_dir or _corpus(tmp_path, 3)),
        "--golden", str(tmp_path / "golden.json"), "--dump", str(tmp_path / "dump.json"),
        *argv,
    ])
    return seeder


@pytest.fixture
def script():
    return load_script("seed_outlook")


def test_seed_refuses_a_mailbox_that_already_holds_a_round(script, monkeypatch, tmp_path):
    with pytest.raises(SystemExit) as refusal:
        _run(script, monkeypatch, tmp_path, [], standing=["old_1", "old_2"])

    assert "2 seeded message(s)" in str(refusal.value)
    assert "make unseed" in str(refusal.value)
    assert not (tmp_path / "golden.json").exists()


def test_a_default_round_draws_the_whole_corpus(script, monkeypatch, tmp_path):
    seeder = _run(script, monkeypatch, tmp_path, [])

    assert len(seeder.written) == 3
    assert len(json.loads((tmp_path / "golden.json").read_text())) == 3


def test_count_still_asks_for_a_smaller_round(script, monkeypatch, tmp_path):
    assert len(_run(script, monkeypatch, tmp_path, ["--count", "2"]).written) == 2


def test_add_stacks_a_round_and_merges_both_artifacts(script, monkeypatch, tmp_path):
    """A stacked round describes the whole mailbox — its own labels and the standing ones."""
    (tmp_path / "golden.json").write_text(json.dumps({
        "old_1": {"path": "deterministic", "bucket": "bucket_a", "action": "archive"},
        "torn_down": {"path": "llm", "bucket": None, "action": None},
    }), encoding="utf-8")
    (tmp_path / "dump.json").write_text(
        json.dumps([_fixture("old_1"), _fixture("torn_down")]), encoding="utf-8"
    )

    _run(script, monkeypatch, tmp_path, ["--add", "--count", "1"], standing=["old_1"])

    golden = json.loads((tmp_path / "golden.json").read_text())
    dump = json.loads((tmp_path / "dump.json").read_text())
    assert sorted(golden) == ["new_0", "old_1"]
    assert [item["message_id"] for item in dump] == ["old_1", "new_0"]


def test_threads_writes_one_chain_per_conversation(script, monkeypatch, tmp_path):
    """Each conversation is written on its own, or the store threads them together."""
    chainer = _ThreadSeeder()
    monkeypatch.setattr(outlook, "seed_thread", chainer)

    _run(script, monkeypatch, tmp_path, ["--threads"],
         corpus_dir=_thread_corpus(tmp_path, {"merci": 2, "suivi": 3}))

    assert chainer.chains == [
        ["merci_01", "merci_02"],
        ["suivi_01", "suivi_02", "suivi_03"],
    ]
    assert len(json.loads((tmp_path / "golden.json").read_text())) == 5


def test_threads_counts_conversations_and_never_half_of_one(script, monkeypatch, tmp_path):
    """A chain cut mid-thread would seed replies whose parent is not in the mailbox."""
    chainer = _ThreadSeeder()
    monkeypatch.setattr(outlook, "seed_thread", chainer)

    _run(script, monkeypatch, tmp_path, ["--threads", "--count", "1"],
         corpus_dir=_thread_corpus(tmp_path, {"merci": 2, "suivi": 3}))

    assert chainer.chains == [["merci_01", "merci_02"]]


def test_threads_refuses_a_bucket_mix(script, monkeypatch, tmp_path):
    monkeypatch.setattr(outlook, "seed_thread", _ThreadSeeder())

    with pytest.raises(SystemExit, match="does not apply"):
        _run(script, monkeypatch, tmp_path, ["--threads", "--mix", "bucket_a=100"],
             corpus_dir=_thread_corpus(tmp_path, {"merci": 2}))
