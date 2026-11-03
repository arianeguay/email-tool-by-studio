"""The verdict unit for a conversation (STU-940).

Classification is per message; the mailbox is per conversation. An Outlook category
attaches to a message, but a folder move does not stay put in a reader's eyes: three
replies free to disagree leave one thread split between the Inbox and the Archive, and
the second apply moves a message out from under the first.

The unit apply acts on is therefore the conversation. Per-message verdicts stand — they
are what the rules and the LLM produce — and this module is the one place stating how
they reconcile:

* **Bucket** — the highest-precedence bucket any message of the conversation carries, in
  the deployment's own taxonomy order (STU-1461: `Taxonomy.buckets`, first listed is
  highest precedence). A newsletter a parent replied into stopped being a newsletter,
  and a deployment's own "in doubt, default to the first bucket" rule says which way
  the tie falls.
* **Action** — the action of the most recent message. What a conversation owes is a claim
  about now, and only the latest message can make it: a "merci" closes a thread that its
  opening question had left needing a reply.
* **Clamp** — that action is then held to the reconciled bucket's action list. Without it,
  a notification posting last on a thread would archive the unanswered question above it.

The three are not interchangeable: reconciling the action by precedence instead would
never let a thread close, and reconciling the bucket by recency would let the last
message rewrite what the conversation is about.

Reconciliation is all-or-nothing. A conversation with any message the report did not
decide produces no verdict at all — acting on the decided half is the split this module
exists to stop.

The unit is the same one message earlier: `collapse` (STU-944) turns a run's escalated
messages into one stage-2 call per conversation, so the verdicts reaching `reconcile`
were never free to disagree in the first place.

Pure module — no I/O.
"""

from dataclasses import dataclass

from email_classifier.core.contract import Taxonomy
from email_classifier.core.types import EmailInput
from email_classifier.orchestrate.report import EmailVerdict


@dataclass(frozen=True)
class ThreadVerdict:
    """What apply acts on: one verdict for every message of one conversation."""

    thread_id: str
    bucket: str
    suggested_action: str
    rule_id: str | None       # of the message the action came from — what the journal cites
    latest_message_id: str    # the message the action came from; where a reply belongs


def group(verdicts: list[EmailVerdict]) -> dict[str, list[EmailVerdict]]:
    """The report's verdicts by conversation, both orders preserved."""
    threads: dict[str, list[EmailVerdict]] = {}
    for verdict in verdicts:
        threads.setdefault(verdict.thread_id, []).append(verdict)
    return threads


def reconcile(messages: list[EmailVerdict], taxonomy: Taxonomy) -> ThreadVerdict | None:
    """One conversation's verdict, or None when the report did not decide all of it.

    Recency is read from each verdict's `date`, never from the report's order: that order
    is the fetch's, and Graph hands its pages back newest-first. A multi-message
    conversation whose verdicts predate that field (STU-940) therefore reconciles to None
    rather than to whichever message the list happens to end on.

    An undecided message is ordinary — an escalation, a stage-2 error. A message decided
    onto a bucket the taxonomy does not name is not: it raises, because holding it for
    review would hold it every run, silently, forever.

    `taxonomy` is this deployment's own bucket shape (STU-1461) — precedence, the
    allowed-action table and the clamp fallback all come from it, never from a kernel
    literal.
    """
    # Every action a bucket can be finalized on. `escalate_to_llm` is deliberately
    # absent: a message still on its way to stage 2 is one the report has not decided.
    applicable = frozenset().union(*taxonomy.allowed_actions.values())
    if not all(m.status == "ok" and m.suggested_action in applicable for m in messages):
        return None
    if len(messages) > 1 and not all(m.date for m in messages):
        return None
    for m in messages:
        if m.bucket not in taxonomy.buckets:
            raise ValueError(
                f"{m.message_id}: bucket {m.bucket!r} is not one of "
                f"{', '.join(taxonomy.buckets)}"
            )

    latest = max(messages, key=lambda m: (m.date, m.message_id))
    bucket = min((m.bucket for m in messages), key=taxonomy.buckets.index)
    action = latest.suggested_action
    if action not in taxonomy.allowed_actions[bucket]:
        action = taxonomy.clamp[bucket]

    return ThreadVerdict(
        thread_id=messages[0].thread_id,
        bucket=bucket,
        suggested_action=action,
        rule_id=latest.rule_id,
        latest_message_id=latest.message_id,
    )


@dataclass(frozen=True)
class Escalation:
    """One conversation's single trip to stage 2 (STU-944).

    Several messages of one thread landing in the same run used to be N independent
    escalations: N times the LLM cost, and N verdicts free to contradict each other
    before reconciliation ever saw them. One call answers for all of them.
    """

    head: EmailInput               # the most recent escalated message — what is classified
    history: list[EmailInput]      # the conversation's other messages in this run, oldest first
    message_ids: tuple[str, ...]   # every escalated message this one verdict covers


def collapse(emails: list[EmailInput], escalated: set[str]) -> list[Escalation]:
    """One escalation per conversation, in the order its first message escalated.

    The head is the most recent escalated message, matching the action half of the
    reconciliation rule: what a conversation owes is a claim about now.

    `history` carries every *other* message of the conversation present in the run,
    decided ones included. A reply is projected without its quote (STU-935), so
    reading it alone is reading half a sentence — and the sibling that supplies the
    other half is often the one a rule already decided.
    """
    threads: dict[str, list[EmailInput]] = {}
    order: list[str] = []
    for email in emails:
        threads.setdefault(email.thread_id, []).append(email)
        if email.message_id in escalated and email.thread_id not in order:
            order.append(email.thread_id)

    out: list[Escalation] = []
    for thread_id in order:
        messages = threads[thread_id]
        escalated_here = [m for m in messages if m.message_id in escalated]
        head = max(escalated_here, key=lambda m: (m.date, m.message_id))
        history = sorted(
            (m for m in messages if m.message_id != head.message_id),
            key=lambda m: (m.date, m.message_id),
        )
        out.append(Escalation(
            head=head,
            history=history,
            message_ids=tuple(m.message_id for m in escalated_here),
        ))
    return out


def transcript(history: list[EmailInput]) -> str:
    """The conversation's other messages as one text field.

    Flat, not a nested list: `thread_history` is one string field, tokenized as one
    (STU-1009) and declarable as one to a middleware that scopes plain strings only —
    a nested shape would reach the LLM untokenized either way. One rendering keeps
    both orchestrations asking the same question of the same agent.

    Called on an already-tokenized history: each turn is tokenized on its own before
    it is joined, or the trailing-fragment veto would only ever see the last one
    (STU-920).
    """
    return "\n\n".join(
        f"[{m.date}] {m.from_address}\n{m.body_snippet}" for m in history
    )
