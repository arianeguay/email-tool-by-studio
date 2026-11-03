"""The drafting bake-off's scoring and its record reader (STU-1073)."""

import json
from pathlib import Path

import pytest
import yaml

from email_classifier import cost, draft_bakeoff, qa_replay
from email_classifier.draft_bakeoff import (
    Arm,
    Authored,
    as_dict,
    compare,
    iterations_to_accept,
    render,
    score,
    spend,
)
from email_classifier.qa_bakeoff import items_from_journal

from tests.test_drafts import STUDIO, load_script, load_yaml

harness = load_script("draft_bakeoff")


def authored(thread_id, *verdicts, attempts=None, model="claude-opus-4-8", tokens=1000):
    return Authored(
        thread_id,
        tuple(verdicts),
        len(verdicts) if attempts is None else attempts,
        {model: {"prompt_tokens": tokens, "total_tokens": tokens}},
    )


def arm(model, *repeats):
    return Arm(model, tuple({run.thread_id: run for run in repeat} for repeat in repeats))


# ── one authoring run ────────────────────────────────────────────────────────

def test_a_draft_the_reviewer_approved_first_time_holds_both_axes():
    run = authored("A", "approved")

    assert run.accepted and run.accepted_first and not run.errored


def test_a_draft_approved_after_a_rejection_is_accepted_but_not_first():
    """The whole economics of the bake-off: this draft reaches the mailbox, and it cost
    two `generate-draft` calls and two `draft-qa` calls to get there."""
    run = authored("A", "rejected", "approved")

    assert run.accepted and not run.accepted_first
    assert run.attempts == 2


def test_a_group_that_exhausted_its_iterations_is_not_accepted():
    assert not authored("A", "rejected", "rejected", "rejected").accepted


def test_a_run_that_never_reached_the_reviewer_errored():
    """A dead run and a bad draft are not the same finding, so it is not a rejection."""
    run = authored("A", attempts=1)

    assert run.errored and not run.accepted


# ── scoring ──────────────────────────────────────────────────────────────────

def test_an_arm_is_scored_only_where_it_agreed_with_itself():
    one = arm("m", [authored("A", "approved"), authored("B", "approved")],
              [authored("A", "approved"), authored("B", "rejected")])

    result = score(one, ["A", "B"], draft_bakeoff.HEADLINE)

    assert (result.stable, result.held) == (1, 1)
    assert result.unstable == ("B",)
    assert result.noise_floor == 0.5


def test_the_denominator_is_every_conversation_both_arms_were_asked_for():
    """`held` over `conversations`, not over what the arm settled: a draft a model
    cannot hold from one repeat to the next is a draft lost like a rejected one."""
    one = arm("m", [authored("A", "approved"), authored("B", "approved")],
              [authored("A", "approved"), authored("B", "rejected")])

    result = score(one, ["A", "B"], draft_bakeoff.HEADLINE)

    assert (result.held, result.conversations) == (1, 2)
    assert result.rate == 1.0


def test_a_single_repeat_reports_no_noise_floor():
    """Everything is trivially stable when it was asked once, and a reported 100%
    would be an artefact of having asked once."""
    result = score(arm("m", [authored("A", "approved")]), ["A"], draft_bakeoff.HEADLINE)

    assert result.noise_floor is None and result.stable == 1


def test_an_arm_that_settled_nothing_reports_no_rate():
    one = arm("m", [authored("A", "approved")], [authored("A", "rejected")])

    assert score(one, ["A"], draft_bakeoff.HEADLINE).rate is None


def test_a_conversation_an_arm_errored_on_is_not_scored():
    one = arm("m", [authored("A", "approved"), authored("B")],
              [authored("A", "approved"), authored("B")])

    result = score(one, ["A", "B"], draft_bakeoff.HEADLINE)

    assert result.errored == ("B",) and result.judged == 1 and result.held == 1


def test_a_conversation_missing_from_a_repeat_is_errored_not_dropped():
    one = arm("m", [authored("A", "approved"), authored("B", "approved")],
              [authored("A", "approved")])

    assert score(one, ["A", "B"], draft_bakeoff.HEADLINE).errored == ("B",)


def test_the_two_axes_separate_quality_from_cost():
    one = arm("m", [authored("A", "rejected", "approved")],
              [authored("A", "rejected", "approved")])

    assert score(one, ["A"], draft_bakeoff.AXES[0]).held == 1
    assert score(one, ["A"], draft_bakeoff.AXES[1]).held == 0


# ── iterations to acceptance (STU-1237) ─────────────────────────────────────

def test_iterations_means_the_attempts_of_a_stably_accepted_conversation():
    one = arm("m", [authored("A", "rejected", "approved")],
              [authored("A", "rejected", "approved")])

    result = iterations_to_accept(one, ["A"])

    assert result.accepted == 1
    assert result.mean == 2.0


def test_iterations_is_none_when_nothing_was_stably_accepted():
    one = arm("m", [authored("A", "rejected")], [authored("A", "rejected")])

    assert iterations_to_accept(one, ["A"]).mean is None


def test_a_conversation_never_accepted_does_not_enter_the_mean():
    """Already penalized by `accepted`; folding it into the mean here would double
    that penalty and blur what `accepted` alone still decides."""
    one = arm("m",
              [authored("A", "rejected", "approved"),
               authored("B", "rejected", "rejected", "rejected")],
              [authored("A", "rejected", "approved"),
               authored("B", "rejected", "rejected", "rejected")])

    result = iterations_to_accept(one, ["A", "B"])

    assert result.accepted == 1
    assert result.mean == 2.0


def test_a_conversation_whose_accept_reject_disagreed_is_unstable_not_averaged():
    one = arm("m", [authored("A", "approved")], [authored("A", "rejected")])

    result = iterations_to_accept(one, ["A"])

    assert result.unstable == ("A",) and result.mean is None


def test_a_conversation_an_arm_errored_on_does_not_enter_iterations():
    one = arm("m", [authored("A", "approved"), authored("B")],
              [authored("A", "approved"), authored("B")])

    result = iterations_to_accept(one, ["A", "B"])

    assert result.errored == ("B",) and result.accepted == 1


def test_a_conversations_own_repeats_average_before_conversations_do():
    """A conversation's repeats count once, not once per repeat, so a conversation
    asked more often does not outweigh one seen only stably."""
    one = arm("m",
              [authored("A", "approved"), authored("B", "rejected", "approved")],
              [authored("A", "approved"),
               authored("B", "rejected", "rejected", "approved")])

    result = iterations_to_accept(one, ["A", "B"])

    # A settles at 1 iteration both repeats; B averages (2 + 3) / 2 = 2.5.
    assert result.mean == pytest.approx((1.0 + 2.5) / 2)


def test_iterations_is_the_tie_break_when_accepted_ties():
    result = compare(
        repeated("base", authored("A", "approved")),
        repeated("cand", authored("A", "rejected", "approved")),
        ["A"],
    )

    assert not result.candidate_is_worse
    assert result.candidate_needs_more_iterations is True
    assert "needs more iterations to acceptance" in render(result)


def test_no_iteration_tie_break_when_an_arm_accepted_nothing():
    result = compare(
        repeated("base", authored("A", "rejected")),
        repeated("cand", authored("A", "approved")),
        ["A"],
    )

    assert result.candidate_needs_more_iterations is None
    assert "no iteration tie-break" in render(result)


def test_the_rendered_report_and_json_carry_the_new_axis():
    result = compare(
        repeated("base", authored("A", "approved")),
        repeated("cand", authored("A", "rejected", "approved")),
        ["A"],
    )

    rendered = render(result)
    payload = json.loads(json.dumps(as_dict(result)))

    assert "iterations to acceptance" in rendered
    assert payload["iterations"][0]["mean"] is not None
    assert payload["candidate_needs_more_iterations"] is True


# ── spend ────────────────────────────────────────────────────────────────────

def test_spend_counts_every_run_including_the_unstable_ones():
    """Money actually spent: a rejection an arm made on one repeat and not the next
    cost a group iteration all the same."""
    one = arm("m", [authored("A", "approved")], [authored("A", "rejected", "approved")])

    result = spend(one)

    assert (result.runs, result.attempts, result.accepted) == (2, 3, 2)
    assert result.tokens["total_tokens"] == 2000


def test_usd_per_accepted_draft_is_the_bill_over_what_was_accepted():
    one = arm("m", [authored("A", "approved", tokens=1_000_000)])

    result = spend(one)

    assert result.usd == pytest.approx(5.0)
    assert result.usd_per_accepted_draft == pytest.approx(5.0)


def test_an_arm_that_accepted_nothing_has_no_cost_per_accepted_draft():
    """A real bill over a denominator the arm never had is a made-up number."""
    assert spend(arm("m", [authored("A", "rejected")])).usd_per_accepted_draft is None


def test_a_model_the_price_table_does_not_carry_raises():
    """A run silently costed at nothing reads as a cheap run."""
    with pytest.raises(cost.UnknownModel):
        authored("A", "approved", model="some-local-model").usd


# ── the verdict ──────────────────────────────────────────────────────────────

def repeated(model, *runs):
    return arm(model, list(runs), list(runs))


def test_a_candidate_that_loses_an_accepted_draft_is_worse():
    result = compare(
        repeated("base", authored("A", "approved"), authored("B", "approved")),
        repeated("cand", authored("A", "approved"), authored("B", "rejected")),
        ["A", "B"],
    )

    assert result.measured and result.candidate_is_worse


def test_a_candidate_that_ties_is_not_worse_however_it_got_there():
    """The trade this licenses: same accepted drafts, one rejection more, less money."""
    result = compare(
        repeated("base", authored("A", "approved")),
        repeated("cand", authored("A", "rejected", "approved")),
        ["A"],
    )

    assert not result.candidate_is_worse
    assert result.of("cand", "accepted_first").held == 0


def test_a_baseline_that_settled_nothing_measures_nothing():
    """Every arm flip-flopping reads through `candidate_is_worse` exactly like a bad
    candidate does, and naming that a verdict on the model is the wrong fact."""
    base = arm("base", [authored("A", "approved")], [authored("A", "rejected")])
    result = compare(base, repeated("cand", authored("A", "approved")), ["A"])

    assert not result.measured
    assert "NO MEASUREMENT" in draft_bakeoff.render(result)


def test_the_rendered_report_carries_every_number_the_issue_asks_for():
    result = compare(
        repeated("base", authored("A", "approved")),
        repeated("cand", authored("A", "rejected", "approved")),
        ["A"],
    )

    rendered = draft_bakeoff.render(result)

    assert "accepted" in rendered and "accepted_first" in rendered
    assert "per accepted draft" in rendered and "group iteration" in rendered
    assert json.loads(json.dumps(draft_bakeoff.as_dict(result)))["measured"] is True


# ── the corpus ───────────────────────────────────────────────────────────────

def journal(*thread_ids):
    return [json.dumps({"event": "stage_complete", "stage": "drafts-pre",
                        "output": {"pending": [{"thread_id": t, "subject": f"re {t}"}
                                               for t in thread_ids]}})]


def test_the_corpus_is_the_recorded_fanout_keyed_by_thread():
    assert sorted(items_from_journal(journal("A", "B"))) == ["A", "B"]


def test_a_journal_with_no_drafting_fanout_yields_nothing():
    assert items_from_journal(["not json", json.dumps({"event": "stage_start"})]) == {}


# ── the record reader ────────────────────────────────────────────────────────

def record(*stages):
    return json.dumps({"stages": list(stages)})


def qa_stage(verdict, **extra):
    return {"stage_name": "draft-qa", "output": {"verdict": verdict}, **extra}


def draft_stage(**extra):
    return {"stage_name": "generate-draft", **extra}


def read(stdout):
    return harness._authored("A", stdout)


def test_the_reader_takes_the_verdicts_in_the_order_the_group_ran_them():
    """A group appends a StageRun per stage per iteration, so the reviewer's rulings in
    order are the run's iterations in order."""
    run = read(record(draft_stage(), qa_stage("rejected"),
                      draft_stage(), qa_stage("approved")))

    assert run.verdicts == ("rejected", "approved")
    assert run.attempts == 2 and run.accepted and not run.accepted_first


def test_the_reader_sums_token_usage_over_every_stage_and_model():
    """Both agents' spend counts: reaching an accepted draft is what is being priced,
    and a rejection pays the reviewer again as well as the drafter."""
    run = read(record(
        draft_stage(token_usage={"by_model": {"drafter-model": {"prompt_tokens": 10}}}),
        qa_stage("approved",
                 token_usage={"by_model": {"judge-model": {"prompt_tokens": 3}}}),
    ))

    assert run.by_model == {"drafter-model": {"prompt_tokens": 10},
                            "judge-model": {"prompt_tokens": 3}}


def test_a_run_that_printed_no_record_errored():
    assert read("studio: command not found").errored


def test_a_stage_with_no_verdict_is_not_counted_as_one():
    assert read(record(draft_stage(), {"stage_name": "draft-qa", "status": "failed"})).errored


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_bakeoff_pipeline_is_email_draft_with_the_mailbox_write_stood_down():
    """Everything that decides a verdict has to be production's, or the number decides
    nothing about production: same agents, same contracts, same group iterations."""
    production = load_yaml("pipelines/email-draft.pipeline.yaml")["stages"][0]
    bakeoff = load_yaml("pipelines/draft-bakeoff.pipeline.yaml")["stages"][0]

    assert bakeoff["max_iterations"] == production["max_iterations"]
    assert [s["name"] for s in bakeoff["stages"]] == [
        "generate-draft", "seal-draft", "draft-qa"]
    for mine, theirs in zip(bakeoff["stages"], production["stages"]):
        if theirs.get("agent"):
            assert (mine["agent"], mine["contract"], mine.get("ralph")) == (
                theirs["agent"], theirs["contract"], theirs.get("ralph"))


def test_the_bakeoff_writes_no_draft_to_the_mailbox():
    """It authors each conversation once per arm per repeat; every one of those would
    otherwise land in the mailbox and in the shared drafts journal."""
    stages = load_yaml("pipelines/draft-bakeoff.pipeline.yaml")["stages"][0]["stages"]
    seal = next(s for s in stages if s["name"] == "seal-draft")

    assert seal["script"] == "scripts/seal_draft.py"
    assert "write_draft" not in yaml.safe_dump(stages)


def test_the_reviewer_reads_the_shape_production_gives_it():
    """`seal-draft` hands on the `draft_id` a mailbox write would have returned, so the
    prompt being scored is the prompt production gives (STU-1014's reason, one stage on)."""
    stages = load_yaml("pipelines/draft-bakeoff.pipeline.yaml")["stages"][0]["stages"]
    seal, qa = stages[-2], stages[-1]

    assert "previous_stage_output" in qa["context"]["include"]
    assert "draft_id" in load_yaml(
        f"contracts/{seal['contract']}.contract.yaml")["schema"]["required_fields"]


def test_the_drafter_pins_the_model_the_bakeoff_settled():
    """Its own pin and not `defaults.model` (STU-1073): that line governs the classifier
    too, and STU-1049 measured that one onto Opus. The env override is what the bake-off
    flips — a model a production run picks up from the environment is a model no report
    can name — so the literal is what production reads."""
    assert load_yaml("agents/drafter.agent.yaml")["model"] == \
        "${DRAFT_MODEL:-claude-sonnet-5}"


def test_the_stage_names_the_reader_keys_on_are_the_ones_the_pipeline_runs():
    """A rename in the pipeline would have the reader find no verdict and report every
    conversation as a dead run."""
    stages = {s["name"] for s in
              load_yaml("pipelines/draft-bakeoff.pipeline.yaml")["stages"][0]["stages"]}

    assert {harness._DRAFT_STAGE, harness._QA_STAGE} <= stages
    assert (STUDIO / "pipelines" / f"{harness._PIPELINE}.pipeline.yaml").is_file()


# ── resume ───────────────────────────────────────────────────────────────────

def trace_row(tmp_path, journal, model, repeat, thread_id, *verdicts):
    run = authored(thread_id, *verdicts)
    harness._append(tmp_path / "t.runs.jsonl", journal, model, repeat, run)
    return run


def test_a_run_already_paid_for_is_read_back_whole(tmp_path):
    """A full pass is hours of live authoring; re-authoring what an interrupted one
    did buy pays twice for one measurement."""
    trace_row(tmp_path, "j.jsonl", "opus", 2, "A", "rejected", "approved")

    done = harness._load_trace(tmp_path / "t.runs.jsonl", "j.jsonl")

    assert list(done) == [("opus", 2, "A")]
    assert done[("opus", 2, "A")].verdicts == ("rejected", "approved")
    assert done[("opus", 2, "A")].by_model


def test_a_row_from_another_corpus_is_not_reused(tmp_path):
    """A drafting item is the whole prompt, so a row from another journal answers
    another question — reusing it publishes a verdict on drafts nobody asked for."""
    trace_row(tmp_path, "other.jsonl", "opus", 1, "A", "approved")

    assert harness._load_trace(tmp_path / "t.runs.jsonl", "j.jsonl") == {}


def test_no_trace_yet_resumes_nothing(tmp_path):
    assert harness._load_trace(tmp_path / "absent.runs.jsonl", "j.jsonl") == {}


def test_both_arms_are_authored_before_the_next_conversation():
    """An arm-major pass that is interrupted has authored one whole arm and none of
    the other, which is no measurement at all however far it got — what happened on
    2026-08-29, 35 runs in, all baseline."""
    source = Path(harness.__file__).read_text(encoding="utf-8")
    loop = source[source.index("for repeat in range(1, args.repeats + 1):"):]

    assert loop.index("for n, thread_id in") < loop.index("for model in models")


# --- what each arm could not reach (STU-1147) ---

PRECEDENTS = "mail_precedents-search_mail"


def unreachable_arm(model, *tools):
    return Arm(model, ({"A": Authored("A", ("approved",), 1, {}, tools)},))


def test_an_arm_pools_the_tools_no_run_of_it_could_confirm():
    arm = Arm("opus", ({
        "A": Authored("A", ("approved",), 1, {}, (PRECEDENTS,)),
        "B": Authored("B", ("approved",), 1, {}, ()),
    },))
    assert arm.unreachable == (PRECEDENTS,)


def test_a_pass_where_every_tool_answered_reports_none():
    result = compare(unreachable_arm("opus"), unreachable_arm("sonnet"), ["A"])
    assert result.unreachable == {"opus": (), "sonnet": ()}
    assert "unreachable" not in render(result)
    assert as_dict(result)["unreachable"] == {"opus": [], "sonnet": []}


def test_the_same_tool_down_for_both_arms_is_named_and_does_not_fail():
    result = compare(unreachable_arm("opus", PRECEDENTS),
                     unreachable_arm("sonnet", PRECEDENTS), ["A"])
    out = render(result)
    assert f"opus: {PRECEDENTS}" in out
    assert "compared like with like" in out
    assert as_dict(result)["unreachable"]["sonnet"] == [PRECEDENTS]
    # Annotated, not failed: the exit code still answers only the model question.
    assert not result.candidate_is_worse and result.measured


def test_a_tool_down_for_one_arm_only_is_two_different_pipelines():
    result = compare(unreachable_arm("opus", PRECEDENTS), unreachable_arm("sonnet"), ["A"])
    assert "two different pipelines" in render(result)


def test_a_trace_row_written_before_the_tools_were_recorded_is_not_silence(tmp_path):
    """Unrecorded is not answered, so a reused row from an older pass says so rather
    than letting the arm read as one that had everything."""
    trace = tmp_path / "t.runs.jsonl"
    trace.write_text(json.dumps({
        "journal": "j.jsonl", "model": "opus", "repeat": 1, "thread_id": "A",
        "verdicts": ["approved"], "attempts": 1, "by_model": {},
    }) + "\n", encoding="utf-8")

    done = harness._load_trace(trace, "j.jsonl")
    assert done[("opus", 1, "A")].unreachable == (qa_replay.UNRECORDED,)


# --- an empty store is not a lost source (STU-1235) ---

CLUB_MEMORY = "org_memory-read_memory"


def empty_store_arm(model, *tools):
    return Arm(model, ({"A": Authored("A", ("approved",), 1, {}, (), tools)},))


def test_an_arm_pools_what_it_asked_an_empty_store():
    arm = Arm("opus", ({
        "A": Authored("A", ("approved",), 1, {}, (), (CLUB_MEMORY,)),
        "B": Authored("B", ("approved",), 1, {}, (), ()),
    },))
    assert arm.empty_store == (CLUB_MEMORY,)
    assert arm.unreachable == ()


def test_only_one_arm_asking_an_empty_store_is_not_two_different_pipelines():
    """The bug this issue fixes: sonnet calling an empty club-memory index while opus
    declines is model behaviour, in neither arm's `unreachable`."""
    result = compare(empty_store_arm("opus"), empty_store_arm("sonnet", CLUB_MEMORY), ["A"])
    out = render(result)

    assert result.unreachable == {"opus": (), "sonnet": ()}
    assert "two different pipelines" not in out
    assert "compared like with like" not in out
    assert not result.candidate_is_worse and result.measured


def test_the_calling_difference_is_named_when_only_one_arm_asks():
    result = compare(empty_store_arm("opus"), empty_store_arm("sonnet", CLUB_MEMORY), ["A"])
    out = render(result)

    assert "asked a store this tree's index says is empty" in out
    assert f"sonnet: {CLUB_MEMORY}" in out
    assert "sonnet called it and opus did not" in out
    assert as_dict(result)["empty_store"] == {"opus": [], "sonnet": [CLUB_MEMORY]}


def test_both_arms_asking_an_empty_store_is_named_without_the_calling_sentence():
    """Symmetric behaviour is data worth keeping, not a finding worth narrating twice."""
    result = compare(empty_store_arm("opus", CLUB_MEMORY),
                     empty_store_arm("sonnet", CLUB_MEMORY), ["A"])
    out = render(result)

    assert f"opus: {CLUB_MEMORY}" in out
    assert f"sonnet: {CLUB_MEMORY}" in out
    assert "called it and" not in out


def test_a_trace_row_written_before_empty_store_was_recorded_says_nothing_about_it(tmp_path):
    """A row this old predates the distinction, not the store being empty — it must not
    read as an excuse it never made."""
    trace = tmp_path / "t.runs.jsonl"
    trace.write_text(json.dumps({
        "journal": "j.jsonl", "model": "opus", "repeat": 1, "thread_id": "A",
        "verdicts": ["approved"], "attempts": 1, "by_model": {}, "unreachable": [],
    }) + "\n", encoding="utf-8")

    done = harness._load_trace(trace, "j.jsonl")
    assert done[("opus", 1, "A")].empty_store == ()


def test_empty_store_round_trips_through_append_and_load_trace(tmp_path):
    run = Authored("A", ("approved",), 1, {}, (), (CLUB_MEMORY,))
    harness._append(tmp_path / "t.runs.jsonl", "j.jsonl", "opus", 1, run)

    done = harness._load_trace(tmp_path / "t.runs.jsonl", "j.jsonl")

    assert done[("opus", 1, "A")].empty_store == (CLUB_MEMORY,)


# --- an arm on its own provider (STU-1769) ---

def test_a_shared_provider_moves_the_whole_run_as_before(tmp_path):
    command, env = harness._invocation(tmp_path / "i.yaml", "claude-sonnet-5", "anthropic", None)

    assert command[-2:] == ["--provider", "anthropic"]
    assert env == {"DRAFT_MODEL": "claude-sonnet-5"}


def test_an_arm_provider_pins_the_drafter_and_keeps_the_reviewer_on_the_shared_one(tmp_path):
    """`--provider` would move the reviewer too, so a local drafter judged by the hosted
    reviewer has to pin each agent instead."""
    command, env = harness._invocation(tmp_path / "i.yaml", "athena-gemma4", "anthropic", "ollama")

    assert "--provider" not in command
    assert env == {"DRAFT_MODEL": "athena-gemma4", "DRAFT_PROVIDER": "ollama",
                   "STUDIO_DEFAULT_PROVIDER": "anthropic"}


def test_a_conversation_gets_the_timeout_the_pass_was_given(monkeypatch, tmp_path):
    """STU-1769: one athena-qwen38 conversation outlasted the 1800 s default."""
    seen = {}

    def fake_run(command, **kwargs):
        seen["timeout"] = kwargs["timeout"]
        raise harness.subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    run = harness._author({"thread_id": "A"}, "athena-qwen38", "claude-code", tmp_path,
                          "run-1", "ollama", timeout=5400)

    assert seen["timeout"] == 5400
    assert run.verdicts == ()
