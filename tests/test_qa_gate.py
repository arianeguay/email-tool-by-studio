"""The frozen known-bad drafts and the harness that replays them (STU-1078)."""

import json
from pathlib import Path

import pytest
import yaml

from email_classifier import qa_gate, qa_provider, qa_replay, qa_studio_version, tree_guard
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = deployment.ROOT / "eval" / "draft-qa-rejections.json"
DRAFT_CONTRACT = deployment.ROOT / ".studio" / "contracts" / "qa-replay-draft.contract.yaml"

DRAFT_FIELDS = ("thread_id", "draft_id", "draft_body", "confidence", "sources_used")

# A resolved provider, the way every harness records one (STU-1175): what ran, not
# what a flag asked for.
PROVIDER = "anthropic"

# A resolved Studio version, the way every harness now records one too (STU-1241).
STUDIO_VERSION = "0.19.0"


def _entry(**overrides):
    return {
        "id": "a-case",
        "gate": "The draft asserts an action nobody has taken.",
        "expected_verdict": "rejected",
        "source": "STU-0000 — a run that approved it.",
        "item": {"thread_id": "T", "body_text": "bonjour"},
        "draft": {f: "x" for f in DRAFT_FIELDS},
    } | overrides


def _corpus(tmp_path, *entries):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(list(entries)), encoding="utf-8")
    return path


def _case(verdict, unreachable=(), empty_store=(), **overrides):
    entry = _entry(**overrides)
    case = qa_gate.Case(
        entry["id"], entry["gate"], entry["source"], entry["item"], entry["draft"]
    )
    return qa_gate.Outcome(case, verdict, unreachable, empty_store)


def test_a_well_formed_entry_loads(tmp_path):
    cases = qa_gate.load_corpus(_corpus(tmp_path, _entry()), DRAFT_FIELDS)

    assert [c.id for c in cases] == ["a-case"]
    assert cases[0].draft["draft_body"] == "x"


def test_an_entry_expecting_approval_is_refused(tmp_path):
    """The one thing this corpus must not become: a frozen approval. A gate that stops
    firing then reads as the corpus agreeing with it."""
    path = _corpus(tmp_path, _entry(expected_verdict="approved"))

    with pytest.raises(ValueError, match="only holds drafts a gate must reject"):
        qa_gate.load_corpus(path, DRAFT_FIELDS)


def test_a_draft_missing_a_contract_field_is_refused_before_any_call(tmp_path):
    """Every entry costs a live reviewer pass, so an entry the pipeline would reject on
    its contract should say so for free."""
    draft = {f: "x" for f in DRAFT_FIELDS if f != "sources_used"}
    path = _corpus(tmp_path, _entry(draft=draft))

    with pytest.raises(ValueError, match="missing sources_used"):
        qa_gate.load_corpus(path, DRAFT_FIELDS)


def test_duplicate_ids_are_refused(tmp_path):
    """The id is how a failure names itself; two cases sharing one make the report
    ambiguous about which draft stopped being rejected."""
    path = _corpus(tmp_path, _entry(), _entry(draft={f: "y" for f in DRAFT_FIELDS}))

    with pytest.raises(ValueError, match="duplicate id"):
        qa_gate.load_corpus(path, DRAFT_FIELDS)


def test_an_entry_naming_no_gate_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no gate named"):
        qa_gate.load_corpus(_corpus(tmp_path, _entry(gate="  ")), DRAFT_FIELDS)


def test_a_rejected_verdict_holds():
    assert qa_gate.failures([_case("rejected")]) == []


def test_an_approved_verdict_is_the_finding():
    outcomes = [_case("approved")]

    assert len(qa_gate.failures(outcomes)) == 1
    assert "HELD NO LONGER" in qa_gate.render(outcomes, PROVIDER, STUDIO_VERSION)


def test_an_unreadable_verdict_fails_rather_than_passing():
    """A reviewer that did not judge did not approve either — but it did not reject,
    and counting the silence as held is what this corpus exists against."""
    outcomes = [_case(qa_replay.ERROR)]

    assert len(qa_gate.failures(outcomes)) == 1
    assert "could not be read" in qa_gate.render(outcomes, PROVIDER, STUDIO_VERSION)


def test_a_failure_names_the_gate_and_where_the_draft_came_from():
    rendered = qa_gate.render([_case("approved")], PROVIDER, STUDIO_VERSION)

    assert "The draft asserts an action nobody has taken." in rendered
    assert "STU-0000" in rendered


def test_the_committed_corpus_is_drivable():
    """The check `make qa-gate` runs at preflight, run here too: it costs a live Opus
    call to discover there, and nothing else in the fast suite reads this file."""
    fields = yaml.safe_load(DRAFT_CONTRACT.read_text(encoding="utf-8"))["schema"][
        "required_fields"
    ]
    cases = qa_gate.load_corpus(CORPUS, fields)

    assert cases, "the corpus is empty"


def test_the_committed_corpus_names_nobody():
    """Committable only because it is de-identified: the mail carries tokens, and a
    draft the club never sent names no one. A cleartext name landing here is a leak."""
    cases = qa_gate.load_corpus(CORPUS, DRAFT_FIELDS)
    for case in cases:
        blob = json.dumps([case.item, case.draft], ensure_ascii=False)
        assert "@" not in case.item["from_address"] or case.item["from_address"].startswith(
            ("EMAIL_", "PERSON_")
        ), f"{case.id}: cleartext sender"
        assert "PERSON_X" not in blob and "EMAIL_X" not in blob, (
            f"{case.id}: an index-free placeholder is a different defect than the one "
            "the entry names, and the reviewer rejects on it"
        )


def test_the_replayed_item_carries_the_harness_run_id(tmp_path):
    """Studio logs the input, so a replay keeping the recorded run id makes its journal
    one more log naming that report — and `audit_run.py cost` refuses it for ambiguity."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["item"] = yaml.safe_load(Path(argv[argv.index("--input-file") + 1]).read_text())
        raise AssertionError("stop before the call")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(qa_replay.subprocess, "run", fake_run)
        with pytest.raises(AssertionError):
            qa_replay.judge({"thread_id": "T"}, {"thread_id": "T"}, tmp_path, "new-id",
                            REPO_ROOT)

    assert captured["item"]["run_id"] == "new-id"


def test_the_gate_leaves_the_reviewers_model_pin_alone(tmp_path, monkeypatch):
    """`make qa-bakeoff` flips the model; the gate asks about the one production pins,
    so it must not set the override at all."""
    monkeypatch.delenv(qa_replay.MODEL_ENV, raising=False)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs["env"]
        raise AssertionError("stop before the call")

    monkeypatch.setattr(qa_replay.subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        qa_replay.judge({}, {}, tmp_path, "id", REPO_ROOT)

    assert qa_replay.MODEL_ENV not in captured["env"]
    assert captured["env"][qa_replay.DRAFT_ENV].endswith("draft.json")


def test_a_run_record_with_no_qa_stage_reads_as_an_error():
    record = json.dumps({"stages": [{"stage_name": "replay-draft", "output": {}}]})

    assert qa_replay.verdict_of(record) == qa_replay.ERROR
    assert qa_replay.verdict_of("not json") == qa_replay.ERROR


def test_the_verdict_is_read_off_the_stage_not_the_run_status():
    """A rejected draft ends the pipeline as `rejected`; the stage that said so still
    carries its output, and that output is the measurement."""
    record = json.dumps({
        "status": "rejected",
        "stages": [{"stage_name": "draft-qa", "output": {"verdict": "rejected"}}],
    })

    assert qa_replay.verdict_of(record) == "rejected"


# --- the fingerprint that says whether the gate needs replaying (STU-1086) ---

RECORD = deployment.ROOT / "eval" / "draft-qa-gate.json"


def _record(tmp_path, sources, **overrides):
    """Stamped the way a passing run stamps it, so a test about a moved leg is about a
    moved leg and not about the digest guarding provenance (STU-1153)."""
    path = tmp_path / "record.json"
    body = {"date": "2026-01-01", "cases": 1, "sources": sources} | overrides
    path.write_text(
        json.dumps(body | {qa_gate.DIGEST_KEY: qa_gate.self_digest(body)}),
        encoding="utf-8",
    )
    return path


def test_an_unchanged_reviewer_needs_no_replay(tmp_path, capsys):
    today = {"a": "1"}
    assert qa_gate.check(today, _record(tmp_path, today)) == 0


def test_an_edited_reviewer_asks_for_a_replay_and_names_the_file(tmp_path, capsys):
    record = _record(tmp_path, {".studio/agents/draft-reviewer.agent.yaml": "old"})
    today = {".studio/agents/draft-reviewer.agent.yaml": "new"}
    assert qa_gate.check(today, record) == qa_gate.RECORD_MOVED
    out = capsys.readouterr().out
    assert ".studio/agents/draft-reviewer.agent.yaml" in out
    assert "make qa-gate" in out


def test_the_red_check_names_what_the_repair_needs(tmp_path, capsys):
    """The repair is unavailable on a machine with no app-only grant, and exit 4 is
    where that is otherwise learned — one reviewer pass after the push went red."""
    record = _record(tmp_path, {"corpus": "old"})
    assert qa_gate.check({"corpus": "new"}, record) == qa_gate.RECORD_MOVED
    out = capsys.readouterr().out
    assert "OUTLOOK_TENANT_ID" in out
    assert "OUTLOOK_CLIENT_SECRET" in out


def test_a_grown_corpus_is_sent_to_the_live_source_replay_too(tmp_path, capsys):
    """`make qa-gate` never asks whether a rejection turns on a live source, so an
    entry added here widens what STU-1114 measured on a tree the gate turns green
    again (STU-1166). The reader is at this message when they add it."""
    record = _record(tmp_path, {qa_gate.CORPUS_LEG: "old"})
    assert qa_gate.check({qa_gate.CORPUS_LEG: "new"}, record) == qa_gate.RECORD_MOVED
    assert "make qa-live-sources" in capsys.readouterr().out


def test_an_edited_reviewer_alone_is_not_sent_there(tmp_path, capsys):
    """Only the corpus leg carries the second repair: the reviewer's prose moving is
    the first measurement's question, and naming both every time is how the extra line
    stops being read."""
    record = _record(tmp_path, {".studio/agents/draft-reviewer.agent.yaml": "old"})
    assert qa_gate.check(
        {".studio/agents/draft-reviewer.agent.yaml": "new"}, record
    ) == qa_gate.RECORD_MOVED
    assert "make qa-live-sources" not in capsys.readouterr().out


def test_a_missing_record_is_not_agreement(tmp_path, capsys):
    assert qa_gate.check({"a": "1"}, tmp_path / "absent.json") == qa_gate.RECORD_UNRECORDED
    assert qa_gate.check({"a": "1"}, _record(tmp_path, {})) == qa_gate.RECORD_UNRECORDED


def test_a_source_deleted_out_from_under_the_gate_reads_as_moved(tmp_path):
    """Not a crash: a source the gate can no longer read is the strongest reason to
    replay it, so it has to reach the same answer an edit does."""
    record = _record(tmp_path, {"gone.yaml": "abc"})
    today = qa_gate.fingerprint(tmp_path, tmp_path / "corpus.json")
    assert today["agent"] == "absent"
    assert qa_gate.check(today, record) == qa_gate.RECORD_MOVED


def test_the_fingerprint_covers_the_corpus_itself(tmp_path):
    corpus = _corpus(tmp_path, _entry())
    assert qa_gate.CORPUS_LEG in qa_gate.fingerprint(deployment.ROOT, CORPUS)
    grown = qa_gate.fingerprint(tmp_path, corpus)
    corpus.write_text(json.dumps([_entry(), _entry(id="b-case")]), encoding="utf-8")
    assert qa_gate.fingerprint(tmp_path, corpus) != grown


def test_the_fingerprint_leaves_the_drafter_out():
    """`drafts._PROMPT_SOURCES` is the wrong set here in both directions — it carries
    files the replay never loads, and misses ones it does."""
    paths = qa_gate.sources(deployment.ROOT).values()
    assert ".studio/agents/drafter.agent.yaml" not in paths
    assert ".studio/contracts/write-draft.contract.yaml" not in paths
    assert ".studio/pipelines/draft-qa-replay.pipeline.yaml" in paths
    assert ".studio/contracts/qa-replay-draft.contract.yaml" in paths


def test_every_source_the_reviewer_reads_exists():
    """A path that silently digests as `absent` would pin a typo as a stable leg."""
    missing = [
        s for s in qa_gate.sources(deployment.ROOT).values()
        if not (deployment.ROOT / s).exists()
    ]
    assert not missing, missing


def test_the_reviewers_agent_skills_and_tools_are_all_fingerprinted():
    """Read off the agent rather than restated (now literally — `qa_gate.sources`
    derives them the same way), so a skill or tool it gains is covered here instead of
    a silent gate later."""
    agent = yaml.safe_load(
        (deployment.ROOT / ".studio" / "agents" / "draft-reviewer.agent.yaml").read_text(
            encoding="utf-8"
        )
    )
    paths = qa_gate.sources(deployment.ROOT).values()
    for skill in agent["skills"]:
        assert f".studio/skills/{skill}.skill.md" in paths
    for tool in agent["tools"]:
        plugin = tool.split("-")[0].replace("_", "-")
        assert any(f"/{plugin}" in s for s in paths), tool


def test_a_record_is_only_written_for_a_gate_that_held():
    """It states that these sources were verified. A run that found a draft approved
    verified the opposite, and its stale record is what keeps the check failing."""
    outcomes = [_case(qa_gate.REJECTED)]
    record = qa_gate.as_record(outcomes, {"a": "1"}, "run-1", "2026-01-01", PROVIDER, STUDIO_VERSION)
    assert record["held"] == ["a-case"]
    assert record["cases"] == 1


def test_the_committed_record_still_describes_this_tree(capsys):
    """The push-time question itself, on the real record — the whole mechanism."""
    today = qa_gate.fingerprint(deployment.ROOT, CORPUS)
    assert qa_gate.check(today, RECORD) == 0, capsys.readouterr().out


def test_the_committed_record_names_nobody():
    """Committable on the corpus's own terms: digests and gate ids, no mail.

    `provider` and `studio_version` are optional here and required of `as_record`
    (STU-1175, STU-1241): the committed record was stamped before each field existed,
    and the only thing that can add one is the next pass — writing it in by hand is the
    assembled record the digest refuses.
    """
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    core = {"date", "run_id", "cases", "held", "sources", qa_gate.DIGEST_KEY}
    assert core <= set(record) <= core | {qa_provider.KEY, qa_studio_version.KEY}
    assert all(len(digest) <= 12 for digest in record["sources"].values())


# --- the digest that refuses a record no single run produced (STU-1153) ---


def _assembled(tmp_path, record_path, **edits):
    """A merge, in the form it actually takes: fields rewritten under a record that was
    never re-stamped, because git resolved two `sources` maps rather than a run."""
    body = json.loads(record_path.read_text(encoding="utf-8")) | edits
    record_path.write_text(json.dumps(body), encoding="utf-8")
    return record_path


def test_a_record_a_run_stamped_verifies(tmp_path, capsys):
    outcomes = [_case(qa_gate.REJECTED)]
    sources = {".studio/agents/draft-reviewer.agent.yaml": "abc"}
    path = tmp_path / "stamped.json"
    path.write_text(
        json.dumps(qa_gate.as_record(outcomes, sources, "run-1", "2026-01-01",
                                     PROVIDER, STUDIO_VERSION)),
        encoding="utf-8",
    )

    assert qa_gate.check(sources, path) == 0, capsys.readouterr().out


def test_a_hand_edited_source_entry_is_refused_as_assembled(tmp_path, capsys):
    """The dangerous merge is the clean one: two branches touching disjoint sources
    leave every leg matching the merged tree, so the leg comparison reads green."""
    leg = ".studio/skills/club-voice.skill.md"
    record = _record(tmp_path, {leg: "old", "corpus": "same"})
    merged = {leg: "theirs", "corpus": "same"}
    _assembled(tmp_path, record, sources=merged)

    assert qa_gate.check(merged, record) == qa_gate.RECORD_ASSEMBLED
    out = capsys.readouterr().out
    assert "assembled, not measured" in out
    assert "make qa-gate" in out


def test_a_record_carrying_no_digest_cannot_answer_for_itself(tmp_path, capsys):
    """The same verdict from the other direction, and it is permanent rather than
    transitional: a merge can drop the key as easily as it rewrites the map."""
    record = _record(tmp_path, {"corpus": "same"})
    body = json.loads(record.read_text(encoding="utf-8"))
    del body[qa_gate.DIGEST_KEY]
    record.write_text(json.dumps(body), encoding="utf-8")

    assert qa_gate.check({"corpus": "same"}, record) == qa_gate.RECORD_ASSEMBLED
    assert "carries no digest" in capsys.readouterr().out


def test_the_digest_binds_every_field_a_run_wrote(tmp_path):
    """`sources` and `run_id` are what the issue named, but a merge stitching `held`
    from one run onto the map of another fabricates just as much."""
    record = _record(tmp_path, {"corpus": "same"}, run_id="run-1", held=["a-case"])
    for edit in ({"run_id": "run-2"}, {"held": []}, {"date": "2026-02-02"},
                 {"cases": 99}):
        assert qa_gate.check(
            {"corpus": "same"}, _assembled(tmp_path, record, **edit)
        ) == qa_gate.RECORD_ASSEMBLED


def test_re_ordering_the_file_is_not_an_assembly(tmp_path):
    """Content is the claim; layout asserts nothing, so a formatter must not read as
    a merge."""
    record = _record(tmp_path, {"b": "2", "a": "1"})
    body = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(
        json.dumps({k: body[k] for k in sorted(body, reverse=True)}), encoding="utf-8"
    )

    assert qa_gate.check({"b": "2", "a": "1"}, record) == 0


def test_an_assembled_record_is_refused_before_its_legs_are_compared(tmp_path, capsys):
    """A record that answers for no run says nothing about which leg moved, and the
    repair is the same replay either way."""
    record = _assembled(tmp_path, _record(tmp_path, {"corpus": "old"}), cases=99)

    assert qa_gate.check({"corpus": "new"}, record) == qa_gate.RECORD_ASSEMBLED
    assert "reviewer moved" not in capsys.readouterr().out


# --- only a run entitled to stamp the record stamps it (STU-1090) ---

def _drive(monkeypatch, tmp_path, verdict, corpus=None, record=None, unreachable=(),
           empty_store=()):
    """Run the script end to end with the reviewer stubbed — no live call, no mailbox."""
    import importlib

    script = importlib.import_module("scripts.qa_gate")
    monkeypatch.setattr(
        script.qa_replay, "judge",
        lambda *a, **k: qa_replay.Judgement((verdict,), unreachable, empty_store=empty_store),
    )
    monkeypatch.setattr(script, "_RECORD", record or tmp_path / "record.json")
    argv = ["-o", str(tmp_path / "out.json")]
    if corpus is not None:
        argv += ["--corpus", str(corpus)]
    return script.main(argv)


def test_a_gate_that_held_stamps_the_record(monkeypatch, tmp_path):
    record = tmp_path / "record.json"
    assert _drive(monkeypatch, tmp_path, qa_gate.REJECTED, record=record) == 0
    assert json.loads(record.read_text(encoding="utf-8"))["cases"] == len(
        qa_gate.load_corpus(CORPUS, DRAFT_FIELDS)
    )


def test_a_gate_that_stopped_firing_leaves_the_record_alone(monkeypatch, tmp_path):
    """The invariant the whole mechanism rests on: a record stamped by a run that found
    a draft approved would turn the check green on a gate that had just stopped firing."""
    record = tmp_path / "record.json"
    assert _drive(monkeypatch, tmp_path, "approved", record=record) == 1
    assert not record.exists()


def test_an_unreadable_verdict_leaves_the_record_alone(monkeypatch, tmp_path):
    """`ERROR` is a failure, not a skip — so it must not stamp either."""
    record = tmp_path / "record.json"
    assert _drive(monkeypatch, tmp_path, qa_replay.ERROR, record=record) == 1
    assert not record.exists()


def test_a_stale_record_survives_a_run_that_stopped_firing(monkeypatch, tmp_path):
    """Leaving it stale is the mechanism: it is what keeps `make qa-gate-check` red
    until somebody actually replays."""
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"date": "2026-01-01", "sources": {"a": "1"}}), encoding="utf-8")
    before = record.read_text(encoding="utf-8")
    assert _drive(monkeypatch, tmp_path, "approved", record=record) == 1
    assert record.read_text(encoding="utf-8") == before


def test_another_corpus_does_not_stamp_the_record(monkeypatch, tmp_path):
    """`--corpus` names any corpus and the record names one, so a pass elsewhere would
    replace a verified record with a measurement of something else."""
    record = tmp_path / "record.json"
    corpus = _corpus(tmp_path, _entry())
    assert _drive(monkeypatch, tmp_path, qa_gate.REJECTED, corpus=corpus, record=record) == 0
    assert not record.exists()


# --- a reviewer that lost a tool did not answer the question (STU-1139) ---

PRECEDENTS = "mail_precedents-search_mail"
SITE = "org_web-search_site"


def _journal(tmp_path, run_id, *calls):
    """A Studio run journal holding `(tool, error)` pairs, error `None` for an answer."""
    runs = tmp_path / qa_replay.JOURNAL_DIR
    runs.mkdir(parents=True, exist_ok=True)
    path = runs / f"2026-01-01T00h00m-draft-qa-replay-{run_id[:8]}.jsonl"
    lines = [json.dumps({"event": "pipeline_start", "run_id": run_id[:8]})]
    for tool, error in calls:
        event = {"event": "tool_call_complete", "tool": tool, "result": None}
        if error is not None:
            event["error"] = error
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_tool_that_raised_on_every_call_is_named(tmp_path):
    journal = _journal(
        tmp_path, "abcdef12",
        (SITE, None),
        (PRECEDENTS, "ValueError: Unable to get authority configuration"),
        (PRECEDENTS, "ValueError: Unable to get authority configuration"),
    )

    assert qa_replay.tool_calls(journal)[1] == (PRECEDENTS,)


def test_a_tool_that_answered_once_was_reachable(tmp_path):
    """One failed query out of two is a query that failed, not a source the reviewer
    never had — and over-firing here is how a gate erodes into being ignored."""
    journal = _journal(tmp_path, "abcdef12", (PRECEDENTS, "timed out"), (PRECEDENTS, None))

    assert qa_replay.tool_calls(journal)[1] == ()


def test_a_tool_nobody_called_is_not_unreachable(tmp_path):
    """Declining to search is a judgement production's reviewer makes too."""
    journal = _journal(tmp_path, "abcdef12", (SITE, None))

    assert qa_replay.tool_calls(journal)[1] == ()


def test_every_tool_the_run_reached_for_is_reported_beside_the_failures(tmp_path):
    """Silencing a tool the reviewer never called cannot move a verdict, so the
    called half is what makes a divergence attributable (STU-1114)."""
    journal = _journal(tmp_path, "abcdef12", (SITE, None), (PRECEDENTS, "boom"))

    assert qa_replay.tool_calls(journal) == ((SITE, PRECEDENTS), (PRECEDENTS,))


def test_the_journal_is_found_by_the_run_records_id(tmp_path):
    """The run record is the only handle stdout gives: it carries no tool calls at all,
    only a count of the successful ones, so the journal is where this is readable."""
    _journal(tmp_path, "abcdef12", (PRECEDENTS, "boom"))
    stdout = json.dumps({"id": "abcdef12-3456-7890-abcd-ef1234567890", "stages": []})

    assert qa_replay.unreachable_in(stdout, tmp_path) == (PRECEDENTS,)


def test_no_journal_is_not_a_reviewer_that_had_its_tools(tmp_path):
    """STU-912's direction: unrecorded must not read as answered."""
    stdout = json.dumps({"id": "abcdef12-3456-7890-abcd-ef1234567890", "stages": []})

    assert qa_replay.unreachable_in(stdout, tmp_path) == (qa_replay.UNRECORDED,)


def test_no_run_record_leaves_the_error_verdict_to_carry_it(tmp_path):
    """A crash before the engine ran has no id to find a journal by, and the `ERROR`
    verdict already fails the gate — naming it twice says nothing more."""
    assert qa_replay.unreachable_in("not json", tmp_path) == ()


# --- an empty store is not a lost source (STU-1235) ---

CLUB_MEMORY = "org_memory-read_memory"


def _memory_dir(tmp_path, has_facts):
    """`tmp_path/club-memory`, empty or holding one real memory file.

    A minimal valid memory: `club_memory.entries()` only needs frontmatter it can
    parse, never the fields `_line()` would need to render an index row.
    """
    if not has_facts:
        return
    directory = tmp_path / "club-memory"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "a-fact.md").write_text("---\nname: a-fact\n---\nbody\n", encoding="utf-8")


def test_a_raise_on_an_empty_club_memory_store_is_excused_not_unreachable(tmp_path):
    """`org_memory-read_memory` raises for any name while its own index is empty —
    the store saying so, not a source the reviewer lost."""
    _journal(tmp_path, "abcdef12", (CLUB_MEMORY, "FileNotFoundError"))
    stdout = json.dumps({"id": "abcdef12-3456-7890-abcd-ef1234567890", "stages": []})

    assert qa_replay.unreachable_in(stdout, tmp_path) == ()
    assert qa_replay.empty_store_in(stdout, tmp_path) == (CLUB_MEMORY,)


def test_a_raise_on_a_nonempty_club_memory_store_is_still_unreachable(tmp_path):
    """A store that holds a real fact and still raises has lost something — the
    excuse is the empty index, not the tool name."""
    _memory_dir(tmp_path, has_facts=True)
    _journal(tmp_path, "abcdef12", (CLUB_MEMORY, "FileNotFoundError"))
    stdout = json.dumps({"id": "abcdef12-3456-7890-abcd-ef1234567890", "stages": []})

    assert qa_replay.unreachable_in(stdout, tmp_path) == (CLUB_MEMORY,)
    assert qa_replay.empty_store_in(stdout, tmp_path) == ()


def test_a_rejection_from_a_reviewer_that_only_asked_an_empty_store_is_verified():
    """The whole point of STU-1235: a raise the tree itself explains must not read as
    a reviewer missing a source, and must not disqualify the verdict."""
    outcome = _case(qa_gate.REJECTED, empty_store=(CLUB_MEMORY,))

    assert outcome.held
    assert outcome.verified
    assert qa_gate.unverified([outcome]) == []


def test_the_render_names_an_empty_store_separately_from_unreachable():
    rendered = qa_gate.render(
        [_case(qa_gate.REJECTED, empty_store=(CLUB_MEMORY,))], PROVIDER, STUDIO_VERSION
    )

    assert "asked an empty store" in rendered
    assert CLUB_MEMORY in rendered
    assert "is not a pass" not in rendered


def test_a_gate_that_only_asked_an_empty_store_still_stamps_the_record(monkeypatch, tmp_path):
    """A gate that refused to run because a model asked an empty store for a fact
    would be a gate that could never run on a laptop whose club-memory is empty."""
    record = tmp_path / "record.json"
    code = _drive(monkeypatch, tmp_path, qa_gate.REJECTED, record=record,
                  empty_store=(CLUB_MEMORY,))

    assert code == 0
    assert record.exists()


def test_a_rejection_from_a_reviewer_missing_a_tool_is_not_verified():
    outcome = _case(qa_gate.REJECTED, unreachable=(PRECEDENTS,))

    assert outcome.held
    assert not outcome.verified
    assert qa_gate.unverified([outcome]) == [outcome]
    assert qa_gate.failures([outcome]) == []


def test_the_render_names_the_tool_that_never_answered():
    rendered = qa_gate.render([_case(qa_gate.REJECTED, unreachable=(PRECEDENTS,))], PROVIDER, STUDIO_VERSION)

    assert PRECEDENTS in rendered
    assert "is not a pass" in rendered


def test_an_unreachable_tool_fails_and_leaves_the_record_alone(monkeypatch, tmp_path):
    """The whole of STU-1139: 4/4 rejected and exit 0 on a run whose reviewer had two
    of its three tools is the record claiming something it did not measure."""
    record = tmp_path / "record.json"
    code = _drive(monkeypatch, tmp_path, qa_gate.REJECTED, record=record,
                  unreachable=(PRECEDENTS,))

    assert code == qa_gate.TOOL_UNREACHABLE
    assert not record.exists()
    assert PRECEDENTS in json.dumps(
        json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    )


def test_an_unreachable_tool_fails_a_run_on_another_corpus_too(monkeypatch, tmp_path):
    """The tool is a fact about the run, not about which corpus it replayed."""
    corpus = _corpus(tmp_path, _entry())
    code = _drive(monkeypatch, tmp_path, qa_gate.REJECTED, corpus=corpus,
                  record=tmp_path / "record.json", unreachable=(PRECEDENTS,))

    assert code == qa_gate.TOOL_UNREACHABLE


def test_the_unreachable_code_is_not_one_of_the_others():
    """Each condition got its own number so a run folded into another does not go
    unnoticed behind it; the check codes are the same family."""
    assert len({qa_gate.TOOL_UNREACHABLE, qa_gate.RECORD_UNRECORDED,
                qa_gate.RECORD_MOVED, qa_gate.RECORD_ASSEMBLED,
                tree_guard.MIXED_TREES, 1}) == 6


# --- which provider produced the measurement (STU-1175) -------------------------------


def test_the_log_record_names_the_provider_that_judged():
    """A number that cannot say what produced it cannot be checked later. Diagnosing
    STU-1174 needed the provider of one gate run and had to reason from the Makefile."""
    payload = qa_gate.as_dict([_case(qa_gate.REJECTED)], PROVIDER, STUDIO_VERSION)
    assert payload[qa_provider.KEY] == PROVIDER


def test_the_stamped_record_names_the_provider_and_the_digest_covers_it():
    """It joins the body rather than sitting beside it, so a merge cannot stitch one
    run's provider onto another run's sources (STU-1153's finding, this field)."""
    outcomes = [_case(qa_gate.REJECTED)]
    record = qa_gate.as_record(outcomes, {"a": "1"}, "run-1", "2026-01-01", PROVIDER, STUDIO_VERSION)

    assert record[qa_provider.KEY] == PROVIDER
    assert record[qa_gate.DIGEST_KEY] == qa_gate.self_digest(record)
    swapped = record | {qa_provider.KEY: "claude-code"}
    assert qa_gate.self_digest(swapped) != record[qa_gate.DIGEST_KEY]


def test_a_stamped_record_still_passes_the_check(tmp_path, capsys):
    """The push-time question is unmoved by the new field: a record a pass wrote is one
    this tree agrees with, provider and all."""
    sources = {".studio/agents/draft-reviewer.agent.yaml": "abc"}
    path = tmp_path / "stamped.json"
    path.write_text(
        json.dumps(qa_gate.as_record([_case(qa_gate.REJECTED)], sources, "run-1",
                                     "2026-01-01", PROVIDER, STUDIO_VERSION)),
        encoding="utf-8",
    )

    exit_code = qa_gate.check(sources, path)
    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert PROVIDER in out


def test_a_provider_swapped_into_a_record_is_refused_as_assembled(tmp_path, capsys):
    """The whole reason it is inside the digest: a merge that took the provider from one
    branch's run and the sources from another's leaves every leg matching."""
    sources = {".studio/agents/draft-reviewer.agent.yaml": "abc"}
    record = _record(tmp_path, sources, run_id="run-1",
                     **{qa_provider.KEY: PROVIDER})
    _assembled(tmp_path, record, **{qa_provider.KEY: "claude-code"})

    assert qa_gate.check(sources, record) == qa_gate.RECORD_ASSEMBLED


def test_a_record_older_than_the_field_says_so_rather_than_claiming_a_provider(
        tmp_path, capsys):
    """Reported, never compared, and never guessed: a record stamped before STU-1175 has
    no provider, and reading it as one taken on this host is the invention STU-912
    refuses. It is still agreement about the tree, so the check stays 0 — a provider is
    a property of the run and no tree can repair a difference in it."""
    sources = {".studio/agents/draft-reviewer.agent.yaml": "abc"}

    assert qa_gate.check(sources, _record(tmp_path, sources)) == 0
    assert "provider unrecorded" in capsys.readouterr().out


def test_the_table_says_what_it_was_measured_on():
    assert PROVIDER in qa_gate.render([_case(qa_gate.REJECTED)], PROVIDER, STUDIO_VERSION)


def test_an_unresolved_provider_is_named_as_unrecorded_not_left_blank():
    """A field left empty because nobody passed a flag is the same silence as no field,
    so the sentence says which of the two happened."""
    rendered = qa_gate.render([_case(qa_gate.REJECTED)], None, STUDIO_VERSION)
    assert "cannot name" in rendered
    assert qa_gate.as_dict([_case(qa_gate.REJECTED)], None, STUDIO_VERSION)[qa_provider.KEY] is None


def test_a_pass_stamps_the_provider_it_resolved_and_the_check_takes_it(
        monkeypatch, tmp_path, capsys):
    """The whole path, end to end: a run resolves its provider before the first call,
    stamps it inside the digested body, and `--check` reads that record back green.

    Both halves matter — a record the check refuses would make the field a red push, and
    the provider is a property of the run that no tree can repair (STU-1175)."""
    record = tmp_path / "record.json"
    assert _drive(monkeypatch, tmp_path, qa_gate.REJECTED, record=record) == 0

    stamped = json.loads(record.read_text(encoding="utf-8"))
    assert stamped[qa_provider.KEY] == qa_provider.resolve(deployment.ROOT,
                                                           qa_provider.REVIEWER)
    assert stamped[qa_gate.DIGEST_KEY] == qa_gate.self_digest(stamped)

    exit_code = qa_gate.check(qa_gate.fingerprint(deployment.ROOT, CORPUS), record)
    assert exit_code == 0, capsys.readouterr().out


def test_the_log_a_run_writes_names_its_provider(monkeypatch, tmp_path):
    """`logs/qa-gate.json` is where a contaminated run is spotted after the fact, and it
    was the file STU-1174 could not ask."""
    _drive(monkeypatch, tmp_path, qa_gate.REJECTED)
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert written[qa_provider.KEY] == qa_provider.resolve(deployment.ROOT,
                                                           qa_provider.REVIEWER)


# --- which Studio version produced the measurement (STU-1241) -------------------------


def test_the_log_record_names_the_studio_version_that_ran():
    payload = qa_gate.as_dict([_case(qa_gate.REJECTED)], PROVIDER, STUDIO_VERSION)
    assert payload[qa_studio_version.KEY] == STUDIO_VERSION


def test_the_stamped_record_names_the_studio_version_and_the_digest_covers_it():
    """Joins the body rather than sitting beside it, for the reason the provider does
    (STU-1175): a merge cannot then stitch one run's version onto another run's sources."""
    outcomes = [_case(qa_gate.REJECTED)]
    record = qa_gate.as_record(outcomes, {"a": "1"}, "run-1", "2026-01-01", PROVIDER,
                               STUDIO_VERSION)

    assert record[qa_studio_version.KEY] == STUDIO_VERSION
    assert record[qa_gate.DIGEST_KEY] == qa_gate.self_digest(record)
    swapped = record | {qa_studio_version.KEY: "0.17.1"}
    assert qa_gate.self_digest(swapped) != record[qa_gate.DIGEST_KEY]


def test_a_studio_version_swapped_into_a_record_is_refused_as_assembled(tmp_path, capsys):
    """The whole reason it is inside the digest: a merge that took the version from one
    branch's run and the sources from another's leaves every leg matching."""
    sources = {".studio/agents/draft-reviewer.agent.yaml": "abc"}
    record = _record(tmp_path, sources, run_id="run-1",
                     **{qa_studio_version.KEY: STUDIO_VERSION})
    _assembled(tmp_path, record, **{qa_studio_version.KEY: "0.17.1"})

    assert qa_gate.check(sources, record) == qa_gate.RECORD_ASSEMBLED


def test_a_record_older_than_the_field_says_so_rather_than_claiming_a_version(
        tmp_path, capsys):
    """Reported, never compared, and never guessed: a record stamped before STU-1241 has
    no Studio version, and reading it as one measured on this host is the invention
    STU-912 refuses. Still agreement about the tree, so the check stays 0 — a Studio
    version is a property of the run and no tree can repair a difference in it."""
    sources = {".studio/agents/draft-reviewer.agent.yaml": "abc"}

    assert qa_gate.check(sources, _record(tmp_path, sources)) == 0
    assert "studio version unrecorded" in capsys.readouterr().out


def test_the_table_says_which_studio_version_ran():
    assert STUDIO_VERSION in qa_gate.render([_case(qa_gate.REJECTED)], PROVIDER,
                                            STUDIO_VERSION)


def test_an_unresolved_studio_version_is_named_as_unrecorded_not_left_blank():
    rendered = qa_gate.render([_case(qa_gate.REJECTED)], PROVIDER, None)
    assert "cannot name" in rendered
    assert qa_gate.as_dict([_case(qa_gate.REJECTED)], PROVIDER, None)[
        qa_studio_version.KEY] is None


def test_a_pass_stamps_the_studio_version_it_resolved_and_the_check_takes_it(
        monkeypatch, tmp_path, capsys):
    """The same end-to-end path as the provider's: a run resolves the Studio version
    before the first call, stamps it inside the digested body, and `--check` reads that
    record back green."""
    record = tmp_path / "record.json"
    assert _drive(monkeypatch, tmp_path, qa_gate.REJECTED, record=record) == 0

    stamped = json.loads(record.read_text(encoding="utf-8"))
    assert stamped[qa_studio_version.KEY] == qa_studio_version.resolve(deployment.ROOT)
    assert stamped[qa_gate.DIGEST_KEY] == qa_gate.self_digest(stamped)

    exit_code = qa_gate.check(qa_gate.fingerprint(deployment.ROOT, CORPUS), record)
    assert exit_code == 0, capsys.readouterr().out


def test_the_log_a_run_writes_names_its_studio_version(monkeypatch, tmp_path):
    _drive(monkeypatch, tmp_path, qa_gate.REJECTED)
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert written[qa_studio_version.KEY] == qa_studio_version.resolve(deployment.ROOT)
