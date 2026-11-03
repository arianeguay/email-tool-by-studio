"""Tests for email_classifier.cost — pricing a run off the artifacts it owns
(STU-1012), and off its Studio run log for a run written before those (STU-1011)."""

import json

import pytest

from email_classifier.cost import (
    CLASSIFY_LEG,
    DRAFTS_LEG,
    UnknownModel,
    combine,
    drafts_denominator,
    drafts_executions,
    drafts_leg,
    hit_ratios,
    journal_calls,
    leaf_calls,
    leg,
    price,
    report_leg,
    run_cost,
)


def tokens(model="claude-opus-4-8", prompt=0, write=0, read=0, out=0):
    counts = {"prompt_tokens": prompt, "cache_creation_tokens": write,
              "cached_input_tokens": read, "completion_tokens": out,
              "total_tokens": prompt + write + read + out}
    return {**counts, "by_model": {model: dict(counts)}}


def once(*usages):
    """Items whose child reported no per-stage record — each priced as one call."""
    return [{"token_usage": usage} for usage in usages]


def item(*stages):
    """A fan-out item carrying the record its child run reported (STU-1064): one
    entry per child stage, with the RALPH attempts it made and what it spent."""
    return {"stages": [{"stage": name, "status": "success", "attempts": attempts,
                        **({"token_usage": usage} if usage else {})}
                       for name, attempts, usage in stages]}


def stage(name, depth, **kw):
    return {"event": "stage_complete", "stage": name, "depth": depth, "tokens": tokens(**kw)}


def root(total):
    return {"event": "pipeline_complete", "total_tokens": total}


def test_price_splits_the_four_rates():
    # 1M each: input 5.00, cache write 6.25, cache read 0.50, output 25.00
    usd = price({"prompt_tokens": 1_000_000, "cache_creation_tokens": 1_000_000,
                 "cached_input_tokens": 1_000_000, "completion_tokens": 1_000_000},
                "claude-opus-4-8")
    assert usd == pytest.approx(36.75)


def test_unknown_model_raises_rather_than_costing_nothing():
    with pytest.raises(UnknownModel):
        price({"prompt_tokens": 1_000_000}, "claude-something-6")


@pytest.mark.parametrize("model", ["athena-gemma4", "athena-qwen38", "athena-gptoss"])
def test_a_model_on_athena_is_listed_at_zero_not_left_unknown(model):
    """STU-1769: a draft bake-off arm on Athena's Ollama bills no tokens; the smoke that
    first ran one died on UnknownModel after both arms had authored."""
    assert price({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, model) == 0


def test_a_dated_model_id_prices_at_its_familys_rate():
    counts = {"prompt_tokens": 1_000_000, "cache_creation_tokens": 500_000,
              "cached_input_tokens": 250_000, "completion_tokens": 100_000}
    assert (price(counts, "claude-haiku-4-5-20251001")
            == price(counts, "claude-haiku-4-5"))


def test_a_date_suffix_does_not_guess_a_price_for_an_unpriced_family():
    with pytest.raises(UnknownModel):
        price({"prompt_tokens": 1_000_000}, "claude-something-6-20251001")


def test_a_parent_stage_does_not_double_count_its_children():
    # `call:`/`map:` stages re-report their children's tokens; only the leaves count.
    events = [stage("classify-batch", 0, write=100, out=10),
              stage("classify", 2, write=60, out=6),
              stage("classify", 2, write=40, out=4),
              root(110)]
    assert len(leaf_calls(events)) == 2
    result = run_cost(events)
    assert result["llm_calls"] == 2
    assert result["tokens"]["cache_creation_tokens"] == 100
    assert result["rollup_mismatch"] is None


def test_a_leaf_sum_that_misses_the_root_total_says_so():
    events = [stage("classify", 1, write=100, out=10), root(999)]
    assert run_cost(events)["rollup_mismatch"] == "leaf sum 110 != root total 999"


def test_a_run_whose_root_reported_no_total_is_not_cross_checked():
    events = [stage("classify", 1, write=100, out=10), root(0)]
    assert "not cross-checked" in run_cost(events)["rollup_mismatch"]


def test_cache_write_share_names_where_the_money_went():
    # 1M written (6.25) against 1M read (0.50): the write is 92.6% of the bill.
    events = [stage("classify", 0, write=1_000_000, read=1_000_000), root(2_000_000)]
    result = run_cost(events)
    assert result["usd"] == pytest.approx(6.75)
    assert result["cache_write_share_of_usd"] == pytest.approx(0.926, abs=0.001)
    assert result["cache_hit_ratio"] == 0.5


def test_stages_are_priced_apart():
    events = [stage("classify", 2, write=1_000_000),
              stage("generate-draft", 2, out=1_000_000),
              root(2_000_000)]
    by_stage = run_cost(events)["by_stage"]
    assert by_stage["classify"]["usd"] == pytest.approx(6.25)
    assert by_stage["generate-draft"]["usd"] == pytest.approx(25.0)


def test_a_mixed_model_run_prices_each_model_at_its_own_rate():
    events = [stage("classify", 0, model="claude-haiku-4-5", out=1_000_000),
              stage("generate-draft", 0, model="claude-opus-4-8", out=1_000_000),
              root(2_000_000)]
    result = run_cost(events)
    assert result["models"] == ["claude-haiku-4-5", "claude-opus-4-8"]
    assert result["usd"] == pytest.approx(30.0)


def test_a_log_with_no_billed_stage_is_a_zero_call_run_not_a_crash():
    result = run_cost([{"event": "stage_complete", "stage": "fetch", "depth": 0}, root(0)])
    assert result["llm_calls"] == 0
    assert result["usd"] == 0


def drafts_post(**output):
    return {"event": "stage_complete", "stage": "drafts-post", "depth": 1, "output": output}


def test_usd_per_drafted_conversation_prices_only_the_drafting_stages():
    """Classification is not what a draft costs — the metric the 1.00 threshold is set
    against is `generate-draft` + `draft-qa` over the conversations drafted."""
    events = [stage("classify", 2, out=1_000_000),
              stage("generate-draft", 2, out=1_000_000),
              stage("draft-qa", 2, out=1_000_000),
              drafts_post(drafted=4), root(3_000_000)]
    result = run_cost(events)
    assert result["drafted_conversations"] == 4
    assert result["usd"] == pytest.approx(75.0)
    assert result["drafting_usd"] == pytest.approx(50.0)
    assert result["usd_per_drafted_conversation"] == pytest.approx(12.5)


def test_a_run_that_drafted_nothing_reports_no_per_draft_cost_rather_than_zero():
    """A classify-only run has no cost per draft; a zero would divide a real bill by a
    denominator the run never had."""
    result = run_cost([stage("classify", 0, out=1_000_000), root(1_000_000)])
    assert result["drafted_conversations"] is None
    assert result["usd_per_drafted_conversation"] is None


def test_a_drafting_stage_that_died_before_reporting_is_not_a_zero_draft_run():
    events = [stage("generate-draft", 2, out=1_000_000), drafts_post(failed=3),
              root(1_000_000)]
    assert run_cost(events)["usd_per_drafted_conversation"] is None

# --- the legs a run's own artifacts carry (STU-1012) ---
# `tokens()` above is already the shape Studio reports one call's spend in, on a
# stage's `token_usage` and on a map item's alike.


def test_a_leg_sums_the_calls_it_made_and_counts_them():
    spend = leg(once(tokens(write=1_000_000), tokens(write=2_000_000)))
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (2, 0)
    assert spend["by_model"]["claude-opus-4-8"]["cache_creation_tokens"] == 3_000_000


def test_a_call_that_reported_nothing_is_counted_and_not_dropped():
    # A stage 2 that hit a provider limit on twenty of twenty-six conversations
    # (STU-1016) spent something on each; dropping them would price the run as if
    # it had made only the calls that answered.
    spend = leg(once(tokens(write=1_000_000), None))
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (2, 1)


def test_an_item_reports_the_attempts_its_child_made_and_not_one_per_usage():
    # A drafting item is a `generate-draft` and a `draft-qa` per attempt, and the
    # child says how many of each it made — including the RALPH retries no journal
    # row of ours records (STU-1058, STU-1210).
    spend = leg([item(("generate-draft", 3, tokens(out=1_000_000)),
                      ("write-draft", 0, None),
                      ("draft-qa", 1, tokens(out=1_000_000)))])
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (4, 0)
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 2_000_000


def test_a_failed_item_is_priced_from_its_record_and_not_written_off():
    # A group that took three QA rejections and then died reports what each stage
    # burned, so `unpriced_calls` keeps only what genuinely came back empty.
    spend = leg([item(("generate-draft", 3, tokens(out=1_000_000)),
                      ("draft-qa", 3, None))])
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (6, 3)
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 1_000_000


def test_a_stage_that_called_no_model_buys_no_call_but_still_carries_its_spend():
    # Studio >= 0.20.2 reports 0 attempts for a script stage such as `write-draft`
    # (STU-1245), so it buys no call without being named. A nested `call`/`map` stage
    # is the other one: no agent ran in it, and it carries what its own children spent
    # where nothing else does.
    spend = leg([item(("write-draft", 0, None),
                      ("fan-out", 0, tokens(out=1_000_000)))])
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (0, 0)
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 1_000_000


def test_an_item_with_no_record_keeps_the_floor_from_before_the_record_existed():
    # A throw from before the child run started, or a Studio too old to report the
    # record. A zero would let the second read as a leg that cost nothing.
    spend = leg([{"status": "failed", "error": "spawn refused"}])
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (1, 1)
    assert spend["unrecorded_items"] == 1


def test_an_item_priced_in_the_old_unit_is_counted_so_the_leg_can_say_so():
    # A host below Studio 0.18.0 reports no per-stage record, so a *successful*
    # drafting item prices as one call with nothing unpriced — half the calls it
    # made, reading as a measurement. `unrecorded_items` is the only thing that
    # says the count is in the older unit.
    spend = leg([{"token_usage": tokens(out=1_000_000)},
                 item(("generate-draft", 2, tokens(out=1_000_000)))])
    assert (spend["llm_calls"], spend["unpriced_calls"]) == (3, 0)
    assert spend["unrecorded_items"] == 1


def test_a_leg_whose_items_all_reported_a_record_owes_nothing_to_the_old_unit():
    assert leg([item(("classify", 1, tokens(out=1)))])["unrecorded_items"] == 0


def test_an_unpriced_call_makes_the_total_a_floor_and_says_so():
    result = combine({CLASSIFY_LEG: leg(once(tokens(out=1_000_000), None))})
    assert result["usd"] == pytest.approx(25.0)
    assert result["unpriced_calls"] == 1


def test_a_fully_priced_run_says_it_owes_nothing_to_a_floor():
    assert combine({CLASSIFY_LEG: leg(once(tokens(out=1)))})["unpriced_calls"] == 0


def test_a_leg_keeps_its_models_apart_so_each_prices_at_its_own_rate():
    spend = leg(once(tokens(model="claude-haiku-4-5", out=1_000_000),
                 tokens(model="claude-opus-4-8", out=1_000_000)))
    assert combine({CLASSIFY_LEG: spend})["usd"] == pytest.approx(30.0)


def test_combine_prices_the_legs_apart_and_sums_them():
    result = combine({CLASSIFY_LEG: leg(once(tokens(write=1_000_000))),
                      DRAFTS_LEG: leg(once(tokens(out=1_000_000)))})
    assert result["by_stage"][CLASSIFY_LEG]["usd"] == pytest.approx(6.25)
    assert result["by_stage"][DRAFTS_LEG]["usd"] == pytest.approx(25.0)
    assert result["usd"] == pytest.approx(31.25)
    assert result["llm_calls"] == 2


def test_combined_legs_have_no_roll_up_to_cross_check():
    # Each call was counted once where it was made, so unlike the log's leaf rule
    # there is no second total for the sum to disagree with.
    assert combine({CLASSIFY_LEG: leg(once(tokens(write=1)))})["rollup_mismatch"] is None


def test_hit_ratios_answer_per_leg_with_the_calls_behind_them():
    ratios = hit_ratios({CLASSIFY_LEG: leg(once(tokens(write=100, read=300))),
                         DRAFTS_LEG: leg([item(("generate-draft", 1, tokens(write=100)),
                                                ("draft-qa", 1, None))])})
    assert ratios[CLASSIFY_LEG] == (0.75, 1, 100, None)
    assert ratios[DRAFTS_LEG] == (0.0, 2, 100, None)


def test_a_ratio_is_readable_under_a_model_the_price_table_does_not_carry():
    # The audit's cache check wants the ratio alone, and pricing would raise here
    # (STU-1057) — a run that escalated under an unpriced model still has one.
    spend = leg(once(tokens(model="claude-experimental-9", write=100, read=300)))
    assert hit_ratios({CLASSIFY_LEG: spend})[CLASSIFY_LEG] == (0.75, 1, 100, None)
    with pytest.raises(UnknownModel):
        combine({CLASSIFY_LEG: spend})


def test_a_ratio_carries_what_the_leg_wrote_so_zero_hits_can_be_told_apart():
    # Run `d2592ee5`'s classify leg: 7758 prompt tokens over 3 calls, every one of
    # them under the haiku default's cacheable floor, so nothing was ever written
    # and the 0.0 is not the leg paying cache-write for nothing (STU-1080).
    ratios = hit_ratios({CLASSIFY_LEG: leg([item(("classify", 3, tokens(prompt=7758)))])})
    assert ratios[CLASSIFY_LEG] == (0.0, 3, 0, None)


def test_a_leg_that_reported_no_input_has_no_ratio_rather_than_zero():
    assert hit_ratios(
        {CLASSIFY_LEG: leg(once(tokens(out=10)))})[CLASSIFY_LEG] == (None, 1, 0, None)


def test_a_report_written_before_this_change_carries_no_leg():
    assert report_leg({"run_id": "r1"}) is None
    assert report_leg({"run_id": "r1", "cost": {}}) is None


def test_a_run_that_escalated_nothing_carries_no_leg_either():
    assert report_leg({"cost": {CLASSIFY_LEG: leg([])}}) is None


def test_a_leg_nothing_priced_is_no_leg_rather_than_a_free_run():
    # Every call died before reporting. `combine` would answer $0; the fallback
    # can still price it off the Studio log if that run's log survives.
    assert report_leg({"cost": {CLASSIFY_LEG: leg(once(None, None))}}) is None


def _stage_log(tmp_path, *entries):
    path = tmp_path / "stages.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return path


def test_the_drafts_leg_is_read_off_the_line_naming_the_run(tmp_path):
    path = _stage_log(
        tmp_path,
        {"run_id": "other", "status": "completed", "tokens": leg(once(tokens(out=9)))},
        {"run_id": "r1", "status": "completed", "tokens": leg(once(tokens(out=1_000_000)))},
    )
    spend = drafts_leg(path, "r1")
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 1_000_000


def test_a_leg_that_ran_twice_under_one_run_id_is_summed(tmp_path):
    # A resume keeps the run id (STU-917) and adds an execution rather than replacing
    # one, so the run's spend is both (STU-1111). Taking the last published the
    # resume's bill as the run's.
    path = _stage_log(
        tmp_path,
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 14,
         "tokens": leg(once(tokens(out=1)))},
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 3,
         "tokens": leg(once(tokens(out=2)))},
    )
    spend = drafts_leg(path, "r1")
    assert spend["by_model"]["claude-opus-4-8"]["completion_tokens"] == 3
    assert spend["llm_calls"] == 2
    assert drafts_denominator(path, "r1") == 17


def test_a_leg_dates_itself_only_when_every_execution_agrees_on_the_unit(tmp_path):
    # A run resumed under its own id can span the STU-1210 change — one execution
    # counting stage executions, its resume counting attempts. `unrecorded_items`
    # is what dates a leg, so a total that carried it would date the whole run by
    # its newer half; absent on one execution, the folded leg says nothing.
    def executed(spend, drafted):
        return [{"run_id": "r1", "status": "started"},
                {"run_id": "r1", "status": "completed", "drafted": drafted,
                 "tokens": spend}]

    older = leg(once(tokens(out=1)))
    older.pop("unrecorded_items")
    mixed = _stage_log(tmp_path, *executed(older, 2),
                       *executed(leg(once(tokens(out=2))), 1))
    assert "unrecorded_items" not in drafts_leg(mixed, "r1")

    both = _stage_log(tmp_path, *executed(leg(once(tokens(out=1))), 2),
                      *executed(leg([item(("classify", 1, tokens(out=2)))]), 1))
    assert drafts_leg(both, "r1")["unrecorded_items"] == 1


def test_an_execution_that_never_closed_is_counted_not_dropped(tmp_path):
    # `fb7889ca`: killed mid-fan-out, resumed under its own id. The spend of the
    # killed pass is nowhere, so the sum is a floor and the run has no denominator.
    path = _stage_log(
        tmp_path,
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 4,
         "tokens": leg(once(tokens(out=2)))},
    )
    closed, unrecorded = drafts_executions(path, "r1")
    assert (len(closed), unrecorded) == (1, 1)
    assert drafts_leg(path, "r1")["llm_calls"] == 1
    assert drafts_denominator(path, "r1") is None


def test_a_stage_that_died_before_spawning_anything_closes_its_execution(tmp_path):
    # `failed` is written by `drafts-pre`, which dies before an item exists: it spent
    # nothing and drafted nothing, which is recorded, not unknown.
    path = _stage_log(
        tmp_path,
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "failed", "drafted": 0, "error": "boom"},
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 5,
         "tokens": leg(once(tokens(out=2)))},
    )
    assert drafts_executions(path, "r1")[1] == 0
    assert drafts_denominator(path, "r1") == 5


def test_another_runs_executions_do_not_count_against_this_one(tmp_path):
    path = _stage_log(
        tmp_path,
        {"run_id": "other", "status": "started"},
        {"run_id": "r1", "status": "started"},
        {"run_id": "r1", "status": "completed", "drafted": 2,
         "tokens": leg(once(tokens(out=2)))},
    )
    assert drafts_executions(path, "r1")[1] == 0
    assert drafts_denominator(path, "r1") == 2


def test_a_drafts_stage_that_never_reached_post_carries_no_leg(tmp_path):
    # `started` and `failed` carry no counts — only `drafts-post` sees the fan-out's.
    path = _stage_log(tmp_path, {"run_id": "r1", "status": "started", "tokens": None})
    assert drafts_leg(path, "r1") is None


def test_no_drafts_log_at_all_is_no_leg_rather_than_a_crash(tmp_path):
    assert drafts_leg(tmp_path / "absent.jsonl", "r1") is None


def test_the_artifact_path_prices_a_draft_off_the_drafts_leg():
    """The Studio log splits drafting in two, a run's own artifacts report it as one
    `drafts` leg — the same number, so one stage list answers both (STU-1014)."""
    legs = {CLASSIFY_LEG: leg(once(tokens(out=1_000_000))),
            DRAFTS_LEG: leg(once(tokens(out=1_000_000), tokens(out=1_000_000)))}
    result = combine(legs, drafted=4)
    assert result["usd"] == pytest.approx(75.0)
    assert result["drafting_usd"] == pytest.approx(50.0)
    assert result["usd_per_drafted_conversation"] == pytest.approx(12.5)


def test_the_denominator_comes_off_the_same_line_as_the_drafting_spend(tmp_path):
    """Both halves of the number from one line, so they cannot describe two runs."""
    path = _stage_log(
        tmp_path,
        {"run_id": "r1", "status": "completed", "drafted": 3,
         "tokens": leg(once(tokens(out=1_000_000)))},
        {"run_id": "r2", "status": "completed", "drafted": 9,
         "tokens": leg(once(tokens(out=1_000_000)))},
    )
    assert drafts_denominator(path, "r1") == 3
    assert drafts_leg(path, "r1")["llm_calls"] == 1


def test_a_completed_drafts_stage_that_drafted_nothing_has_no_denominator(tmp_path):
    path = _stage_log(tmp_path, {"run_id": "r1", "status": "completed", "drafted": 0,
                                 "tokens": leg(once(tokens(out=1_000_000)))})
    assert drafts_denominator(path, "r1") is None
    assert combine({DRAFTS_LEG: drafts_leg(path, "r1")},
                   drafts_denominator(path, "r1"))["usd_per_drafted_conversation"] is None


# `journal_calls` — the attempts a Studio journal saw, which is what a leg's own
# execution count cannot reach (STU-1058).

def started(name, depth=3):
    return {"event": "stage_start", "stage": name, "depth": depth}


def retried(name, depth=3):
    return {"event": "stage_retry", "stage": name, "depth": depth}


def finished(name, depth=3, status="success"):
    return {"event": "stage_complete", "stage": name, "depth": depth, "status": status}


def test_a_stage_that_never_retried_made_one_call_per_execution():
    events = [started("generate-draft"), finished("generate-draft"),
              started("draft-qa"), finished("draft-qa")]
    assert journal_calls(events) == {
        "generate-draft": {"executions": 1, "retries": 0, "calls": 1},
        "draft-qa": {"executions": 1, "retries": 0, "calls": 1},
    }


def test_every_retry_is_another_call_the_execution_count_cannot_see():
    # Ralph fires onRetry between attempts and never after the last, so three
    # attempts leave two retries — and cost four calls across the two stages.
    events = [started("generate-draft"), retried("generate-draft"),
              retried("generate-draft"), finished("generate-draft"),
              started("draft-qa"), finished("draft-qa")]
    counted = journal_calls(events)
    assert counted["generate-draft"] == {"executions": 1, "retries": 2, "calls": 3}
    assert sum(s["calls"] for s in counted.values()) == 4


def test_an_execution_the_run_never_finished_still_counted_its_calls():
    # A killed stage leaves no `stage_complete`, so its `attempts` is gone with it —
    # counting starts rather than completions is what keeps its calls on the books.
    events = [started("generate-draft"), retried("generate-draft")]
    assert journal_calls(events)["generate-draft"] == {
        "executions": 1, "retries": 1, "calls": 2}


def test_a_skipped_stage_started_without_calling_anything():
    events = [started("generate-draft"), finished("generate-draft", status="skipped")]
    assert journal_calls(events)["generate-draft"]["calls"] == 0


def test_a_parent_re_reporting_its_children_does_not_double_the_calls():
    events = [started("generate-draft", depth=1), finished("generate-draft", depth=1),
              started("generate-draft", depth=3), retried("generate-draft", depth=3),
              finished("generate-draft", depth=3)]
    assert journal_calls(events)["generate-draft"] == {
        "executions": 1, "retries": 1, "calls": 2}


def test_a_fanout_stage_sharing_its_children_s_name_is_not_one_of_them():
    # The classify `map:` stage is named `classify` over a child stage named
    # `classify`, so depth alone drops the parent only while some child ran
    # (STU-1246). `map_start` names it outright, and the children still count.
    events = [started("classify", depth=1),
              {"event": "map_start", "map": "classify", "depth": 1},
              started("classify", depth=2), retried("classify", depth=2),
              finished("classify", depth=2),
              {"event": "map_complete", "map": "classify", "depth": 1},
              finished("classify", depth=1)]
    assert journal_calls(events, ("classify",))["classify"] == {
        "executions": 1, "retries": 1, "calls": 2}


def test_a_fanout_every_item_resumed_made_no_call_at_all():
    # No child ran, so the parent's own rows are alone at the deepest depth. Counting
    # them reports a call nobody made against a leg that rightly reported none. Zeros
    # rather than no row: the stage ran and called nothing, which is not the same as a
    # journal with no such stage.
    events = [started("classify", depth=1),
              {"event": "map_start", "map": "classify", "depth": 1},
              {"event": "map_complete", "map": "classify", "depth": 1},
              finished("classify", depth=1)]
    assert journal_calls(events, ("classify",))["classify"] == {
        "executions": 0, "retries": 0, "calls": 0}


def test_a_journal_too_old_to_name_its_fanout_keeps_the_depth_rule():
    # The reading lost is a gap that is not there. Guessing the parent from the
    # absence of something would lower the ceiling under a real count instead, and
    # report the leg as wrong rather than low.
    events = [started("classify", depth=1), finished("classify", depth=1)]
    assert journal_calls(events, ("classify",))["classify"]["calls"] == 1


def test_a_stage_the_journal_never_saw_is_absent_rather_than_zero():
    events = [started("generate-draft"), finished("generate-draft")]
    assert "draft-qa" not in journal_calls(events)


def test_a_leg_records_the_provider_its_calls_ran_on():
    # Recorded by the run rather than re-derived at audit time (STU-1145): the answer
    # is a CLI override over an agent field over `defaults.provider`, so a tree read
    # weeks later answers for the tree.
    spend = leg(once(tokens(write=100, read=300)), "claude-code")
    assert spend["provider"] == "claude-code"
    assert hit_ratios({CLASSIFY_LEG: spend})[CLASSIFY_LEG] == (0.75, 1, 100, "claude-code")


def test_a_leg_written_before_this_change_reports_no_provider():
    # `None` has to stay distinguishable from a named one: the cache check treats an
    # unrecorded provider as it always did, so unknown is never the way through.
    legacy = hit_ratios({CLASSIFY_LEG: {"llm_calls": 2, "by_model": {}}})
    assert legacy[CLASSIFY_LEG][3] is None
