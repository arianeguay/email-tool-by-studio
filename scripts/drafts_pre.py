#!/usr/bin/env python3
"""Pre-stage of `drafts` (STU-850): pick what needs a reply, build the fan-out items.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

The classification report decides what gets drafted — `suggested_action:
needs_reply`, nothing else — so a draft can never reach a sender the classifier
did not put in front of a human.

What drops out here is decided by the conversation's state, not by the mere
presence of a draft (STU-945): an unsent draft a human may have started editing
still blocks, a thread the club already answered is not answered twice, and a
draft a human has since replied past is reported and left where it is. A draft
this pipeline wrote and nobody has touched is not a human's, so a prompt or
skill corrected since it was written re-drafts it in place (STU-886) — that is
what the fingerprint on the item is for, and a blocked mail never reaches the
fan-out to notice it changed. What does *not* drop out but used to
carry a draft is named too (STU-998) — a draft deleted from outside leaves no
trace, so without that line the next draft on that conversation is
indistinguishable from a guard that failed.

The mail's full body is read here too (STU-852), for the pending subset only:
the fetch that produced the corpus asks Graph for a preview, which is enough to
classify and not enough to answer. The conversation's earlier turns are read the
same way (STU-941), so a follow-up is answered as one.

Anonymization happens HERE, not in the leaf, for the same reason as
`classify-pre`: one child run per email would re-derive its own tokens and lose
cross-email consistency. The keymap is the batch's, seeded from disk, so a
person keeps the token the classification stage gave them.

The stage opens its own record in `logs/drafts/stages.jsonl` before doing any of
that (STU-917). `drafts` is the one `mailbox-full` stage tolerated to fail, and
until this line existed a stage that died and a mailbox with nothing to answer
left the same artifacts — none — so a run reported clean with zero drafts and
nothing said which it was.

Input:  { "corpus": "<dir|json>", "report": "<report path>", ...path overrides }
Output: { "run_id", "needs_draft", "needs_reply", "selection", "pending",
          "provider", "paths" }
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from email_classifier import club_memory
from email_classifier.anonymize import (
    Tokenizer,
    load_keymap,
    record_minted,
    run_keymap_path,
    record_sent,
    sent_path,
    save_keymap,
)
from email_classifier.apply.draft_journal import FAILED, STARTED, record, record_stage
from email_classifier.apply.journal import read_entries
from email_classifier.core.contract import load_taxonomy
from email_classifier.eval.harness import load_corpus
from email_classifier.orchestrate import drafts
from email_classifier.orchestrate.pipeline import dedupe_by_message
from email_classifier.orchestrate.provenance import resolved_provider
from email_classifier.providers import get_body_fetcher, get_drafter, get_thread_reader
from email_classifier.stage1.policy import load_policy
from email_classifier import deployment

REPO_ROOT = deployment.ROOT
STUDIO_DIR = REPO_ROOT / ".studio"

def _paths(ctx: dict) -> dict[str, str]:
    return {key: str(REPO_ROOT / ctx.get(key, default))
            for key, default in drafts.DEFAULT_PATHS.items()}


def _verdict(email, drafter, threads, entries, prompt_fp) -> tuple[str | None, str]:
    """The conversation's draft id, if any, and what to do about it."""
    draft_id = drafter.find_draft(email.thread_id)
    draft = drafter.get_draft(draft_id) if draft_id else None
    last_sent = threads.last_sent_at(email.thread_id)
    ours = drafts.our_draft(entries, draft_id) if draft_id else None
    return draft_id, drafts.conversation_verdict(
        email.date, draft, last_sent, ours, prompt_fp)


def _report_states(verdicts: dict, emails: dict, journal: Path, run_id: str) -> None:
    """Name every mail the conversation's state does something with.

    That is every mail the state keeps out of the fan-out, plus the one it puts in
    under a rewrite: a `redraft` is the only line that says a draft already in the
    mailbox is about to be replaced rather than written (STU-886).

    A superseded draft is journaled as well as printed: it is the one case where
    the mailbox holds something of ours that no longer answers anything, and the
    journal is where a human looks for what a run did with a draft (STU-945).
    """
    for message_id, (draft_id, verdict) in verdicts.items():
        if verdict == drafts.DRAFT:
            continue
        print(f"[drafts-pre] {message_id}: {verdict}", file=sys.stderr)
        if verdict == drafts.SUPERSEDED:
            record(journal, run_id, message_id, emails[message_id].thread_id,
                   drafts.SUPERSEDED, draft_id)


def _report_vanished(verdicts: dict, emails: dict, entries: list[dict],
                     drafter, journal: Path, run_id: str) -> None:
    """Name the draft a conversation about to be drafted again used to carry.

    Re-drafting is right — the mailbox no longer holds the reply, so the mail owes
    one — but the journal read alone cannot say why, and two draft ids on one
    conversation across runs is also what a broken guard looks like (STU-998).

    `DRAFT` alone and never `DRAFTABLE`: a `redraft` conversation is holding the very
    draft this would report as lost, and the count of `vanished` rows has to stay a
    count of lost drafts (STU-1028).

    The mailbox is asked where the draft went before the row is written (STU-1549): a
    draft a human sent left Drafts too, and is journaled `sent` rather than lost.
    """
    for message_id, (_, verdict) in verdicts.items():
        if verdict != drafts.DRAFT:
            continue
        thread_id = emails[message_id].thread_id
        lost = drafts.vanished_draft_id(entries, thread_id)
        if not lost:
            continue
        sent = drafts.sent_at(drafter.get_draft(lost))
        status = drafts.SENT if sent else drafts.VANISHED
        print(f"[drafts-pre] {message_id}: {status} {lost}", file=sys.stderr)
        record(journal, run_id, message_id, thread_id, status, lost, sent_at=sent)


def _pending_items(paths: dict, policy, report: dict, emails: dict,
                   wanted: list, taxonomy) -> tuple[list[dict], dict]:
    # A reply the reconciliation dropped without anyone deciding nothing was owed. Apply
    # names the same conversations; unnamed here, a mail nobody answers is the failure the
    # whole pipeline exists to avoid.
    held = drafts.held_replies(report, taxonomy)
    if held:
        print(
            f"[drafts-pre] {len(held)} message(s) asked for a reply their conversation "
            f"does not, held: {', '.join(sorted(held)[:5])}",
            file=sys.stderr,
        )

    missing = [message_id for message_id in wanted if message_id not in emails]
    if missing:
        raise SystemExit(
            f"[ERROR] {len(missing)} message(s) need a reply but are absent from the "
            f"corpus — report and dump are from different runs: {', '.join(missing[:5])}"
        )

    # What the conversation's state — not the mere presence of a draft — says to
    # do with each pending mail (STU-945). The read belongs here rather than in
    # the tool: inside a run, the draft/QA group still has to rewrite the draft it
    # just made.
    drafter = get_drafter(policy.email_provider)
    threads = get_thread_reader(policy.email_provider) if wanted else None
    journal = Path(paths["draft_journal"])
    entries = read_entries(journal) if journal.exists() else []
    # Read before the verdicts and not beside the items: today's prompt is what says
    # whether a draft of ours is still the draft this repo would write (STU-886).
    prompt_fp = drafts.prompt_fingerprint(STUDIO_DIR, policy.anonymization_fields)
    # Mail the reviewer answers by hand is held before the mailbox is asked anything
    # (STU-2003): its draft would be noise whatever state the conversation is in.
    holds = drafts.load_holds(Path(paths["draft_holds"]))
    verdicts = {
        message_id: (None, hold)
        if (hold := drafts.content_hold(emails[message_id].body_snippet, holds))
        else _verdict(emails[message_id], drafter, threads, entries, prompt_fp)
        for message_id in wanted
    }
    _report_states(verdicts, emails, journal, report["run_id"])
    _report_vanished(verdicts, emails, entries, drafter, journal, report["run_id"])
    states = {message_id: verdict for message_id, (_, verdict) in verdicts.items()}
    wanted = [mid for mid in wanted if states[mid] in drafts.DRAFTABLE]

    # The corpus dump only carries the provider's preview — the question is often
    # past its cutoff (STU-852). The real bodies are read here, for the pending
    # subset alone, and tokenized below with everything else.
    bodies = get_body_fetcher(policy.email_provider).fetch_bodies_by_ids(wanted) if wanted else {}

    # What the mail being answered answers (STU-941). Read per conversation rather
    # than off the corpus: the earlier turns of a two-turn thread were classified
    # in a previous run and archived out of the fetch that produced this one.
    history = {
        message_id: threads.fetch_thread_history(emails[message_id].thread_id, message_id)
        for message_id in wanted
    }

    keymap_path = Path(paths["keymap"])
    tokenizer = Tokenizer(load_keymap(keymap_path) if policy.anonymization_fields else None)

    # The index, not the memories: a line is what tells the drafter whether a file
    # is worth opening, and opening it is a tool call (STU-873).
    memory_index = club_memory.load_index(club_memory.MEMORY_DIR)

    # Read once for the batch, not per item: every draft of a fan-out is written in the
    # same minute, and a period that turned over mid-loop would split one run's replies
    # between two wishes.
    written_period = drafts.period_at(datetime.now(timezone.utc), ZoneInfo(policy.timezone))

    pending = [
        drafts.draft_item(
            emails[message_id], bodies.get(message_id, ""), history[message_id],
            tokenizer, policy.anonymization_fields, prompt_fp, paths, memory_index,
            report["run_id"], written_period,
        )
        for message_id in wanted
    ]
    if policy.anonymization_fields:
        save_keymap(keymap_path, tokenizer.keymap)
        record_minted(
            run_keymap_path(keymap_path, report["run_id"]), tokenizer.minted())
        record_sent(sent_path(keymap_path, report["run_id"]), pending)
    # What the stage line will carry (STU-1186). Built here and not in `drafts-post`,
    # because this is the stage that decided it — `drafts-post` sees the fan-out's
    # results and never the conversations that never reached it.
    return pending, drafts.selection(states, held)


def main() -> None:
    payload = json.load(sys.stdin)
    ctx = yaml.safe_load(payload.get("additional_context", "") or "") or {}
    for required in ("corpus", "report"):
        if required not in ctx:
            raise SystemExit(f"[ERROR] drafts input needs a `{required}`")

    paths = _paths(ctx)
    policy = load_policy(Path(paths["policy"]))
    taxonomy = load_taxonomy(Path(paths["taxonomy"]))
    report = json.loads((REPO_ROOT / ctx["report"]).read_text(encoding="utf-8"))
    emails = {e.message_id: e for e in dedupe_by_message(load_corpus(REPO_ROOT / ctx["corpus"]))}

    wanted = drafts.reply_targets(report, taxonomy)
    # Opened before the mailbox is touched, so a stage killed where Python cannot
    # catch it — a timeout, a contract violation — still leaves the run id and the
    # count it was handed. A `started` with no terminal line is that death.
    stage_log = Path(paths["stage_log"])
    record_stage(stage_log, report["run_id"], STARTED, len(wanted))
    try:
        pending, selection = _pending_items(paths, policy, report, emails, wanted, taxonomy)
    except (Exception, SystemExit) as exc:  # a refusal is a stage death like any other
        record_stage(stage_log, report["run_id"], FAILED, len(wanted),
                     error=f"{type(exc).__name__}: {exc}")
        raise

    print(f"[drafts-pre] {len(pending)} emails need a reply", file=sys.stderr)
    json.dump({
        "run_id": report["run_id"],
        "needs_draft": bool(pending),
        "needs_reply": len(wanted),
        # Why `needs_reply` is not what the fan-out got (STU-1186), on its way to the
        # terminal stage line: which conversations were handed over, and which were
        # held back under which reason. A `failed` line carries none of it — a stage
        # that died deciding decided nothing it can report.
        "selection": selection,
        "pending": pending,
        # Which provider the drafting calls will run on, resolved here because only
        # the run sees the override and `drafts-post` records it on the leg (STU-1145).
        # `classify-pre` reads the same key off its own input for the same reason.
        "provider": resolved_provider(STUDIO_DIR, ctx.get("provider"), "drafter"),
        "paths": paths,
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
