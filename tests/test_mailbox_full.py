"""Tests for the `mailbox-full` chain (STU-846): its two script stages + its wiring."""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from email_classifier.apply.executor import PROCESSED_LABEL
from email_classifier.apply.journal import read_entries
from tests.conftest import FakeWriter, inboxed, stub_detector
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
PIPELINES = deployment.ROOT / ".studio" / "pipelines"
CONTRACTS = deployment.ROOT / ".studio" / "contracts"

EMAIL = {
    "message_id": "m1", "thread_id": "t1", "from_address": "a@b.ca",
    "subject": "Re: projet", "headers": {"From": "a@b.ca"},
    "body_snippet": "Bonjour.", "date": "2026-06-12T08:15:00+00:00",
    "provider_category": "primary",
}


def load_script(name):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fetch_mailbox():
    return load_script("fetch_mailbox")


@pytest.fixture
def apply_report():
    return load_script("apply_report")


def run_stage(module, ctx, monkeypatch, capsys):
    payload = json.dumps({"additional_context": yaml.safe_dump(ctx)})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    module.main()
    return json.loads(capsys.readouterr().out)


class FakeFetcher:
    def __init__(self, inputs, siblings=None):
        self.inputs = inputs
        self.siblings = siblings or {}     # thread_id -> extra EmailInput dicts
        self.calls = []

    def fetch_inputs(self, query, max_results, since=None):
        self.calls.append((query, max_results, since))
        return self.inputs

    def conversation_message_ids(self, thread_id):
        return [
            *(i["message_id"] for i in self.inputs if i["thread_id"] == thread_id),
            *(i["message_id"] for i in self.siblings.get(thread_id, [])),
        ]

    def fetch_inputs_by_ids(self, message_ids):
        by_id = {i["message_id"]: i for group in self.siblings.values() for i in group}
        return [by_id[message_id] for message_id in message_ids]


def write_policy(tmp_path, provider="outlook"):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump({
        "deterministic_can_finalize": True,
        "destructive_actions_allowed": ["mark_for_delete"],
        "frontier_log": {"file": True, "stdout": False},
        "email": {"provider": provider},
        "anonymization": {"scope": "body_only"},
        # `EMAIL` is dated, so today's default floor (STU-1123) would drop it and
        # every fetch test below would assert over an empty corpus.
        "start_date": "2000-01-01",
    }), encoding="utf-8")
    return path


def write_report(tmp_path, action="label_archive", run_id="run-1"):
    report = {
        "run_id": run_id,
        "verdicts": [{
            "message_id": "m1", "thread_id": "t1", "path": "deterministic", "status": "ok",
            "bucket": "B", "suggested_action": action, "confidence": "high",
            "rule_id": "r1", "subtype": None, "reasoning": None, "error": None,
        }],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


# ── fetch stage ──────────────────────────────────────────────────────────────

def test_fetch_names_the_dump_after_the_run_id_it_mints(
    fetch_mailbox, tmp_path, monkeypatch, capsys
):
    fetcher = FakeFetcher([EMAIL])
    monkeypatch.setattr(fetch_mailbox, "get_fetcher", lambda provider: fetcher)

    out = run_stage(
        fetch_mailbox,
        {"dump_dir": str(tmp_path), "query": "label:promotions", "max_results": 7,
         "policy": str(write_policy(tmp_path))},
        monkeypatch, capsys,
    )

    assert fetcher.calls == [
        ("label:promotions", 7, "2000-01-01T05:00:00+00:00")
    ]
    assert out["count"] == 1
    assert out["dump_path"] == str(tmp_path / f"{out['run_id']}.json")
    assert json.loads(Path(out["dump_path"]).read_text(encoding="utf-8")) == [EMAIL]


def test_fetch_publishes_the_floor_it_honoured(
    fetch_mailbox, tmp_path, monkeypatch, capsys
):
    """What is not in the dump and older than this was out of scope, not missed —
    the two are otherwise indistinguishable from what a run leaves (STU-1123)."""
    monkeypatch.setattr(fetch_mailbox, "get_fetcher", lambda provider: FakeFetcher([EMAIL]))

    out = run_stage(
        fetch_mailbox,
        {"dump_dir": str(tmp_path), "policy": str(write_policy(tmp_path))},
        monkeypatch, capsys,
    )

    assert out["start_date"] == "2000-01-01"


def test_fetch_publishes_the_mailbox_it_read(
    fetch_mailbox, tmp_path, monkeypatch, capsys
):
    """STU-1183: the classify stage puts this in the report, so the fetch has to say it."""
    monkeypatch.setattr(fetch_mailbox, "get_fetcher", lambda provider: FakeFetcher([EMAIL]))
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")

    out = run_stage(
        fetch_mailbox,
        {"dump_dir": str(tmp_path), "policy": str(write_policy(tmp_path))},
        monkeypatch, capsys,
    )

    assert out["mailbox"] == "inbox@club.example"


def test_fetch_publishes_an_empty_mailbox_when_nothing_names_one(
    fetch_mailbox, tmp_path, monkeypatch, capsys
):
    """A delegated grant addresses `/me`, and empty is how that crosses a template."""
    monkeypatch.setattr(fetch_mailbox, "get_fetcher", lambda provider: FakeFetcher([EMAIL]))
    monkeypatch.delenv("OUTLOOK_MAILBOX", raising=False)

    out = run_stage(
        fetch_mailbox,
        {"dump_dir": str(tmp_path), "policy": str(write_policy(tmp_path))},
        monkeypatch, capsys,
    )

    assert out["mailbox"] == ""


def test_a_fetch_leaves_behind_what_arrived_before_the_floor(
    fetch_mailbox, tmp_path, monkeypatch, capsys
):
    old = {**EMAIL, "message_id": "m0", "thread_id": "t0",
           "date": "1999-12-31T23:59:59+00:00"}
    monkeypatch.setattr(
        fetch_mailbox, "get_fetcher", lambda provider: FakeFetcher([old, EMAIL]))

    out = run_stage(
        fetch_mailbox,
        {"dump_dir": str(tmp_path), "policy": str(write_policy(tmp_path))},
        monkeypatch, capsys,
    )

    dumped = json.loads(Path(out["dump_path"]).read_text(encoding="utf-8"))
    assert [item["message_id"] for item in dumped] == ["m1"]


def test_two_fetches_leave_two_dumps(fetch_mailbox, tmp_path, monkeypatch, capsys):
    """STU-896: the shared path was overwritten every run, so the older corpus was gone."""
    monkeypatch.setattr(fetch_mailbox, "get_fetcher", lambda provider: FakeFetcher([EMAIL]))
    ctx = {"dump_dir": str(tmp_path), "policy": str(write_policy(tmp_path))}

    first = run_stage(fetch_mailbox, ctx, monkeypatch, capsys)
    second = run_stage(fetch_mailbox, ctx, monkeypatch, capsys)

    assert first["dump_path"] != second["dump_path"]
    assert Path(first["dump_path"]).exists() and Path(second["dump_path"]).exists()


def test_fetch_reads_the_mailbox_named_by_the_policy_not_by_the_input(
    fetch_mailbox, tmp_path, monkeypatch, capsys
):
    asked = []
    monkeypatch.setattr(
        fetch_mailbox, "get_fetcher",
        lambda provider: asked.append(provider) or FakeFetcher([]),
    )

    out = run_stage(
        fetch_mailbox,
        {"dump_dir": str(tmp_path), "provider": "claude-code",
         "policy": str(write_policy(tmp_path, "outlook"))},
        monkeypatch, capsys,
    )

    assert asked == ["outlook"]
    assert out["provider"] == "outlook"


def test_fetch_refuses_to_write_a_dump_that_breaks_the_schema(
    fetch_mailbox, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        fetch_mailbox, "get_fetcher",
        lambda provider: FakeFetcher([{**EMAIL, "subject": None}]),
    )
    dump_dir = tmp_path / "dumps"

    with pytest.raises(Exception):
        run_stage(fetch_mailbox,
                  {"dump_dir": str(dump_dir), "policy": str(write_policy(tmp_path))},
                  monkeypatch, capsys)

    assert not dump_dir.exists()


# ── classify-pre, on the run id the fetch minted ─────────────────────────────

@pytest.fixture
def classify_pre(monkeypatch):
    # These assert on the run id, the fan-out and the keymap — never on who is in the
    # text — so the stage runs against a stubbed detector and needs no `ner` extra.
    stub_detector(monkeypatch)
    return load_script("classify_pre")


def test_classify_pre_reports_under_the_run_id_it_was_given(
    classify_pre, tmp_path, monkeypatch, capsys
):
    """STU-896: the dump is named after the run id, so the report has to share it."""
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps([EMAIL]), encoding="utf-8")

    out = run_stage(
        classify_pre,
        {"corpus": str(dump), "run_id": "fetched-1", "log": str(tmp_path / "frontier.jsonl"),
         "keymap": str(tmp_path / "keymap.json")},
        monkeypatch, capsys,
    )

    assert out["run_id"] == "fetched-1"
    assert out["provenance"]["input_path"].endswith("dump.json")
    # The stage-1 record feeds every verdict `classify-post` assembles, and a verdict
    # with no date holds its conversation instead of reconciling it (STU-940).
    assert [r["date"] for r in out["stage1"]] == [EMAIL["date"]]


def test_classify_pre_records_the_mailbox_the_fetch_read(
    classify_pre, tmp_path, monkeypatch, capsys
):
    """STU-1183: carried from the fetch stage, so the report names the store its own
    ids resolve in — not whichever box this host is pointed at when it classifies."""
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps([EMAIL]), encoding="utf-8")
    monkeypatch.setenv("OUTLOOK_MAILBOX", "somewhere-else@club.example")

    out = run_stage(
        classify_pre,
        {"corpus": str(dump), "run_id": "r1", "mailbox": "inbox@club.example",
         "log": str(tmp_path / "frontier.jsonl"), "keymap": str(tmp_path / "keymap.json")},
        monkeypatch, capsys,
    )

    assert out["provenance"]["mailbox"] == "inbox@club.example"


@pytest.mark.parametrize("ctx", [{}, {"mailbox": ""}])
def test_classify_pre_names_no_mailbox_over_a_dump_nobody_fetched(
    classify_pre, tmp_path, monkeypatch, capsys, ctx
):
    """`run-batch` over a frozen dump is the rollback path: today's mailbox is
    routinely not the one those ids came out of, so it claims none (STU-1183)."""
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps([EMAIL]), encoding="utf-8")
    monkeypatch.setenv("OUTLOOK_MAILBOX", "inbox@club.example")

    out = run_stage(
        classify_pre,
        {"corpus": str(dump), "run_id": "r1", **ctx,
         "log": str(tmp_path / "frontier.jsonl"), "keymap": str(tmp_path / "keymap.json")},
        monkeypatch, capsys,
    )

    assert out["provenance"]["mailbox"] is None


def test_classify_pre_fans_out_once_per_conversation(
    classify_pre, tmp_path, monkeypatch, capsys
):
    """STU-944: three messages of one thread cost one fan-out item, and each of them
    points at it — `classify-post` hands all three the one verdict it produced."""
    thread = [
        {**EMAIL, "message_id": f"m{n}", "subject": f"tour {n}",
         "body_snippet": f"corps {n}", "date": f"2026-06-1{n}T08:15:00+00:00"}
        for n in (1, 2, 3)
    ]
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(thread), encoding="utf-8")

    out = run_stage(
        classify_pre,
        {"corpus": str(dump), "run_id": "r1", "log": str(tmp_path / "frontier.jsonl"),
         "keymap": str(tmp_path / "keymap.json")},
        monkeypatch, capsys,
    )

    assert all(r["escalated"] for r in out["stage1"])
    assert len(out["escalated"]) == 1
    assert out["escalated"][0]["body_snippet"] == "corps 3"      # the latest message
    assert "corps 1" in out["escalated"][0]["thread_history"]
    assert [r["escalation_index"] for r in out["stage1"]] == [0, 0, 0]


def test_classify_pre_writes_the_keymap_entries_this_run_minted(
    classify_pre, tmp_path, monkeypatch, capsys
):
    """STU-924: the shared keymap is seeded from the previous run, so an audit reading
    it credits one run with every entry the machine ever minted. The run's own file
    carries what this run saw and nothing else."""
    keymap = tmp_path / "keymap.json"
    keymap.write_text(json.dumps({"EMAIL_1": "run-precedente@x.ca"}), encoding="utf-8")
    dump = tmp_path / "dump.json"
    dump.write_text(
        json.dumps([{**EMAIL, "body_snippet": "Bonjour, écrire à parent@exemple.ca."}]),
        encoding="utf-8",
    )

    run_stage(
        classify_pre,
        {"corpus": str(dump), "run_id": "run-1", "log": str(tmp_path / "frontier.jsonl"),
         "keymap": str(keymap)},
        monkeypatch, capsys,
    )

    minted = json.loads((tmp_path / "run-1.json").read_text(encoding="utf-8"))
    assert "parent@exemple.ca" in minted.values()
    assert "run-precedente@x.ca" not in minted.values()
    # The shared keymap still carries it: the seeding is what keeps a value's token
    # stable across runs, and this artifact records rather than replaces it.
    assert "run-precedente@x.ca" in json.loads(
        keymap.read_text(encoding="utf-8")).values()


# ── apply stage ──────────────────────────────────────────────────────────────

@pytest.fixture
def writer(apply_report, monkeypatch):
    fake = inboxed(FakeWriter(), "m1")
    monkeypatch.setattr(apply_report, "get_writer", lambda provider: fake)
    return fake


def test_apply_defaults_to_a_dry_run(apply_report, writer, tmp_path, monkeypatch, capsys):
    out = run_stage(
        apply_report,
        {"report": str(write_report(tmp_path)), "journal_dir": str(tmp_path / "applied"),
         "policy": str(write_policy(tmp_path, "gmail"))},
        monkeypatch, capsys,
    )

    assert out["dry_run"] is True
    assert writer.labels_of("m1") == {"INBOX"}
    plan = read_entries(tmp_path / "applied" / "run-1.plan.jsonl")
    assert {e["status"] for e in plan} == {"planned"}


@pytest.mark.parametrize("apply_value", [False, "true", "yes", None])
def test_only_a_real_boolean_true_mutates_the_mailbox(
    apply_report, writer, tmp_path, monkeypatch, capsys, apply_value
):
    out = run_stage(
        apply_report,
        {"report": str(write_report(tmp_path)), "journal_dir": str(tmp_path / "applied"),
         "policy": str(write_policy(tmp_path, "gmail")),
         "apply": apply_value},
        monkeypatch, capsys,
    )

    assert out["dry_run"] is True
    assert writer.labels_of("m1") == {"INBOX"}


def test_apply_true_mutates_and_journals(apply_report, writer, tmp_path, monkeypatch, capsys):
    out = run_stage(
        apply_report,
        {"report": str(write_report(tmp_path)), "journal_dir": str(tmp_path / "applied"),
         "policy": str(write_policy(tmp_path, "gmail")),
         "apply": True},
        monkeypatch, capsys,
    )

    assert out["dry_run"] is False
    assert out["acted"] == 1
    assert writer.labels_of("m1") == {"email-tool/B", PROCESSED_LABEL}
    assert {e["status"] for e in read_entries(tmp_path / "applied" / "run-1.jsonl")} == {"ok"}


def test_a_failed_operation_fails_the_stage(apply_report, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        apply_report, "get_writer",
        lambda provider: inboxed(FakeWriter(fail_on=["m1"]), "m1"),
    )

    with pytest.raises(SystemExit):
        run_stage(
            apply_report,
            {"report": str(write_report(tmp_path)), "journal_dir": str(tmp_path / "applied"),
             "policy": str(write_policy(tmp_path, "gmail")),
             "apply": True},
            monkeypatch, capsys,
        )

    assert {e["status"] for e in read_entries(tmp_path / "applied" / "run-1.jsonl")} == {"ok", "error"}


def test_apply_archives_an_expired_conversation_an_earlier_report_decided(
    apply_report, monkeypatch, tmp_path, capsys
):
    """Tomorrow's fetch does not reach back to today's code (STU-2070): its expiry is
    read from the report that decided it, and the archive is journaled by this run."""
    writer = inboxed(FakeWriter(), "m1", "old")
    monkeypatch.setattr(apply_report, "get_writer", lambda provider: writer)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "run-0.json").write_text(json.dumps({"run_id": "run-0", "verdicts": [{
        "message_id": "old", "thread_id": "t-old", "path": "deterministic",
        "status": "ok", "bucket": "A", "suggested_action": "keep_visible",
        "confidence": "high", "rule_id": "code", "subtype": None, "reasoning": None,
        "error": None, "date": "2026-01-01T08:00:00+00:00",
        "expires_at": "2026-01-01T09:00:00+00:00",
    }]}), encoding="utf-8")
    current = write_report(tmp_path).rename(reports / "run-1.json")

    run_stage(
        apply_report,
        {"report": str(current), "journal_dir": str(tmp_path / "applied"),
         "policy": str(write_policy(tmp_path, "gmail")), "apply": True},
        monkeypatch, capsys,
    )

    assert writer.labels_of("old") == {"email-tool/A", PROCESSED_LABEL}
    journaled = {e["message_id"] for e in read_entries(tmp_path / "applied" / "run-1.jsonl")}
    assert journaled == {"m1", "old"}


# ── wiring ───────────────────────────────────────────────────────────────────

def load_pipeline(name):
    return yaml.safe_load((PIPELINES / f"{name}.pipeline.yaml").read_text(encoding="utf-8"))


def test_the_parent_chains_the_children_in_order():
    stages = load_pipeline("mailbox-full")["stages"]
    assert [s["call"] for s in stages] == [
        "fetch", "classify-batch", "apply", "drafts", "calendar"]


def test_only_the_fetch_child_reads_the_parent_input():
    stages = load_pipeline("mailbox-full")["stages"]
    assert "input" not in stages[0]
    report = "{{stages.classify-batch.output.report_path}}"
    assert stages[2]["input"]["report"] == report
    assert stages[3]["input"]["report"] == report
    assert stages[4]["input"]["report"] == report


def test_the_children_classify_and_draft_the_dump_the_fetch_wrote():
    """STU-896: a corpus path in the input is a path two runs share."""
    stages = load_pipeline("mailbox-full")["stages"]
    dump = "{{stages.fetch.output.dump_path}}"
    assert stages[1]["input"]["corpus"] == dump
    assert stages[3]["input"]["corpus"] == dump
    assert stages[4]["input"]["corpus"] == dump
    assert stages[1]["input"]["run_id"] == "{{stages.fetch.output.run_id}}"


def test_the_classify_child_is_handed_the_floor_and_the_mailbox_of_the_fetch():
    """Both are statements about the read that happened, not about the moment the
    classify stage runs (STU-1123, STU-1183). This template is the only thing tying
    them to it, so a report stops naming either the moment it goes."""
    stages = load_pipeline("mailbox-full")["stages"]
    assert stages[1]["input"]["start_date"] == "{{stages.fetch.output.start_date}}"
    assert stages[1]["input"]["mailbox"] == "{{stages.fetch.output.mailbox}}"


def test_only_the_drafts_and_calendar_children_tolerate_a_failure():
    stages = load_pipeline("mailbox-full")["stages"]
    tolerated = {s["call"] for s in stages if s.get("on_failure") == "continue"}
    assert tolerated == {"drafts", "calendar"}


def test_every_called_pipeline_and_its_scripts_and_contracts_exist():
    for stage in load_pipeline("mailbox-full")["stages"]:
        child = load_pipeline(stage["call"])
        for child_stage in child["stages"]:
            if "script" in child_stage:
                assert (REPO_ROOT / child_stage["script"]).exists()
            if "contract" in child_stage:
                assert (CONTRACTS / f"{child_stage['contract']}.contract.yaml").exists()
