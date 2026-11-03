"""The frozen coverage reads, and what the gate refuses to drive (STU-1151)."""

import json
from pathlib import Path

import pytest
import yaml

from email_classifier import (coverage_read, qa_coverage, qa_coverage_gate,
                              qa_provider, qa_studio_version, tree_guard)

from tests.test_drafts import REPO_ROOT
from email_classifier import deployment

CORPUS = deployment.ROOT / "eval" / "draft-coverage-cases.json"

GREETING = "Bonjour,\n\n"
CLOSING = "\n\nBonne journée,\n\nEMC"

# A resolved provider, the way every harness records one (STU-1175): what ran, not
# what a flag asked for.
PROVIDER = "anthropic"

# A resolved Studio version, the way every harness now records one too (STU-1241).
STUDIO_VERSION = "0.19.0"


def _entry(**overrides) -> dict:
    return {
        "id": "a-case",
        "gate": "a rule the reader must keep applying",
        "source": "a journal that is going to expire",
        "expected_verdict": "incomplete",
        "body_text": "où est-ce que j'annule?",
        "passes": [
            {"draft_body": f"{GREETING}{'A' * 200}{CLOSING}", "verdict": "rejected",
             "critique": "because", "issues": ["cut it"]},
            {"draft_body": f"{GREETING}{'A' * 100}{CLOSING}", "verdict": "approved",
             "critique": "fine", "issues": []},
        ],
    } | overrides


def _corpus(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(list(entries), ensure_ascii=False), encoding="utf-8")
    return path


def _read(verdict: str, *unaddressed: str) -> qa_coverage.Read:
    return qa_coverage.Read(1, verdict, unaddressed)


def _outcome(expected: str, *verdicts: str) -> qa_coverage_gate.Outcome:
    case = qa_coverage_gate.Case(
        "a-case", "a rule", "a journal", expected,
        qa_coverage.Group(
            "a-case", {"body_text": "où?"},
            (qa_coverage.Pass(f"{GREETING}{'A' * 100}{CLOSING}", "approved", "", ()),),
        ),
    )
    return qa_coverage_gate.Outcome(case, tuple(_read(v) for v in verdicts))


# --- what the corpus loader refuses ----------------------------------------------------


def test_a_well_formed_entry_loads(tmp_path):
    (case,) = qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry()))
    assert case.id == "a-case" and case.expected == "incomplete"
    assert case.group.approved and case.group.converged_by_removal
    assert case.group.body_text == "où est-ce que j'annule?"


def test_the_entry_id_stands_in_for_the_thread_id(tmp_path):
    """Both journals are the same conversation, so the real handle would name two entries
    the same in every table this harness prints — and the reader never reads the field."""
    (case,) = qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry()))
    assert case.group.thread_id == case.id


def test_both_verdicts_a_read_can_return_are_allowed(tmp_path):
    """Unlike `qa_gate`, whose corpus may only hold drafts that must be rejected: a corpus
    of flagging cases alone passes trivially under a reader that flags everything."""
    cases = qa_coverage_gate.load_corpus(_corpus(
        tmp_path,
        _entry(id="flags", expected_verdict="incomplete"),
        _entry(id="clears", expected_verdict="covered"),
    ))
    assert [c.expected for c in cases] == ["incomplete", "covered"]


def test_a_verdict_no_read_can_return_is_refused(tmp_path):
    with pytest.raises(ValueError, match="expected_verdict is 'approved'"):
        qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry(expected_verdict="approved")))


def test_an_entry_naming_no_gate_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no gate named"):
        qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry(gate="  ")))


def test_an_entry_with_no_sender_message_is_refused(tmp_path):
    """A message that asked nothing reads back `covered` — a false clear, and the one
    direction this must not fail in."""
    with pytest.raises(ValueError, match="no sender message"):
        qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry(body_text="")))


def test_an_entry_whose_group_shipped_no_body_is_refused(tmp_path):
    passes = _entry()["passes"]
    passes[-1] = passes[-1] | {"verdict": "rejected"}
    with pytest.raises(ValueError, match="shipped no body"):
        qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry(passes=passes)))


def test_an_entry_with_no_passes_at_all_is_refused_rather_than_crashing(tmp_path):
    with pytest.raises(ValueError, match=r"\(no pass\)"):
        qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry(passes=[])))


def test_duplicate_ids_are_refused(tmp_path):
    with pytest.raises(ValueError, match="duplicate id"):
        qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry(), _entry()))


def test_the_corpus_is_checked_before_the_first_call(tmp_path):
    """Every entry costs a live read per repeat, so an entry the harness cannot drive
    should say so for free rather than after the bill."""
    with pytest.raises(ValueError):
        qa_coverage_gate.load_corpus(_corpus(tmp_path, _entry(gate="")))


# --- what holds, and what does not -----------------------------------------------------


def test_reads_that_all_match_hold():
    assert _outcome("incomplete", "incomplete", "incomplete").held


def test_one_read_that_moved_is_the_finding():
    """Not a vote: a read that came back the other way has moved whichever side the
    majority fell on, and a gate that takes the vote can be half off without saying so."""
    outcome = _outcome("incomplete", "incomplete", "covered")
    assert not outcome.held


def test_a_control_entry_that_started_flagging_is_the_finding():
    """The half a corpus of flagging cases cannot ask about."""
    assert not _outcome("covered", "covered", "incomplete").held


def test_a_read_that_did_not_land_fails_rather_than_being_skipped():
    """A gate that could not be asked did not fire (STU-912)."""
    assert not _outcome("covered", "covered", qa_coverage.ERROR).held
    assert not _outcome("covered", qa_coverage.INCOHERENT).held


def test_no_reads_at_all_do_not_hold():
    assert not _outcome("covered").held


def test_a_failure_names_the_gate_and_where_the_case_came_from():
    rendered = qa_coverage_gate.render([_outcome("covered", "incomplete")], PROVIDER, STUDIO_VERSION)
    assert "READS NO LONGER" in rendered
    assert "a rule" in rendered and "a journal" in rendered


def test_a_held_case_does_not_print_its_gate():
    rendered = qa_coverage_gate.render([_outcome("covered", "covered")], PROVIDER, STUDIO_VERSION)
    assert "0/1" not in rendered and "1/1" in rendered
    assert "a rule" not in rendered


# --- unreadable: nothing landed is not a moved read (STU-1254) -------------------------


def test_a_case_whose_every_read_errored_is_unreadable():
    assert _outcome("covered", qa_coverage.ERROR, qa_coverage.ERROR).unreadable


def test_a_case_with_one_incoherent_read_is_unreadable():
    """Matches `test_a_read_that_did_not_land_fails_rather_than_being_skipped`: a lone
    non-verdict already fails `held`, and it is this and not a moved read either."""
    assert _outcome("covered", qa_coverage.INCOHERENT).unreadable


def test_a_case_with_a_real_verdict_among_its_reads_is_not_unreadable():
    """One read landed on the wrong side — that is a moved read, not a dead one."""
    assert not _outcome("covered", "incomplete", qa_coverage.ERROR).unreadable


def test_a_held_case_is_not_unreadable():
    assert not _outcome("covered", "covered", "covered").unreadable


def test_no_reads_at_all_are_not_unreadable_either():
    """Nothing to call unreadable — `held` already covers a case nothing was asked."""
    assert not _outcome("covered").unreadable


def test_an_unreadable_case_is_not_reported_as_a_moved_read():
    """The whole of STU-1254: `READS NO LONGER` is prose for a frozen read that came
    back the other way, and nothing came back at all here."""
    rendered = qa_coverage_gate.render(
        [_outcome("incomplete", qa_coverage.ERROR, qa_coverage.ERROR)], PROVIDER,
        STUDIO_VERSION
    )
    assert "READS NO LONGER" not in rendered
    assert "UNREADABLE" in rendered


def test_a_run_where_nothing_read_does_not_claim_a_moved_verdict():
    """The measured regression: `ANTHROPIC_API_KEY` unset, every one of four cases comes
    back `error` on every repeat, and the header must not say `0/4 still read the way
    they were frozen` — that sentence asserts the reader answered and disagreed."""
    outcomes = [
        _outcome("incomplete", qa_coverage.ERROR, qa_coverage.ERROR),
        _outcome("covered", qa_coverage.ERROR, qa_coverage.ERROR),
        _outcome("incomplete", qa_coverage.ERROR, qa_coverage.ERROR),
        _outcome("covered", qa_coverage.ERROR, qa_coverage.ERROR),
    ]
    rendered = qa_coverage_gate.render(outcomes, PROVIDER, STUDIO_VERSION)
    assert "still read the way they were frozen" not in rendered
    assert "READS NO LONGER" not in rendered
    assert rendered.count("UNREADABLE") >= 4
    assert "check the provider" in rendered


def test_a_mix_of_unreadable_and_moved_keeps_the_ordinary_header():
    """Only a run where *every* case went unread is the whole-run condition — one case
    dying beside one that genuinely moved is still an ordinary, per-case failure."""
    outcomes = [
        _outcome("incomplete", qa_coverage.ERROR, qa_coverage.ERROR),
        _outcome("covered", "incomplete"),
    ]
    rendered = qa_coverage_gate.render(outcomes, PROVIDER, STUDIO_VERSION)
    assert "still read the way they were frozen" in rendered
    assert "READS NO LONGER" in rendered
    assert "UNREADABLE" in rendered
    assert "A frozen read that moved is the finding" in rendered
    assert "got no read at all" in rendered


def test_unreadable_outcomes_still_count_as_failures():
    """The exit-non-zero half must not regress: a gate nobody could interrogate has not
    fired (STU-912), whatever the wording that reports it."""
    outcomes = [_outcome("incomplete", qa_coverage.ERROR, qa_coverage.ERROR)]
    assert qa_coverage_gate.failures(outcomes) == outcomes
    assert qa_coverage_gate.unreadable(outcomes) == outcomes


def test_as_dict_names_the_unreadable_count():
    payload = qa_coverage_gate.as_dict(
        [_outcome("incomplete", qa_coverage.ERROR, qa_coverage.ERROR)], 2, PROVIDER,
        STUDIO_VERSION
    )
    assert payload["unreadable"] == 1
    assert payload["outcomes"][0]["unreadable"] is True


# --- the committed corpus --------------------------------------------------------------


def test_the_committed_corpus_is_drivable():
    cases = qa_coverage_gate.load_corpus(CORPUS)
    assert len(cases) >= 2


def test_the_committed_corpus_holds_a_case_of_each_verdict():
    """A corpus of only-flagging cases passes trivially under a reader that flags
    everything, which is what this reader becomes if the one line saying a plain
    « l'information n'est pas publiée » addresses a request is edited away."""
    expected = {c.expected for c in qa_coverage_gate.load_corpus(CORPUS)}
    assert expected == {"covered", "incomplete"}


# --- the harness itself ----------------------------------------------------------------


def test_nothing_re_freezes_the_corpus():
    """An entry states what the read must say, so a run that starts saying something else
    is the finding. A target that could re-freeze would be the mechanism for erasing it."""
    script = (REPO_ROOT / "scripts" / "qa_coverage_gate.py").read_text(encoding="utf-8")
    assert "--freeze" not in script
    assert "args.corpus.write" not in script and "CORPUS.write" not in script
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "draft-coverage-cases.json" not in makefile.replace(
        "COVERAGE_CORPUS ?= eval/draft-coverage-cases.json", "")


def test_the_gate_leaves_the_readers_model_pin_alone():
    """The question is whether *production's* reader still says this, not whether some
    model would. `--model` is `qa_coverage`'s, where a future bake-off would flip one."""
    body = (REPO_ROOT / "scripts" / "qa_coverage_gate.py").read_text(
        encoding="utf-8").split('"""', 2)[2]
    assert "--model" not in body
    assert coverage_read.MODEL_ENV not in body


def test_the_gate_exits_non_zero_on_a_read_that_moved():
    assert qa_coverage_gate.failures([_outcome("covered", "incomplete")])
    assert not qa_coverage_gate.failures([_outcome("covered", "covered", "covered")])


# --- the fingerprint that says whether the gate needs replaying (STU-1156) --------------

RECORD = deployment.ROOT / "eval" / "draft-coverage-gate.json"


def _record(tmp_path, sources, **overrides):
    path = tmp_path / "record.json"
    body = {"date": "2026-01-01", "run_id": "run-1", "cases": 2, "read": ["a-case"],
            "sources": sources} | overrides
    path.write_text(
        json.dumps(body | {qa_coverage_gate.DIGEST_KEY:
                           qa_coverage_gate.self_digest(body)}),
        encoding="utf-8",
    )
    return path


def _assembled(tmp_path, record, **edits):
    """A record edited after stamping — what a clean `git merge` of two passes leaves."""
    body = json.loads(record.read_text(encoding="utf-8")) | edits
    path = tmp_path / "assembled.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_an_unchanged_reader_needs_no_replay(tmp_path):
    today = {"a": "1"}
    assert qa_coverage_gate.check(today, _record(tmp_path, today)) == 0


def test_an_edited_reader_asks_for_a_replay_and_names_the_file(tmp_path, capsys):
    record = _record(tmp_path, {".studio/agents/coverage-reader.agent.yaml": "old"})
    today = {".studio/agents/coverage-reader.agent.yaml": "new"}
    assert qa_coverage_gate.check(today, record) == qa_coverage_gate.RECORD_MOVED
    out = capsys.readouterr().out
    assert ".studio/agents/coverage-reader.agent.yaml" in out
    assert "make qa-coverage-gate" in out


def test_a_missing_record_is_not_agreement(tmp_path):
    absent = tmp_path / "absent.json"
    assert qa_coverage_gate.check({"a": "1"}, absent) == qa_coverage_gate.RECORD_UNRECORDED
    assert qa_coverage_gate.check(
        {"a": "1"}, _record(tmp_path, {})
    ) == qa_coverage_gate.RECORD_UNRECORDED


def test_a_source_deleted_out_from_under_the_gate_reads_as_moved(tmp_path):
    """Not a crash: a source the gate can no longer read is the strongest reason to
    replay it, so it has to reach the same answer an edit does."""
    record = _record(tmp_path, {"gone.yaml": "abc"})
    today = qa_coverage_gate.fingerprint(tmp_path, tmp_path / "corpus.json")
    assert today["agent"] == "absent"
    assert qa_coverage_gate.check(today, record) == qa_coverage_gate.RECORD_MOVED


def test_the_fingerprint_covers_the_corpus_itself(tmp_path):
    """An entry added is a replay too — a corpus the record does not cover would let one
    be appended and never read."""
    corpus = _corpus(tmp_path, _entry())
    assert qa_coverage_gate.CORPUS_LEG in qa_coverage_gate.fingerprint(deployment.ROOT, CORPUS)
    before = qa_coverage_gate.fingerprint(tmp_path, corpus)
    corpus.write_text(json.dumps([_entry(), _entry(id="b-case")]), encoding="utf-8")
    assert qa_coverage_gate.fingerprint(tmp_path, corpus) != before


def test_every_source_the_reader_reads_exists():
    """A path that silently digests as `absent` would pin a typo as a stable leg."""
    missing = [
        s for s in qa_coverage_gate.sources(deployment.ROOT).values()
        if not (deployment.ROOT / s).exists()
    ]
    assert not missing, missing


def test_the_readers_agent_declares_no_skills_and_no_tools():
    """Read off the agent rather than restated (STU-1140): the short fingerprint list is
    a consequence of the reader being ungrounded on purpose, so a skill or tool it gains
    fails here instead of one silent gate later."""
    agent = yaml.safe_load(
        (deployment.ROOT / ".studio" / "agents" / "coverage-reader.agent.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert not agent.get("skills")
    assert not agent.get("tools")


def test_the_fingerprint_leaves_the_reviewer_out():
    """`qa_gate.sources()` is the wrong set here: the coverage replay loads neither the
    reviewer nor its skills, so editing them would bill a live read to re-answer a
    question that did not move."""
    paths = qa_coverage_gate.sources(deployment.ROOT).values()
    assert ".studio/agents/draft-reviewer.agent.yaml" not in paths
    assert ".studio/skills/club-voice.skill.md" not in paths
    assert ".studio/pipelines/draft-coverage.pipeline.yaml" in paths
    assert ".studio/contracts/draft-coverage.contract.yaml" in paths


def test_a_record_is_only_written_for_a_gate_that_read_the_way_it_was_frozen():
    """It states that these sources were verified. A run that found a frozen read moved
    verified the opposite, and its stale record is what keeps the check failing."""
    outcomes = [_outcome("covered", "covered")]
    record = qa_coverage_gate.as_record(outcomes, {"a": "1"}, "run-1", "2026-01-01",
                                        PROVIDER, STUDIO_VERSION)
    assert record["read"] == ["a-case"]
    assert record["cases"] == 1


def test_the_committed_record_still_describes_this_tree(capsys):
    """The push-time question itself, on the real record — the whole mechanism."""
    today = qa_coverage_gate.fingerprint(deployment.ROOT, CORPUS)
    assert qa_coverage_gate.check(today, RECORD) == 0, capsys.readouterr().out


def test_the_committed_record_names_nobody():
    """Committable on the corpus's own terms: digests and gate ids, no mail.

    `provider` and `studio_version` are optional here and required of `as_record`
    (STU-1175, STU-1241), for the reason `test_qa_gate` says: this record was stamped
    before each field existed, and only the next pass can add one — by hand it is the
    assembled record the digest refuses.
    """
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    core = {"date", "run_id", "cases", "read", "sources", qa_coverage_gate.DIGEST_KEY}
    assert core <= set(record) <= core | {qa_provider.KEY, qa_studio_version.KEY}
    assert all(len(digest) <= 12 for digest in record["sources"].values())
    assert record[qa_coverage_gate.DIGEST_KEY] == qa_coverage_gate.self_digest(record)


# --- only a run entitled to stamp the record stamps it ---------------------------------


def _drive(monkeypatch, tmp_path, *verdicts, corpus=None, record=None):
    """Run the script end to end with the reader stubbed — no live call."""
    import importlib

    script = importlib.import_module("scripts.qa_coverage_gate")
    reads = iter([_read(v) for v in verdicts] * 100)
    monkeypatch.setattr(script.coverage_read, "read", lambda *a, **k: next(reads))
    monkeypatch.setattr(script, "_RECORD", record or tmp_path / "record.json")
    argv = ["--repeats", "1", "-o", str(tmp_path / "out.json")]
    if corpus is not None:
        argv += ["--corpus", str(corpus)]
    return script.main(argv)


def test_a_gate_that_held_stamps_the_record(monkeypatch, tmp_path):
    record = tmp_path / "record.json"
    expected = [c.expected for c in qa_coverage_gate.load_corpus(CORPUS)]
    assert _drive(monkeypatch, tmp_path, *expected, record=record) == 0
    assert json.loads(record.read_text(encoding="utf-8"))["cases"] == len(expected)


def test_a_gate_that_stopped_reading_that_way_leaves_the_record_alone(monkeypatch, tmp_path):
    """The invariant the whole mechanism rests on: a record stamped by a run that found a
    frozen read moved would turn the check green on a gate that had just stopped firing."""
    record = tmp_path / "record.json"
    flipped = [qa_coverage.COVERED if c.expected == qa_coverage.INCOMPLETE
               else qa_coverage.INCOMPLETE
               for c in qa_coverage_gate.load_corpus(CORPUS)]
    assert _drive(monkeypatch, tmp_path, *flipped, record=record) == 1
    assert not record.exists()


def test_a_dead_provider_exits_its_own_code_and_leaves_the_record_alone(
        monkeypatch, tmp_path, capsys):
    """The measured regression (STU-1254): `ANTHROPIC_API_KEY` unset, every call on
    every case comes back `error`. The gate still refuses to stamp — nobody could
    interrogate it, so it has not fired — but the exit code and the summary must say
    that rather than `0/N still read the way they were frozen`."""
    record = tmp_path / "record.json"
    assert _drive(monkeypatch, tmp_path, qa_coverage.ERROR,
                  record=record) == qa_coverage_gate.NOTHING_READ
    assert not record.exists()
    out = capsys.readouterr().out
    assert "still read the way they were frozen" not in out
    assert "READS NO LONGER" not in out


def test_a_stale_record_survives_a_run_that_stopped_firing(monkeypatch, tmp_path):
    """Leaving it stale is the mechanism: it is what keeps the check red until somebody
    actually replays."""
    record = tmp_path / "record.json"
    record.write_text(json.dumps({"date": "2026-01-01", "sources": {"a": "1"}}),
                      encoding="utf-8")
    before = record.read_text(encoding="utf-8")
    flipped = [qa_coverage.COVERED if c.expected == qa_coverage.INCOMPLETE
               else qa_coverage.INCOMPLETE
               for c in qa_coverage_gate.load_corpus(CORPUS)]
    assert _drive(monkeypatch, tmp_path, *flipped, record=record) == 1
    assert record.read_text(encoding="utf-8") == before


def test_another_corpus_does_not_stamp_the_record(monkeypatch, tmp_path):
    """`--corpus` names any corpus and the record names one, so a pass elsewhere would
    replace a verified record with a measurement of something else."""
    record = tmp_path / "record.json"
    corpus = _corpus(tmp_path, _entry())
    assert _drive(monkeypatch, tmp_path, "incomplete", corpus=corpus, record=record) == 0
    assert not record.exists()


def test_a_check_refuses_when_the_script_and_the_package_are_two_trees(monkeypatch, tmp_path):
    """A bare run from a worktree fingerprints main's package against the worktree's
    record, which describes neither reader (STU-1109)."""
    import importlib

    script = importlib.import_module("scripts.qa_coverage_gate")
    monkeypatch.setattr(script.tree_guard, "package_tree", lambda: tmp_path)
    assert script.main(["--check"]) == tree_guard.MIXED_TREES


# --- a record a git merge assembled answers for no run (STU-1153, one harness over) ---


def test_a_hand_edited_source_entry_is_refused_as_assembled(tmp_path, capsys):
    """The dangerous merge is the clean one: two branches touching disjoint sources
    leave every leg matching the merged tree, so the leg comparison reads green."""
    leg = ".studio/agents/coverage-reader.agent.yaml"
    record = _record(tmp_path, {leg: "old", "corpus": "same"})
    merged = {leg: "theirs", "corpus": "same"}

    assert qa_coverage_gate.check(
        merged, _assembled(tmp_path, record, sources=merged)
    ) == qa_coverage_gate.RECORD_ASSEMBLED
    out = capsys.readouterr().out
    assert "assembled, not measured" in out
    assert "make qa-coverage-gate" in out


def test_a_record_carrying_no_digest_cannot_answer_for_itself(tmp_path, capsys):
    """The same verdict from the other direction, and it is permanent rather than
    transitional: a merge can drop the key as easily as it rewrites the map."""
    record = _record(tmp_path, {"corpus": "same"})
    body = json.loads(record.read_text(encoding="utf-8"))
    del body[qa_coverage_gate.DIGEST_KEY]
    record.write_text(json.dumps(body), encoding="utf-8")

    assert qa_coverage_gate.check(
        {"corpus": "same"}, record
    ) == qa_coverage_gate.RECORD_ASSEMBLED
    assert "carries no digest" in capsys.readouterr().out


def test_the_digest_binds_every_field_a_run_wrote(tmp_path):
    """`sources` is what the check compares, but a merge stitching `read` from one run
    onto the map of another fabricates just as much."""
    record = _record(tmp_path, {"corpus": "same"})
    for edit in ({"run_id": "run-2"}, {"read": []}, {"date": "2026-02-02"},
                 {"cases": 99}):
        assert qa_coverage_gate.check(
            {"corpus": "same"}, _assembled(tmp_path, record, **edit)
        ) == qa_coverage_gate.RECORD_ASSEMBLED


def test_re_ordering_the_file_is_not_an_assembly(tmp_path):
    """Content is the claim; layout asserts nothing, so a formatter must not read as
    a merge."""
    record = _record(tmp_path, {"b": "2", "a": "1"})
    body = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(
        json.dumps({k: body[k] for k in sorted(body, reverse=True)}), encoding="utf-8"
    )

    assert qa_coverage_gate.check({"b": "2", "a": "1"}, record) == 0


def test_an_assembled_record_is_refused_before_its_legs_are_compared(tmp_path, capsys):
    """A record that answers for no run says nothing about which leg moved, and the
    repair is the same replay either way."""
    record = _assembled(tmp_path, _record(tmp_path, {"corpus": "old"}), cases=99)

    assert qa_coverage_gate.check(
        {"corpus": "new"}, record
    ) == qa_coverage_gate.RECORD_ASSEMBLED
    assert "reader moved" not in capsys.readouterr().out


def test_each_check_code_is_its_own():
    """Each condition got its own number so a run folded into another does not go
    unnoticed behind it; the check codes are the same family."""
    assert len({qa_coverage_gate.RECORD_UNRECORDED, qa_coverage_gate.RECORD_MOVED,
                qa_coverage_gate.RECORD_ASSEMBLED, tree_guard.MIXED_TREES, 1}) == 5


def test_the_nothing_read_code_is_not_one_of_the_others():
    """`NOTHING_READ` is the live run's code, the way `qa_gate.TOOL_UNREACHABLE` is —
    its own number so it is not folded into the generic non-zero exit (STU-1254)."""
    assert len({qa_coverage_gate.NOTHING_READ, qa_coverage_gate.RECORD_UNRECORDED,
                qa_coverage_gate.RECORD_MOVED, qa_coverage_gate.RECORD_ASSEMBLED,
                tree_guard.MIXED_TREES, 1}) == 6


# --- which provider produced the measurement (STU-1175) -------------------------------


def test_the_log_record_names_the_provider_that_read():
    payload = qa_coverage_gate.as_dict([_outcome("covered", "covered")], 2, PROVIDER, STUDIO_VERSION)
    assert payload[qa_provider.KEY] == PROVIDER


def test_the_stamped_record_names_the_provider_and_the_digest_covers_it():
    """Inside the body rather than beside it, so a merge cannot stitch one run's provider
    onto another run's sources — STU-1153's finding reaching this field."""
    record = qa_coverage_gate.as_record([_outcome("covered", "covered")], {"a": "1"},
                                        "run-1", "2026-01-01", PROVIDER, STUDIO_VERSION)

    assert record[qa_provider.KEY] == PROVIDER
    assert record[qa_coverage_gate.DIGEST_KEY] == qa_coverage_gate.self_digest(record)
    swapped = record | {qa_provider.KEY: "claude-code"}
    assert qa_coverage_gate.self_digest(swapped) != record[qa_coverage_gate.DIGEST_KEY]


def test_a_stamped_record_still_passes_the_check(tmp_path, capsys):
    sources = {".studio/agents/coverage-reader.agent.yaml": "abc"}
    path = tmp_path / "stamped.json"
    path.write_text(
        json.dumps(qa_coverage_gate.as_record([_outcome("covered", "covered")], sources,
                                              "run-1", "2026-01-01", PROVIDER, STUDIO_VERSION)),
        encoding="utf-8",
    )

    exit_code = qa_coverage_gate.check(sources, path)
    out = capsys.readouterr().out
    assert exit_code == 0, out
    assert PROVIDER in out


def test_a_provider_swapped_into_a_record_is_refused_as_assembled(tmp_path):
    sources = {".studio/agents/coverage-reader.agent.yaml": "abc"}
    record = _record(tmp_path, sources, **{qa_provider.KEY: PROVIDER})
    merged = _assembled(tmp_path, record, **{qa_provider.KEY: "claude-code"})

    assert qa_coverage_gate.check(sources, merged) == qa_coverage_gate.RECORD_ASSEMBLED


def test_a_record_older_than_the_field_says_so_rather_than_claiming_a_provider(
        tmp_path, capsys):
    """Reported, never compared: a provider that differs from this host's is not a tree
    that moved, and there would be nothing in the tree to repair it with."""
    sources = {".studio/agents/coverage-reader.agent.yaml": "abc"}

    assert qa_coverage_gate.check(sources, _record(tmp_path, sources)) == 0
    assert "provider unrecorded" in capsys.readouterr().out


def test_the_table_says_what_it_was_measured_on():
    assert PROVIDER in qa_coverage_gate.render([_outcome("covered", "covered")], PROVIDER, STUDIO_VERSION)


def test_a_pass_stamps_the_provider_it_resolved_and_the_check_takes_it(
        monkeypatch, tmp_path, capsys):
    """The whole path end to end, for the reason `test_qa_gate` runs it: a record the
    check refuses would make the field a red push no tree could repair (STU-1175)."""
    record = tmp_path / "record.json"
    expected = [c.expected for c in qa_coverage_gate.load_corpus(CORPUS)]
    assert _drive(monkeypatch, tmp_path, *expected, record=record) == 0

    stamped = json.loads(record.read_text(encoding="utf-8"))
    assert stamped[qa_provider.KEY] == qa_provider.resolve(
        deployment.ROOT, qa_provider.READER, coverage_read.DEFAULT_PROVIDER)
    assert stamped[qa_coverage_gate.DIGEST_KEY] == qa_coverage_gate.self_digest(stamped)

    today = qa_coverage_gate.fingerprint(deployment.ROOT, CORPUS)
    exit_code = qa_coverage_gate.check(today, record)
    assert exit_code == 0, capsys.readouterr().out


def test_the_log_a_run_writes_names_its_provider(monkeypatch, tmp_path):
    expected = [c.expected for c in qa_coverage_gate.load_corpus(CORPUS)]
    _drive(monkeypatch, tmp_path, *expected)
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert written[qa_provider.KEY] == qa_provider.resolve(
        deployment.ROOT, qa_provider.READER, coverage_read.DEFAULT_PROVIDER)


# --- which Studio version produced the measurement (STU-1241) --------------------------


def test_the_log_record_names_the_studio_version_that_read():
    payload = qa_coverage_gate.as_dict([_outcome("covered", "covered")], 2, PROVIDER,
                                       STUDIO_VERSION)
    assert payload[qa_studio_version.KEY] == STUDIO_VERSION


def test_the_stamped_record_names_the_studio_version_and_the_digest_covers_it():
    """Inside the body rather than beside it, for the reason the provider is (STU-1175):
    a merge cannot then stitch one run's version onto another run's sources."""
    record = qa_coverage_gate.as_record([_outcome("covered", "covered")], {"a": "1"},
                                        "run-1", "2026-01-01", PROVIDER, STUDIO_VERSION)

    assert record[qa_studio_version.KEY] == STUDIO_VERSION
    assert record[qa_coverage_gate.DIGEST_KEY] == qa_coverage_gate.self_digest(record)
    swapped = record | {qa_studio_version.KEY: "0.17.1"}
    assert qa_coverage_gate.self_digest(swapped) != record[qa_coverage_gate.DIGEST_KEY]


def test_a_studio_version_swapped_into_a_record_is_refused_as_assembled(tmp_path):
    sources = {".studio/agents/coverage-reader.agent.yaml": "abc"}
    record = _record(tmp_path, sources, **{qa_studio_version.KEY: STUDIO_VERSION})
    merged = _assembled(tmp_path, record, **{qa_studio_version.KEY: "0.17.1"})

    assert qa_coverage_gate.check(sources, merged) == qa_coverage_gate.RECORD_ASSEMBLED


def test_a_record_older_than_the_field_says_so_rather_than_claiming_a_version(
        tmp_path, capsys):
    """Reported, never compared: a Studio version that differs from this host's is not a
    tree that moved, and there would be nothing in the tree to repair it with."""
    sources = {".studio/agents/coverage-reader.agent.yaml": "abc"}

    assert qa_coverage_gate.check(sources, _record(tmp_path, sources)) == 0
    assert "studio version unrecorded" in capsys.readouterr().out


def test_the_table_says_which_studio_version_ran():
    assert STUDIO_VERSION in qa_coverage_gate.render(
        [_outcome("covered", "covered")], PROVIDER, STUDIO_VERSION)


def test_a_pass_stamps_the_studio_version_it_resolved_and_the_check_takes_it(
        monkeypatch, tmp_path, capsys):
    """The whole path end to end, for the reason `test_qa_gate` runs it."""
    record = tmp_path / "record.json"
    expected = [c.expected for c in qa_coverage_gate.load_corpus(CORPUS)]
    assert _drive(monkeypatch, tmp_path, *expected, record=record) == 0

    stamped = json.loads(record.read_text(encoding="utf-8"))
    assert stamped[qa_studio_version.KEY] == qa_studio_version.resolve(deployment.ROOT)
    assert stamped[qa_coverage_gate.DIGEST_KEY] == qa_coverage_gate.self_digest(stamped)

    today = qa_coverage_gate.fingerprint(deployment.ROOT, CORPUS)
    exit_code = qa_coverage_gate.check(today, record)
    assert exit_code == 0, capsys.readouterr().out


def test_the_log_a_run_writes_names_its_studio_version(monkeypatch, tmp_path):
    expected = [c.expected for c in qa_coverage_gate.load_corpus(CORPUS)]
    _drive(monkeypatch, tmp_path, *expected)
    written = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert written[qa_studio_version.KEY] == qa_studio_version.resolve(deployment.ROOT)
