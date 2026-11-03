"""Widen a fetch from messages to conversations (STU-963).

STU-940 made the conversation the verdict unit, but only within one report. A push
run's report holds just the message the notification named, so a reply landing on a
thread an earlier run archived reconciles as a conversation of one: the reply stays
in the Inbox while its thread sits in the Archive — the split STU-940 exists to close,
moved from within a run to across runs. `mailbox-full` has the same hole whenever a
thread straddles two fetch windows.

Reconciliation cannot fix that; it only reconciles the verdicts it is handed. This is
the upstream half: every message of a fetched message's conversation joins the report,
so the thread reconciles whole.

The siblings are re-classified, not read back from an earlier run's verdicts. The
verdict store is LLM-only and keyed by sender, and a report lives in gitignored
`reports/` — a sibling verdict read back would be decided by whatever rules ran that
day, on a machine that happens to still hold the file. Re-classifying costs stage 1,
which is free, plus a stage-2 call for any sibling that escalates.

The `max_results` a caller passed for the listing does not bound the result: it caps
how many conversations are looked at, not how much of one is read. Half a conversation
is the failure this module exists to remove.

The run's date floor (STU-1123) is applied here for the same reason, one step earlier:
the order between the two *is* the rule, and a caller free to get it wrong would rebuild
the split by dropping the pre-cutoff half of a live thread. So `start_date` is a required
argument rather than a filter each of the seven corpus entry points remembers to call —
the same shape `pipeline.run` uses for its tokenizer, where the branch that sends the
mailbox out unguarded must not be the one you get by forgetting.
"""

from datetime import date
from zoneinfo import ZoneInfo

from email_classifier.fetch import window
from email_classifier.providers import ConversationReader, Fetcher


def expand_to_conversations(
    fetcher: Fetcher | ConversationReader, inputs: list[dict], *, start_date: date,
    tz: ZoneInfo = window.CLUB_TZ,
) -> list[dict]:
    """The listing, bounded by `start_date`, plus every other live message of its
    conversations.

    Bounded first and widened second: what the floor decides is which conversations
    a run reaches for, and a conversation it reached for is read whole. A sibling
    older than the floor therefore stays — it is part of a thread that is in scope,
    and leaving it out is the Inbox/Archive split, not a boundary.

    Order is the fetch's order, siblings appended after it — reconciliation reads
    recency off each verdict's `date` and never off this list (STU-940), so nothing
    downstream depends on where a sibling lands.
    """
    inputs = window.in_scope(inputs, start_date, tz)
    known = {item["message_id"] for item in inputs}
    missing: list[str] = []
    for thread_id in dict.fromkeys(item["thread_id"] for item in inputs):
        missing.extend(
            message_id
            for message_id in fetcher.conversation_message_ids(thread_id)
            if message_id not in known and message_id not in missing
        )
    if not missing:
        return inputs
    return [*inputs, *fetcher.fetch_inputs_by_ids(missing)]
