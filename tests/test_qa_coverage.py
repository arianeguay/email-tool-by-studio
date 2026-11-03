"""Reading a finished group as a sequence, and the coverage verdict on it (STU-1140)."""

import json

import pytest

from email_classifier import coverage_read, qa_coverage, qa_provider, qa_studio_version

from tests.test_drafts import REPO_ROOT, load_script, load_yaml

harness = load_script("qa_coverage")

GREETING = "Bonjour,\n\n"
CLOSING = "\n\nBonne journée,"

# A resolved provider, the way every harness records one (STU-1175): what ran, not
# what a flag asked for.
PROVIDER = "anthropic"

# A resolved Studio version, the way every harness now records one too (STU-1241).
STUDIO_VERSION = "0.19.0"


def _body(answer: str) -> str:
    return f"{GREETING}{answer}{CLOSING}"


def _journal(*events) -> list[str]:
    flat = [e for group in events for e in (group if isinstance(group, list) else [group])]
    return [json.dumps(e) for e in flat]


def _ctx(stage: str, child: str) -> dict:
    """What Studio writes when a stage opens inside a child run of the fan-out.

    Every stage completion in a real journal has one in front of it, and it is the only
    thing naming which conversation the completion belongs to — a fixture without it is
    not a journal Studio writes.
    """
    return {"event": "stage_context", "run_id": child, "stage": stage}


def _drafted(thread: str, answer: str, child: str = "C1") -> list[dict]:
    return [_ctx("generate-draft", child), {
        "event": "stage_complete",
        "stage": "generate-draft",
        "output": {"thread_id": thread, "draft_body": _body(answer)},
    }]


def _judged(verdict: str, *issues: str, child: str = "C1") -> list[dict]:
    return [_ctx("draft-qa", child), {
        "event": "stage_complete",
        "stage": "draft-qa",
        "output": {"verdict": verdict, "critique": "because", "issues": list(issues)},
    }]


def _start(**input_fields) -> dict:
    return {"event": "pipeline_start", "input": input_fields}


def _group(*answers_and_verdicts, thread: str = "T1") -> qa_coverage.Group:
    return qa_coverage.Group(
        thread,
        {"body_text": "où annuler?"},
        tuple(qa_coverage.Pass(_body(a), v, "", ()) for a, v in answers_and_verdicts),
    )


# --- reading the journal -------------------------------------------------------------


def test_a_three_pass_group_reads_back_in_order():
    lines = _journal(
        _start(thread_id="T1", body_text="où annuler?"),
        _drafted("T1", "A" * 200), _judged("rejected", "cut it"),
        _drafted("T1", "A" * 160), _judged("rejected", "cut more"),
        _drafted("T1", "A" * 100), _judged("approved"),
    )
    (group,) = qa_coverage.groups_from_journal(lines)
    assert [p.verdict for p in group.passes] == ["rejected", "rejected", "approved"]
    assert group.approved and group.body_text == "où annuler?"
    assert group.answer_chars == (200, 160, 100)


def test_the_source_message_comes_from_the_fan_out_items_when_there_are_any():
    lines = _journal(
        _start(body_text="the root input, which names no thread"),
        {"event": "stage_complete", "stage": "drafts-pre",
         "output": {"pending": [{"thread_id": "T1", "body_text": "hers"}]}},
        _drafted("T1", "A" * 100), _judged("approved"),
    )
    (group,) = qa_coverage.groups_from_journal(lines)
    assert group.body_text == "hers"


def test_a_group_that_spent_its_iterations_without_an_approval_is_still_a_group():
    lines = _journal(
        _drafted("T1", "A" * 100), _judged("rejected"),
        _drafted("T1", "A" * 90), _judged("rejected"),
        _drafted("T2", "B" * 80, child="C2"), _judged("approved", child="C2"),
    )
    first, second = qa_coverage.groups_from_journal(lines)
    assert first.thread_id == "T1" and not first.approved and len(first.passes) == 2
    assert second.thread_id == "T2" and second.approved


def test_a_draft_the_reviewer_never_judged_is_an_error_pass_not_a_dropped_one():
    lines = _journal(_drafted("T1", "A" * 100), _judged("rejected"), _drafted("T1", "A" * 90))
    (group,) = qa_coverage.groups_from_journal(lines)
    assert [p.verdict for p in group.passes] == ["rejected", qa_coverage.ERROR]
    assert not group.approved


def test_two_conversations_drafting_at_once_read_as_two_groups():
    """The shape of every production drafting journal (STU-1158).
    `draft-items.pipeline.yaml` pins the fan-out at concurrency 2, so the passes of two
    conversations interleave in the file. Refused outright until `journal_sequence`
    recovered whose is whose; each child's own stages are its group now."""
    lines = _journal(
        _drafted("T1", "A" * 100), _drafted("T2", "B" * 90, child="C2"),
        _judged("rejected"), _judged("approved", child="C2"),
        _drafted("T1", "A" * 60), _judged("approved"),
    )
    first, second = qa_coverage.groups_from_journal(lines)
    assert first.thread_id == "T1" and first.answer_chars == (100, 60) and first.approved
    assert second.thread_id == "T2" and second.answer_chars == (90,)


def test_the_group_order_is_the_order_the_run_started_drafting_them():
    lines = _journal(
        _drafted("T1", "A" * 100, child="C9"), _drafted("T2", "B" * 90, child="C2"),
        _judged("approved", child="C2"), _judged("approved", child="C9"),
    )
    assert [g.thread_id for g in qa_coverage.groups_from_journal(lines)] == ["T1", "T2"]


def test_a_drafting_stage_that_failed_wrote_no_pass_to_read():
    """It completes carrying no output at all, so there is no body and nothing for the
    next pass to be shorter than. Measured on this checkout's journals: four
    `mailbox-full` runs are nothing but these, and they hold no group rather than a
    group of empty ones."""
    lines = _journal(
        _ctx("generate-draft", "C1"),
        {"event": "stage_complete", "stage": "generate-draft", "status": "failed"},
        _drafted("T1", "A" * 100), _judged("approved"),
    )
    (group,) = qa_coverage.groups_from_journal(lines)
    assert group.answer_chars == (100,) and group.approved


def test_a_conversation_whose_every_attempt_failed_is_not_a_group():
    lines = _journal(
        {"event": "stage_complete", "stage": "drafts-pre",
         "output": {"pending": [{"thread_id": "T1", "body_text": "hers"}]}},
        _ctx("generate-draft", "C1"),
        {"event": "stage_complete", "stage": "generate-draft", "status": "failed"},
    )
    assert qa_coverage.groups_from_journal(lines) == []


def test_a_verdict_with_no_draft_in_front_of_it_is_malformed():
    with pytest.raises(qa_coverage.MalformedJournal):
        qa_coverage.groups_from_journal(_journal(_judged("approved")))


def test_unparsable_lines_are_skipped_rather_than_raising():
    lines = ["not json"] + _journal(_drafted("T1", "A" * 100), _judged("approved"))
    assert len(qa_coverage.groups_from_journal(lines)) == 1


# --- the sequence read ---------------------------------------------------------------


def test_converged_by_removal_when_the_approved_body_is_the_shortest_and_shrank():
    group = _group(("A" * 200, "rejected"), ("A" * 160, "rejected"), ("A" * 100, "approved"))
    assert group.converged_by_removal


def test_a_group_that_converged_by_adding_is_not_converged_by_removal():
    group = _group(("A" * 110, "rejected"), ("A" * 220, "approved"))
    assert not group.converged_by_removal


def test_a_single_pass_approval_complied_with_nothing():
    assert not _group(("A" * 100, "approved")).converged_by_removal


def test_a_group_that_never_shipped_has_no_removal_to_attribute():
    assert not _group(("A" * 200, "rejected"), ("A" * 100, "rejected")).converged_by_removal


def test_the_last_rejection_is_the_feedback_the_shipped_body_answered():
    group = qa_coverage.Group("T1", {}, (
        qa_coverage.Pass(_body("a"), "rejected", "", ("say only the fact",)),
        qa_coverage.Pass(_body("b"), "approved", "", ()),
    ))
    assert group.last_rejection.issues == ("say only the fact",)


# --- reading one coverage call -------------------------------------------------------


def test_a_covered_reply_reads_back_covered():
    read = qa_coverage.read_of(
        {"verdict": "covered",
         "requests": [{"request": "où annuler?", "addressed": True, "evidence": "ici"}]}, 1)
    assert read.verdict == qa_coverage.COVERED and not read.unaddressed


def test_an_incomplete_reply_names_the_request_it_left():
    read = qa_coverage.read_of(
        {"verdict": "incomplete",
         "requests": [{"request": "où annuler?", "addressed": False, "evidence": ""}]}, 1)
    assert read.flagged and read.unaddressed == ("où annuler?",)


def test_a_message_that_asked_nothing_is_covered():
    assert qa_coverage.read_of({"verdict": "covered", "requests": []}, 1).verdict == "covered"


@pytest.mark.parametrize("output", [
    {"verdict": "covered", "requests": [{"request": "q", "addressed": False}]},
    {"verdict": "incomplete", "requests": [{"request": "q", "addressed": True}]},
])
def test_a_verdict_that_disagrees_with_its_own_list_is_incoherent_not_a_pick(output):
    assert qa_coverage.read_of(output, 1).verdict == qa_coverage.INCOHERENT


@pytest.mark.parametrize("output", [{}, {"verdict": "maybe", "requests": []},
                                    {"verdict": "covered", "requests": "none"}])
def test_an_output_that_carries_no_verdict_is_an_error(output):
    assert qa_coverage.read_of(output, 1).verdict == qa_coverage.ERROR


def test_a_call_that_died_and_a_reader_that_contradicted_itself_are_told_apart():
    """Different repairs — a provider to fix versus a prompt to fix — so a shared name
    would hide one behind the other (STU-912). Both stay out of `flagged`: a read that
    did not land is not a read that cleared the body."""
    dead = qa_coverage.read_of({}, 1)
    confused = qa_coverage.read_of(
        {"verdict": "covered", "requests": [{"request": "q", "addressed": False}]}, 2)
    assert dead.verdict != confused.verdict
    assert dead.unreadable and confused.unreadable
    assert not dead.flagged and not confused.flagged
    assert "error" in _result(_group(("A" * 9, "approved")), ("error", ())).finding
    assert "incoherent" in _result(_group(("A" * 9, "approved")), ("incoherent", ())).finding


def test_requests_serialised_as_a_json_string_is_still_a_list_of_requests():
    """Measured on the STU-1140 corpus, 2026-08-30: the same prompt returns a real array
    on one call and the array serialised into a string on the next, roughly half and
    half. Read as `list`-only, a coherent verdict was discarded as unreadable and the
    sweep quietly measured half of what it ran."""
    encoded = json.dumps([{"request": "où annuler?", "addressed": False, "evidence": ""}])
    read = qa_coverage.read_of({"verdict": "incomplete", "requests": encoded}, 1)
    assert read.flagged and read.unaddressed == ("où annuler?",)


def test_a_string_that_is_not_a_json_array_is_still_not_a_list():
    assert qa_coverage.read_of({"verdict": "covered", "requests": "[unclosed"}, 1).verdict \
        == qa_coverage.ERROR
    assert qa_coverage.read_of({"verdict": "covered", "requests": '"a string"'}, 1).verdict \
        == qa_coverage.ERROR


# --- the finding ---------------------------------------------------------------------


def _result(group, *reads) -> qa_coverage.GroupResult:
    return qa_coverage.GroupResult(group, tuple(
        qa_coverage.Read(n, v, tuple(u)) for n, (v, u) in enumerate(reads, 1)))


def test_a_body_every_read_answered_is_not_flagged():
    result = _result(_group(("A" * 100, "approved")), ("covered", ()), ("covered", ()))
    assert not result.flagged and "answered in 2/2" in result.finding


def test_a_single_flag_in_two_reads_is_a_single_read_not_a_finding():
    result = _result(_group(("A" * 100, "approved")), ("incomplete", ("où?",)), ("covered", ()))
    assert not result.flagged and "not yet a finding" in result.finding


def test_both_reads_agreeing_is_the_finding():
    result = _result(_group(("A" * 100, "approved")),
                     ("incomplete", ("où?",)), ("incomplete", ("où annuler?",)))
    assert result.flagged and "2/2" in result.finding


def test_the_finding_names_the_loop_only_when_the_group_converged_by_removal():
    shrank = _group(("A" * 200, "rejected"), ("A" * 100, "approved"))
    grew = _group(("A" * 100, "rejected"), ("A" * 200, "approved"))
    flags = (("incomplete", ("où?",)), ("incomplete", ("où?",)))
    assert "converged by removal" in _result(shrank, *flags).finding
    assert "converged by removal" not in _result(grew, *flags).finding


def test_the_finding_quotes_one_read_rather_than_every_phrasing_of_one_request():
    result = _result(_group(("A" * 100, "approved")),
                     ("incomplete", ("où annuler?",)), ("incomplete", ("où l'annuler?",)))
    assert result.unaddressed == ("où annuler?",)
    assert "où l'annuler?" not in result.finding


# --- the table -----------------------------------------------------------------------


def test_a_flagged_row_prints_the_instruction_the_reviewer_gave_before_approving():
    group = qa_coverage.Group("T1", {}, (
        qa_coverage.Pass(_body("A" * 200), "rejected", "", ("the fact is all there is",)),
        qa_coverage.Pass(_body("A" * 100), "approved", "", ()),
    ))
    table = qa_coverage.render(
        [_result(group, ("incomplete", ("où?",)), ("incomplete", ("où?",)))], PROVIDER, STUDIO_VERSION)
    assert "the fact is all there is" in table
    assert "1/1" in table


def test_an_unflagged_row_does_not_print_a_critique_nobody_needs():
    group = qa_coverage.Group("T1", {}, (
        qa_coverage.Pass(_body("A" * 200), "rejected", "", ("cut the invented sentence",)),
        qa_coverage.Pass(_body("A" * 100), "approved", "", ()),
    ))
    table = qa_coverage.render([_result(group, ("covered", ()), ("covered", ()))], PROVIDER, STUDIO_VERSION)
    assert "cut the invented sentence" not in table


def test_the_record_keeps_every_read_even_where_the_finding_quotes_one():
    group = _group(("A" * 200, "rejected"), ("A" * 100, "approved"))
    record = qa_coverage.as_dict(
        [_result(group, ("incomplete", ("où?",)), ("incomplete", ("où l'annuler?",)))],
        2, PROVIDER, STUDIO_VERSION)
    assert record["flagged"] == 1
    assert record["results"][0]["answer_chars"] == [200, 100]
    assert record["results"][0]["converged_by_removal"] is True
    assert [r["unaddressed"] for r in record["results"][0]["reads"]] == [["où?"], ["où l'annuler?"]]


# --- wiring --------------------------------------------------------------------------


def test_the_stage_the_reader_keys_on_is_the_one_the_pipeline_runs():
    """A rename in the pipeline would have the script find no output and record `error`
    on every conversation — a sweep that measured nothing, reported as one that did."""
    pipeline = load_yaml("pipelines/draft-coverage.pipeline.yaml")
    assert [s["name"] for s in pipeline["stages"]] == [coverage_read.STAGE]
    assert pipeline["name"] == coverage_read.PIPELINE


def test_the_reader_is_not_the_reviewer_that_approved_the_body():
    """The whole point of the measurement (STU-1140): the reviewer approved this body,
    and on the run that motivated this it approved its own prior instruction coming
    back. Asking it again asks the party under suspicion."""
    stage = load_yaml("pipelines/draft-coverage.pipeline.yaml")["stages"][0]
    reviewer = load_yaml("pipelines/draft-qa-replay.pipeline.yaml")["stages"][-1]["agent"]
    assert stage["agent"] == "coverage-reader" != reviewer


def test_the_reader_carries_no_feedback_and_no_history():
    """It reads the message and the shipped body. `group_feedback` would hand it the
    critique that is the suspect, and `previous_stage_output` presumes a stage before
    it — there is none, and the body arrives in the input."""
    stage = load_yaml("pipelines/draft-coverage.pipeline.yaml")["stages"][0]
    assert stage["context"]["include"] == ["input"]


def test_the_reader_reads_nothing_of_the_club():
    """No skills and no tools: the club skill would let it reason about what the club
    could have said and the site tool would let it go looking, and both are grounding —
    `draft-reviewer`'s job, not this one's."""
    agent = load_yaml("agents/coverage-reader.agent.yaml")
    assert not agent.get("skills") and not agent.get("tools")


def test_the_reader_is_pinned_rather_than_inheriting_the_classifier_s_model():
    agent = load_yaml("agents/coverage-reader.agent.yaml")
    assert agent["model"].startswith("${" + coverage_read.MODEL_ENV)


def test_the_contract_asks_for_both_halves_the_reader_checks_against_each_other():
    contract = load_yaml("contracts/draft-coverage.contract.yaml")
    assert set(contract["schema"]["required_fields"]) == {"requests", "verdict"}
    # No rejection detection: `incomplete` is a finding, not a stage the engine ends.
    assert "post_validation" not in contract


def test_the_coverage_call_is_handed_the_message_and_the_body_and_nothing_else():
    """The fan-out item carries thread history, the memory index and earlier club mail;
    every one of them is material the reader could mistake for an answer this reply gave."""
    source = (REPO_ROOT / "email_classifier" / "coverage_read.py").read_text(
        encoding="utf-8")
    body = source.split("def read(")[1]
    assert '"body_text": group.body_text' in body
    assert '"draft_body": group.final_body' in body
    assert "group.item" not in body


def test_the_reader_runs_on_the_api_rather_than_the_cli_by_default():
    """Measured, not assumed (2026-08-30, Studio 0.17.0): under the config default
    `claude-code` every read is a fresh CLI process, and back-to-back reads returned the
    session limit as the agent's prose — 3 of 4 reads unreadable, 4 of 4 on the API.
    The same flag `draft_bakeoff` carries, for the reasons it carries it."""
    assert coverage_read.DEFAULT_PROVIDER == "anthropic"
    assert 'default=coverage_read.DEFAULT_PROVIDER' in (
        (REPO_ROOT / "scripts" / "qa_coverage.py").read_text(encoding="utf-8"))


# --- what the reader refuses -----------------------------------------------------------


def test_a_replay_or_erosion_journal_is_refused_by_name_not_read_as_empty():
    """Its drafting stage echoes a recorded draft, so the body at the end is one no group
    wrote toward. Falling through, it reads as a run that simply drafted nothing — a
    different thing, and the one a caller would act on."""
    lines = _journal(
        {"event": "stage_complete", "stage": "replay-draft",
         "output": {"thread_id": "T1", "draft_body": _body("frozen")}},
        _judged("rejected"),
    )
    with pytest.raises(qa_coverage.EchoJournal):
        qa_coverage.groups_from_journal(lines)


def test_a_drafted_thread_with_no_fan_out_item_is_refused_not_given_the_root_input():
    """Borrowing the run's input hands the reader an empty `body_text`, and a message
    that asked nothing reads back `covered` — a false clear."""
    lines = _journal(
        {"event": "stage_complete", "stage": "drafts-pre",
         "output": {"pending": [{"thread_id": "OTHER", "body_text": "hers"}]}},
        _drafted("T1", "A" * 100), _judged("approved"),
    )
    with pytest.raises(qa_coverage.OrphanGroup):
        qa_coverage.groups_from_journal(lines)


def test_an_execution_no_conversation_claims_refuses_the_whole_journal():
    """Two children hold `draft-qa` open and neither is forced by what it does next, so
    the completion ties to no conversation. Some group is short a pass and the journal
    does not say whose — and a group short of a pass can report the wrong shipped body,
    which is a false clear. So none are read (STU-912)."""
    lines = _journal(
        _ctx("draft-qa", "C1"), _ctx("draft-qa", "C2"),
        {"event": "stage_complete", "stage": "draft-qa", "output": {"verdict": "approved"}},
    )
    with pytest.raises(qa_coverage.Unattributable) as caught:
        qa_coverage.groups_from_journal(lines)
    assert "1 drafting stage execution" in str(caught.value)
    assert "draft-qa" in str(caught.value)


# --- a dead read must not bury the reads that landed -----------------------------------


def test_a_dead_read_beside_agreeing_flags_does_not_hide_them():
    """The bug this tool exists to catch, one level up: reads (error, incomplete,
    incomplete, incomplete) reported UNREADABLE and flagged=False."""
    result = _result(_group(("A" * 200, "rejected"), ("A" * 100, "approved")),
                     ("error", ()), ("incomplete", ("où?",)),
                     ("incomplete", ("où?",)), ("incomplete", ("où?",)))
    assert result.flagged and "3/3" in result.finding


def test_what_the_dead_read_cost_is_named_on_the_line_rather_than_dropped():
    result = _result(_group(("A" * 100, "approved")), ("error", ()), ("covered", ()))
    assert not result.flagged
    assert "answered in 1/1" in result.finding and "1 not a verdict" in result.finding


def test_only_a_group_where_nothing_landed_is_unreadable():
    result = _result(_group(("A" * 100, "approved")), ("error", ()), ("incoherent", ()))
    assert result.finding.startswith("UNREADABLE") and not result.flagged


def test_a_lone_flag_beside_a_dead_read_is_not_the_finding_two_flags_are():
    """STU-1188: what decided this was whether the other call happened to die.

    `(error, incomplete)` published the same `UNANSWERED` line two agreeing flags earn,
    while the same lone `incomplete` beside a `covered` was "not yet a finding" — same
    evidence, opposite verdicts, decided by the weather.
    """
    asked = ("où annuler?",)
    group = _group(("A" * 100, "approved"))
    lone = _result(group, ("error", ()), ("incomplete", asked))
    agreeing = _result(group, ("incomplete", asked), ("incomplete", asked))

    assert not lone.flagged and lone.finding != agreeing.finding
    assert "not yet a finding" in lone.finding
    # The loss is still named, and so is what the read that did land called unaddressed:
    # the verdict stops claiming corroboration, it does not hide the flag.
    assert "1 not a verdict" in lone.finding and "où annuler?" in lone.finding
    assert agreeing.flagged and "2/2" in agreeing.finding


def test_a_lone_landed_flag_does_not_read_as_a_flag_the_other_reads_disagreed_with():
    """Both are "not yet a finding", and they are not the same thing: one read
    disagreed, the other was never taken."""
    asked = ("où annuler?",)
    group = _group(("A" * 100, "approved"))
    only_read = _result(group, ("error", ()), ("incomplete", asked))
    outvoted = _result(group, ("incomplete", asked), ("covered", ()))
    assert only_read.finding != outvoted.finding
    assert "one flag in 2 reads" in outvoted.finding
    assert "the only read that landed" in only_read.finding


def test_a_single_read_asked_for_and_flagged_is_still_a_single_read():
    """`--repeats 1` is the named cost: it cannot reach a finding at all."""
    result = _result(_group(("A" * 100, "approved")), ("incomplete", ("où?",)))
    assert not result.flagged and "not yet a finding" in result.finding


# --- which provider produced the measurement (STU-1175) -------------------------------


def test_the_json_names_the_provider_the_reader_ran_on():
    """Resolved, not requested: `--provider ""` asks for the config default, and the
    record used to keep the flag — which says nothing about what answered."""
    result = _result(_group(("A" * 100, "approved")), ("covered", ()), ("covered", ()))
    assert qa_coverage.as_dict([result], 2, PROVIDER, STUDIO_VERSION)[qa_provider.KEY] == PROVIDER


def test_the_table_says_what_it_was_measured_on():
    result = _result(_group(("A" * 100, "approved")), ("covered", ()), ("covered", ()))
    assert PROVIDER in qa_coverage.render([result], PROVIDER, STUDIO_VERSION)


def test_an_unresolved_provider_is_named_rather_than_left_blank():
    result = _result(_group(("A" * 100, "approved")), ("covered", ()), ("covered", ()))
    assert "cannot name" in qa_coverage.render([result], None, STUDIO_VERSION)
    assert qa_coverage.as_dict([result], 2, None, STUDIO_VERSION)[qa_provider.KEY] is None


# --- which Studio version produced the measurement (STU-1241) -------------------------


def test_the_json_names_the_studio_version_that_ran():
    result = _result(_group(("A" * 100, "approved")), ("covered", ()), ("covered", ()))
    assert qa_coverage.as_dict([result], 2, PROVIDER, STUDIO_VERSION)[
        qa_studio_version.KEY] == STUDIO_VERSION


def test_the_table_says_which_studio_version_ran():
    result = _result(_group(("A" * 100, "approved")), ("covered", ()), ("covered", ()))
    assert STUDIO_VERSION in qa_coverage.render([result], PROVIDER, STUDIO_VERSION)


def test_an_unresolved_studio_version_is_named_rather_than_left_blank():
    result = _result(_group(("A" * 100, "approved")), ("covered", ()), ("covered", ()))
    assert "cannot name" in qa_coverage.render([result], PROVIDER, None)
    assert qa_coverage.as_dict([result], 2, PROVIDER, None)[qa_studio_version.KEY] is None
