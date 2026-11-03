"""Recovering each conversation's stage sequence from an interleaved journal (STU-1112)."""

from email_classifier import journal_sequence


def _context(stage: str, child: str) -> dict:
    return {"event": "stage_context", "stage": stage, "run_id": child}


def _complete(stage: str, status: str = "success", **output) -> dict:
    return {"event": "stage_complete", "stage": stage, "status": status,
            "ts": f"T-{stage}", "output": output}


def _item_done(child: str, index: int, map_name: str = "drafts") -> dict:
    return {"event": "map_item_complete", "map": map_name, "index": index,
            "run_id": child}


def _pipeline_done(child: str) -> dict:
    return {"event": "pipeline_complete", "run_id": child}


def _pending(*threads: str) -> dict:
    return _complete("drafts-pre", pending=[{"thread_id": t} for t in threads])


def _pass(child: str, thread: str, verdict: str) -> list[dict]:
    """One uninterleaved group iteration, as the engine writes it."""
    return [
        _context("generate-draft", child), _complete("generate-draft", thread_id=thread),
        _context("write-draft", child), _complete("write-draft", thread_id=thread),
        _context("draft-qa", child), _complete("draft-qa", verdict=verdict),
    ]


def _sequences(events):
    found, unattributed = journal_sequence.conversations(events)
    return ({c.thread_id: [[e.stage for e in p] for p in c.passes] for c in found},
            unattributed)


def test_two_interleaved_conversations_recover_both_sequences():
    # Two items open together throughout, as the pinned `concurrency: 2` leaves them: no
    # stage of either is adjacent to its own next stage, and every completion has both
    # children waiting on that same stage. A child advances by emitting its next context
    # right after its own completion, and that is the whole of what says whose it was.
    events = [
        _pending("TA", "TB"),
        _context("generate-draft", "A"), _context("generate-draft", "B"),
        _complete("generate-draft", thread_id="TA"), _context("write-draft", "A"),
        _complete("generate-draft", thread_id="TB"), _context("write-draft", "B"),
        _complete("write-draft", thread_id="TB"), _context("draft-qa", "B"),
        _complete("write-draft", thread_id="TA"), _context("draft-qa", "A"),
        _complete("draft-qa", verdict="approved"),
        _pipeline_done("B"), _item_done("B", 1),
        _complete("draft-qa", status="rejected", verdict="rejected"),
        *_pass("A", "TA", "approved"),
        _pipeline_done("A"), _item_done("A", 0),
    ]
    found, unattributed = _sequences(events)
    assert not unattributed
    assert found == {
        "TA": [list(journal_sequence.DRAFT_STAGES), list(journal_sequence.DRAFT_STAGES)],
        "TB": [list(journal_sequence.DRAFT_STAGES)],
    }


def test_the_verdict_of_each_interleaved_conversation_stays_with_its_own_draft():
    # The half that cannot be checked against anything in the journal: `draft-qa` names
    # no thread, so a swap here is invisible in the output and shows up as a table that
    # reads exactly right.
    events = [
        _pending("TA", "TB"),
        _context("generate-draft", "A"), _context("generate-draft", "B"),
        _complete("generate-draft", thread_id="TA"), _context("write-draft", "A"),
        _complete("generate-draft", thread_id="TB"), _context("write-draft", "B"),
        _complete("write-draft", thread_id="TB"), _context("draft-qa", "B"),
        _complete("write-draft", thread_id="TA"), _context("draft-qa", "A"),
        _complete("draft-qa", verdict="B-verdict"),
        _pipeline_done("B"), _item_done("B", 1),
        _complete("draft-qa", verdict="A-verdict"),
        _pipeline_done("A"), _item_done("A", 0),
    ]
    found, _ = journal_sequence.conversations(events)
    assert {c.thread_id: c.executions[-1].verdict for c in found} == {
        "TA": "A-verdict", "TB": "B-verdict"}


def test_a_verdict_landing_between_a_completion_and_its_own_next_stage():
    # The case the ordering rule alone gets wrong, taken from run d861d50d: B's
    # `generate-draft` completes, and A's `draft-qa` verdict and A's whole tail land
    # before B's own next `stage_context`. Reading the completion off the next id-bearing
    # event hands B's draft to A.
    events = [
        _pending("TA", "TB"),
        _context("generate-draft", "A"), _complete("generate-draft", thread_id="TA"),
        _context("write-draft", "A"), _complete("write-draft", thread_id="TA"),
        _context("draft-qa", "A"),
        _context("generate-draft", "B"),
        _complete("generate-draft", thread_id="TB"),
        _complete("draft-qa", verdict="approved"),
        _pipeline_done("A"), _item_done("A", 0),
        _context("write-draft", "B"), _complete("write-draft", thread_id="TB"),
        _context("draft-qa", "B"), _complete("draft-qa", verdict="approved"),
        _pipeline_done("B"), _item_done("B", 1),
    ]
    found, unattributed = journal_sequence.conversations(events)
    assert not unattributed
    by_thread = {c.thread_id: c for c in found}
    assert by_thread["TB"].child == "B"
    assert [e.stage for e in by_thread["TA"].executions] == list(journal_sequence.DRAFT_STAGES)
    assert [e.stage for e in by_thread["TB"].executions] == list(journal_sequence.DRAFT_STAGES)


def test_a_verdict_two_open_groups_could_both_own_is_named_not_guessed():
    # Both children are waiting on a `draft-qa` and neither moves on before the other's
    # verdict lands, so nothing in the journal says whose the first one is. Refused rather
    # than assigned: a mis-paired verdict reads exactly as right as a correct one.
    events = [
        _pending("TA", "TB"),
        _context("generate-draft", "A"), _complete("generate-draft", thread_id="TA"),
        _context("generate-draft", "B"), _complete("generate-draft", thread_id="TB"),
        _context("draft-qa", "A"), _context("draft-qa", "B"),
        _complete("draft-qa", verdict="approved"),
        _complete("draft-qa", status="rejected", verdict="rejected"),
    ]
    found, unattributed = journal_sequence.conversations(events)
    assert [e.stage for e in unattributed] == ["draft-qa", "draft-qa"]
    assert all(len(c.executions) == 1 for c in found)
    rendered = journal_sequence.render(found, unattributed)
    assert "UNATTRIBUTED — 2 drafting stage execution(s)" in rendered


def test_a_pass_the_run_died_inside_is_reported_short():
    events = [
        _pending("TA"),
        *_pass("A", "TA", "rejected"),
        _context("generate-draft", "A"), _complete("generate-draft", thread_id="TA"),
        _context("write-draft", "A"),
    ]
    sequences, unattributed = _sequences(events)
    assert not unattributed
    assert sequences == {"TA": [list(journal_sequence.DRAFT_STAGES), ["generate-draft"]]}


def test_the_verdict_is_read_off_the_output_not_the_status():
    # An approval and a read the contract could not parse both complete `success`.
    events = [_pending("TA"), *_pass("A", "TA", "approved")]
    found, _ = journal_sequence.conversations(events)
    assert [e.verdict for e in found[0].executions] == ["success", "success", "approved"]


def test_a_conversation_no_stage_named_falls_back_to_its_fan_out_item():
    events = [
        _pending("TA", "TB"),
        _context("draft-qa", "B"), _complete("draft-qa", verdict="approved"),
        _pipeline_done("B"), _item_done("B", 1),
    ]
    found, _ = journal_sequence.conversations(events)
    assert [(c.thread_id, c.item) for c in found] == [("TB", ("drafts", 1))]


def test_the_classify_fan_out_does_not_lend_its_items_to_the_drafting_one():
    # Keyed on the index alone, `classify` item 0 would name the drafting conversation.
    events = [
        _pending("TA"),
        _context("classify", "C"), _complete("classify", verdict="A"),
        _pipeline_done("C"), _item_done("C", 0, map_name="classify"),
        *_pass("A", "TA", "approved"),
        _pipeline_done("A"), _item_done("A", 0),
    ]
    found, unattributed = journal_sequence.conversations(events)
    assert not unattributed
    assert [(c.thread_id, c.item) for c in found] == [("TA", ("drafts", 0))]
