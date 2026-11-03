"""Shared logic of the `drafts` pipeline (STU-850).

Same topology as `classify-batch`: `drafts-pre` picks what needs a reply out of
the classification report and builds the fan-out items, the engine's `map:` runs
`email-draft` once per email, `drafts-post` counts what the mailbox actually
received. This module holds what both scripts need.

The fan-out item is the resume key, so everything in it is a cache buster. It
carries two fields that are not the mail. `run_id`, because the journal line the
draft tool writes is worthless to an audit without it (STU-917); and
`written_period`, because the wish a reply closes on follows the club's clock
(STU-1119). The price is smaller than it looks: a mail that already holds a draft
is dropped by `drafts-pre` before the fan-out ever sees it (STU-945), so the items
a *later* run submits are the ones no cache could serve anyway, a replay of the
*same* run keeps its run id and still hits, and the period takes two values that
turn over once a day rather than a timestamp that busts every call.

Pure module — no I/O beyond hashing the config files a fingerprint covers.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from email_classifier import envelope, lang, prompt_sources, voice_baseline
from email_classifier.anonymize import Tokenizer, anonymize_fields
from email_classifier.core.contract import Taxonomy
from email_classifier.core.types import EmailInput
from email_classifier.fetch.window import CLUB_TZ
from email_classifier.orchestrate import batch, conversation
from email_classifier.orchestrate.report import EmailVerdict

envelope.load(lang.deployment_policy().envelope_rewriters)

# The action that means "a human still owes this sender an answer" (taxonomy skill).
REPLY_ACTION = "needs_reply"

# Where `Bonne journée,` turns into `Bonne soirée,`, moved out of this file into
# `rules/voice.yaml` (STU-1472): measured on the 404 replies of `Éléments envoyés` that
# close on one of the two (STU-1119), the cut calls 366 of them right, against 222 for
# always writing `Bonne journée,`. `None` on a deployment with no archive measured yet;
# `period_at` below never guesses a split it has no basis for.
EVENING_FROM_HOUR = (voice_baseline.load() or {}).get("evening_from_hour")

DAY = "day"
EVENING = "evening"


def period_at(now: datetime, tz: ZoneInfo = CLUB_TZ) -> str:
    """Which half of the club's day a draft written `now` falls in.

    Takes the instant rather than reading the clock, so the module stays pure and the
    boundary is testable without freezing time. The callers are scripts, which is where
    a clock belongs. `tz` defaults to the first deployment's zone (STU-1462) for a caller that has
    not yet threaded `policy.timezone` through.

    `EVENING_FROM_HOUR` is `None` on a deployment with no measured cutoff (STU-1472):
    rather than fabricate one, every draft is `day`, a single unified closing and not
    a guessed split.
    """
    if EVENING_FROM_HOUR is None:
        return DAY
    return EVENING if now.astimezone(tz).hour >= EVENING_FROM_HOUR else DAY

# No `body_snippet`: the item carries `body_text`, the full mail (STU-852), and the
# preview is its first line — sending both would just spend tokens twice.
_EMAIL_FIELDS = ("message_id", "thread_id", "from_address", "subject", "date")

_DRAFTING_AGENTS = ("drafter", "draft-reviewer")

# What shapes a draft but is not the email, besides the two agents' own skills and
# tools — read off their agent YAML rather than re-listed here (STU-1458). Editing the
# club skill must re-draft every pending mail.
_PROMPT_SOURCES = (
    "agents/drafter.agent.yaml",
    "agents/draft-reviewer.agent.yaml",
    "contracts/email-draft.contract.yaml",
    "contracts/write-draft.contract.yaml",
    "contracts/draft-qa.contract.yaml",
    "pipelines/email-draft.pipeline.yaml",
    "invariants.md",
)


def _prompt_sources(studio_dir: Path) -> tuple[str, ...]:
    """Both drafting agents' own paths plus their declared skills and tools.

    Skills and tools are deduplicated across the two agents rather than listed once
    each: `drafter` and `draft-reviewer` both declare the facts skill, and a source
    fingerprinted twice would still only be one file to bust the cache on.
    """
    skills: dict[str, str] = {}
    tools: dict[str, str] = {}
    for agent in _DRAFTING_AGENTS:
        skills |= prompt_sources.skill_paths(studio_dir, agent)
        tools |= prompt_sources.tool_paths(studio_dir, agent)
    return tuple(dict.fromkeys((*_PROMPT_SOURCES[:2], *skills.values(), *tools.values(),
                                 *_PROMPT_SOURCES[2:])))


# Where the drafting path reads and writes, relative to the repo root. Shared by
# `drafts-pre` and `redraft`: a keymap read from a second path would mint tokens
# of its own, and the drafter would answer a person it has already named.
DEFAULT_PATHS = {
    "policy": "rules/policy.yaml",
    "taxonomy": "rules/taxonomy.yaml",
    "keymap": "logs/anonymization/keymap.json",
    "draft_journal": "logs/drafts/journal.jsonl",
    "stage_log": "logs/drafts/stages.jsonl",
    "draft_holds": "rules/draft_holds.yaml",
}


def prompt_fingerprint(studio_dir: Path, anonymization_fields: list[str]) -> str:
    return batch.prompt_fingerprint(studio_dir, anonymization_fields, _prompt_sources(studio_dir))


def pending_message_ids(report: dict) -> list[str]:
    """The message ids the report says need a reply — errors and other actions out.

    Per message deliberately, and only `undo` reads it: it scopes the shared drafts
    journal down to what this run could have drafted, and a run that drafted under an
    older rule must still be findable.
    """
    return [
        verdict["message_id"]
        for verdict in report["verdicts"]
        if verdict.get("status") == "ok" and verdict.get("suggested_action") == REPLY_ACTION
    ]


def reply_targets(report: dict, taxonomy: Taxonomy) -> list[str]:
    """The one message per conversation that owes a reply (STU-940).

    A draft is a mailbox outcome like a folder move, so it takes the same unit: the
    conversation's reconciled verdict says whether a reply is owed, and it is owed on the
    latest message — not once per message of a thread whose verdicts happen to agree.
    `taxonomy` is this deployment's own bucket shape (STU-1461), threaded to `reconcile`.
    """
    return [
        decision.latest_message_id
        for messages in conversation.group(_verdicts(report)).values()
        if (decision := conversation.reconcile(messages, taxonomy))
        and decision.suggested_action == REPLY_ACTION
    ]


# The two ways `reply_targets` drops a message the report marked `needs_reply`, named
# rather than counted together (STU-1186): they are different failures of a different
# thing, and a stage line that said only "two held" would not tell an audit which.
THREAD_UNDECIDED = "thread_undecided"
CLAMPED_BY_BUCKET = "clamped_by_bucket"


def held_replies(report: dict, taxonomy: Taxonomy) -> dict[str, str]:
    """Messages asking for a reply their conversation does not ask for, and why — minus
    the ones a later message deliberately closed.

    Two ways to lose a reply without anyone saying so: the conversation was not decided
    whole, or the clamp bent the latest message's action to fit the reconciled bucket —
    a notification posting last on a thread that holds a question. Neither is a decision
    that nothing is owed, and a mail silently left unanswered is worse than one left
    unmoved. A "merci" closing a thread is the third case and is not held: the latest
    message's own action carried, unbent.
    """
    held: dict[str, str] = {}
    for messages in conversation.group(_verdicts(report)).values():
        asked = {
            m.message_id
            for m in messages
            if m.status == "ok" and m.suggested_action == REPLY_ACTION
        }
        if not asked:
            continue
        decision = conversation.reconcile(messages, taxonomy)
        if decision is None:
            held.update(dict.fromkeys(asked, THREAD_UNDECIDED))
            continue
        latest = next(m for m in messages if m.message_id == decision.latest_message_id)
        if decision.suggested_action != latest.suggested_action:
            held.update(dict.fromkeys(asked, CLAMPED_BY_BUCKET))
    return held


def _verdicts(report: dict) -> list[EmailVerdict]:
    return [EmailVerdict(**v) for v in report["verdicts"]]


def draft_item(
    email: EmailInput,
    body_text: str,
    thread_history: list[dict],
    tokenizer: Tokenizer,
    anonymization_fields: list[str],
    prompt_fp: str,
    paths: dict[str, str],
    memory_index: str,
    run_id: str,
    written_period: str,
) -> dict:
    """One fan-out item: the child's input, and therefore its resume key.

    `message_id` and `thread_id` ride along because the draft tool reads them from
    the item rather than from the agent — the agent chooses the words, never which
    conversation they land in.

    `run_id` rides along for the same reason and one more: the tool's journal line
    is the only record that a draft was created, and an audit cannot tell whose it
    is without it (STU-917). Empty for `redraft`, which serves a human and has no
    run to name.

    `body_text` is the mail as the sender wrote it (STU-852) — the question is
    often past the preview cutoff. It falls back to the preview when the mailbox
    has no body to give, which is a real shape for subject-only mail.

    `thread_history` is the rest of the conversation, oldest first (STU-941) — a
    follow-up like "ok mais et pour le samedi?" is unanswerable without the reply
    it follows. Its bodies are tokenized with the batch keymap like `body_text`,
    not flattened the way a precedent is: this is the same exchange, so its
    `PERSON_1` is the person the draft is addressed to.

    `memory_index` is the club memory's index (STU-873), carried here because the
    item is what reaches the prompt — and tokenized with the batch's keymap like
    the mail itself (STU-879). A memory an agent wrote already holds tokens and
    the tokenizer leaves those alone; one a human hand-edited holds real names,
    and this is the only place that catches them.

    `written_period` is `period_at` on the clock the caller read — the time the draft
    is being written, never the reader's, which nothing here can know.

    `reply_to`/`reply_name` are empty for ordinary mail and carry the parent
    behind a contact-form notification (STU-888). They are parsed off the
    cleartext body, before the tokenizer runs, then tokenized with everything
    else — the address keeps the token its copy in `body_text` gets, and
    `write-draft` restores both at the mailbox boundary.
    """
    item: dict = {field: getattr(email, field) for field in _EMAIL_FIELDS}
    item["body_text"] = body_text or email.body_snippet
    # A turn's body rides in the same scopes as `body_text` — reading more of a
    # conversation never tokenizes less of it.
    tokenize_bodies = "body_text" in anonymization_fields
    # A turn is vetoed against its own sender's domain (STU-1467); the address is
    # only a tokenizer context and never reaches the item.
    item["thread_history"] = [
        {**{k: v for k, v in turn.items() if k != "from_address"}, "body": tokenizer.anonymize(
            turn["body"], from_address=turn.get("from_address") or email.from_address
        ) if tokenize_bodies else turn["body"]}
        for turn in thread_history
    ]
    item["memory_index"] = memory_index
    form = envelope.resolve(item["body_text"])
    item["reply_to"] = form.reply_to if form else ""
    item["reply_name"] = form.name if form and form.name else ""
    # The override is a sender, so it is tokenized exactly when senders are — the
    # `body_only` scope leaves `from_address` readable and this with it. The name
    # is not salutation-anchored, so `anonymize_fields` would pass over it; under
    # `full` it is tokenized by hand rather than added to the item in the clear.
    fields = list(anonymization_fields)
    if "from_address" in fields:
        fields.append("reply_to")
        if item["reply_name"]:
            item["reply_name"] = tokenizer.token_for(item["reply_name"], "person")
    anonymize_fields(item, tokenizer, fields, from_address=email.from_address)
    if tokenize_bodies:
        for turn in item["thread_history"]:
            turn["body"] = tokenizer.redact_known(turn["body"])
    item["prompt_fingerprint"] = prompt_fp
    # `write-draft` reads all three: which mailbox it writes to, under which
    # keymap, and which run owns the journal line (STU-1030).
    item["policy"] = paths["policy"]
    item["keymap"] = paths["keymap"]
    item["journal"] = paths["draft_journal"]
    item["run_id"] = run_id
    item["written_period"] = written_period
    return item


DRAFT = "draft"
REDRAFT = "redraft"
BLOCKED = "blocked"
ANSWERED = "answered"
SUPERSEDED = "superseded"
VANISHED = "vanished"
SENT = "sent"

_VERDICTS = (DRAFT, REDRAFT, BLOCKED, ANSWERED, SUPERSEDED, VANISHED, SENT)

# Cut before a hold reads the mail: an iPhone reply's `uniqueBody` still carries the
# club's quoted message, whose words are not the sender's.
_QUOTE_MARKERS = lang.compiled_quote_markers(lang.deployment_config())


def load_holds(path: Path) -> dict[str, dict]:
    """`rules/draft_holds.yaml` compiled, or no holds on a deployment without one
    (STU-2003).

    A hold's name lands in `selection.held` next to the mailbox's own verdicts, so a
    name reusing one would fold two reasons into one count, and a rule with no `all`
    pattern would hold every short mail: both refuse.
    """
    if not path.exists():
        return {}
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("holds") or {}
    holds = {}
    for name, rule in raw.items():
        if name in _VERDICTS or not rule.get("all"):
            raise ValueError(f"{path}: hold `{name}` reuses a conversation verdict's "
                             f"name or has no `all` pattern")
        holds[name] = {
            "all": [re.compile(p, re.I) for p in rule["all"]],
            "none": [re.compile(p, re.I) for p in rule.get("none") or ()],
            "max_words": rule.get("max_words"),
            "quoted": bool(rule.get("quoted")),
        }
    return holds


def content_hold(body: str, holds: dict[str, dict]) -> str | None:
    """The first hold whose rule the mail's own text meets, or `None` (STU-2003)."""
    cut = min((m.start() for m in (p.search(body) for p in _QUOTE_MARKERS) if m),
              default=len(body))
    text = body[:cut]
    for name, rule in holds.items():
        if rule["quoted"] and cut == len(body):
            continue
        if rule["max_words"] is not None and len(text.split()) > rule["max_words"]:
            continue
        if (all(p.search(text) for p in rule["all"])
                and not any(p.search(text) for p in rule["none"])):
            return name
    return None


# The two verdicts that put a mail in the fan-out. `REDRAFT` differs from `DRAFT` in
# what the mailbox already holds, not in what is owed: `upsert_reply_draft` rewrites
# that draft in place rather than leaving a second one behind.
DRAFTABLE = (DRAFT, REDRAFT)

# The journal row is stamped locally once the mailbox has answered, so an untouched
# draft's `lastModifiedDateTime` is always the earlier of the two. The slack is what
# absorbs the skew between this machine's clock and the server's.
_CLOCK_SKEW = timedelta(minutes=2)


def _at(timestamp: str) -> datetime:
    """Graph spells UTC `Z`; a projected date and the journal spell it `+00:00`."""
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def edited_since(last_modified: str, journaled_at: str) -> bool:
    """Whether a human has touched the draft that journal row created.

    One statement with two readers: `undo` refuses to delete a draft on it (STU-854)
    and `conversation_verdict` refuses to rewrite one on it (STU-886). Two definitions
    of "a human owns this draft" would answer the same question two ways.
    """
    return _at(last_modified) > _at(journaled_at) + _CLOCK_SKEW


def our_draft(entries: list[dict], draft_id: str) -> dict | None:
    """The row this pipeline wrote for the draft the mailbox is holding, if it wrote one.

    Keyed by draft id and never by run (STU-886), unlike `drafted_message_ids`: the
    question is whether this pipeline made that draft, and the run that made it is by
    construction an earlier one. Only an `ok` row is a write — `deleted`, `superseded`,
    `vanished` and `sent` are reports *about* a draft, made by code that did not create it.

    The last such row, because `upsert_reply_draft` rewrites in place: a draft answers
    for the prompt it was last written under, never the first.
    """
    last = None
    for entry in entries:
        if entry.get("draft_id") == draft_id and entry.get("status") == "ok":
            last = entry
    return last


def conversation_verdict(
    message_date: str,
    draft: dict | None,
    last_sent: str | None,
    ours: dict | None,
    prompt_fp: str,
) -> str:
    """What the conversation's state says to do with a mail marked `needs_reply`.

    STU-884 asked only whether a draft existed, which conflates four different
    conversations (STU-945, then STU-886):

    - `BLOCKED` — a draft is waiting for a human and nothing has answered the
      thread since. Re-drafting would overwrite an edit they had started.
    - `REDRAFT` — that draft is one this pipeline wrote, nobody has touched it
      since, and it was written under a prompt the repo has since corrected. It is
      rewritten in place: `_PROMPT_SOURCES` exists so that fixing a fee in
      the facts skill reaches every pending reply, and a mail held behind
      `BLOCKED` is never in the fan-out to notice the fingerprint changed.
    - `SUPERSEDED` — a human answered from the mailbox while that draft sat
      there, so the draft answers a question that has already been answered. Left
      in place and reported: deciding what to do with a stale draft is theirs.
    - `ANSWERED` — no draft, and the club's last reply on the thread is newer
      than the mail itself. The reply was written and sent; drafting it again
      would answer it twice.
    - `DRAFT` — everything else, which is what "new mail arrived after we
      replied" looks like: a reply was sent, and this mail came after it.

    `ours` is the journal row that created the draft (`our_draft`), and both halves of
    it are needed before a rewrite: a draft no row of ours claims was written by
    somebody else, and a row whose draft has moved since was opened and edited. Either
    way `BLOCKED` stands — destroying a human's work is the failure this order exists
    to prevent, and a draft left stale is one they can still see and fix.

    A row naming no fingerprint at all reads as older than today's, the one place this
    answers a question it cannot measure. It costs one re-draft per conversation still
    holding a draft from before the field existed, paid once and self-clearing, and it
    can overwrite nothing a human owns — the edit test runs first. Reading it the other
    way would freeze exactly the drafts this exists to unfreeze.

    Dates decide, not the report: the mailbox can move between the fetch that
    produced the report and this stage.
    """
    if draft is None:
        return ANSWERED if last_sent and _at(last_sent) > _at(message_date) else DRAFT
    if last_sent and _at(last_sent) > _at(draft["lastModifiedDateTime"]):
        return SUPERSEDED
    if ours is None or edited_since(draft["lastModifiedDateTime"], ours["timestamp"]):
        return BLOCKED
    return BLOCKED if ours.get("prompt_fingerprint") == prompt_fp else REDRAFT


def selection(conversation_states: dict[str, str], held: dict[str, str]) -> dict:
    """What `drafts-pre` decided, for the stage line to carry (STU-1186).

    `needs_reply` and `drafted` disagreeing has two opposite causes and the line could
    not tell them apart: `drafts-pre` held the conversation on purpose, and nothing is
    owed, or the leg selected it and lost it, and a parent is waiting. Run `af590fa8`
    wrote `needs_reply: 1, drafted: 0` and it took the Studio journal — gitignored,
    pruned at 30 days (STU-1012) — to find that the conversation already carried a
    draft. This is STU-917's argument one field further in: a stage that ran fine and
    drafted nothing must not look like a stage that lost something.

    Two maps rather than one, because the counts do not sit on the same side of
    `needs_reply` and adding them together is the arithmetic this split exists to
    prevent:

    - `held` counts the conversations `reply_targets` did select and the mailbox's own
      state then kept back, under the `conversation_verdict` that kept them —
      `selected` plus `held` is `needs_reply`, so `selected` minus `drafted` is what
      the leg lost.
    - `unreconciled` counts the messages `reply_targets` never selected at all
      (`held_replies`), because the conversation they belong to does not ask for a
      reply. They are outside `needs_reply` by construction: the clamp only ever bends
      an action *away* from `needs_reply` (`conversation._CLAMP`), and an undecided
      thread reconciles to no verdict.

    The vocabulary is `conversation_verdict`'s own, not a second naming of it: an
    unsent draft (`blocked`), a thread the club already answered (`answered`) and a
    draft a human replied past (`superseded`) are three different reasons a reply is
    not owed today, and collapsing them into "already drafted" would be wrong for the
    one where the mailbox holds no draft at all. `redraft` counts as selected and not
    as held (STU-886): the conversation went to the fan-out like any other, and the
    draft it already carries is one the leg is asked to replace. A `content_hold` name
    (STU-2003) sits beside them: mail a human answers by hand, whatever the mailbox holds.
    """
    return {
        "selected": sum(
            1 for state in conversation_states.values() if state in DRAFTABLE),
        "held": _tally(
            state for state in conversation_states.values() if state not in DRAFTABLE),
        "unreconciled": _tally(held.values()),
    }


def _tally(reasons) -> dict[str, int]:
    counts: dict[str, int] = {}
    for reason in reasons:
        counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def sent_at(draft: dict | None) -> str | None:
    """When the mailbox says a draft that left Drafts was sent, or None.

    Leaving Drafts is not being deleted (STU-1549): of 22 drafts journaled `vanished`
    on the serving host by 2026-09-20, Graph still held 19 as sent messages (`isDraft` false,
    a `sentDateTime`), 2 answered 404 and 1 was neither. A sent draft is the pilot
    working, so it is journaled `sent` and `vanished` keeps meaning gone. Gmail moves a
    sent draft under a new id and answers 404, so there it still reads as `vanished`.
    """
    if draft is None or draft["isDraft"]:
        return None
    return draft.get("sentDateTime")


def vanished_draft_id(entries: list[dict], thread_id: str) -> str | None:
    """Id of the draft this pipeline last put on that conversation, unless we removed it.

    Called only where the mailbox says the conversation carries no draft, so the answer
    is the id of a draft that is gone. Every deletion of ours is journaled `deleted` —
    `undo` (STU-854) and `redraft` both write one — so anything else is a deletion from
    outside, and re-drafting is the right answer to it.

    Naming it is what an audit cannot recover otherwise (STU-998): a human deleting a
    draft leaves no trace anywhere, so two draft ids on one conversation across runs
    read exactly like the one-draft-per-conversation guard failing.

    A `vanished` row of ours accounts for the loss the same way a `deleted` one does
    (STU-1028), and for the same reason: it is a report already made. `drafts` is the
    stage `mailbox-full` tolerates in failure (STU-850) and the push path drops on
    purpose, so a run that names a loss and then dies drafting leaves the row as the
    thread's last word — and every run after it would name that one loss again. The
    count of `vanished` rows is the whole point of the row, so it has to be a count of
    lost drafts and not of runs that failed to replace one. The next `vanished` on the
    thread therefore waits for a draft made and lost in turn. A `sent` row settles it
    the same way (STU-1549): it is the same report, with the loss explained.

    A `held` row names a human's draft that `write-draft` left alone (STU-1548), not
    one of ours, so it never stands as the thread's last draft.
    """
    last = None
    for entry in entries:
        if (entry.get("thread_id") == thread_id and entry.get("draft_id")
                and entry.get("status") != "held"):
            last = entry
    return last["draft_id"] if last and last["status"] not in ("deleted", VANISHED, SENT) else None


def drafted_message_ids(entries: list[dict], run_id: str) -> set[str]:
    """Message ids *this run* journaled a draft for — real drafts, not claimed ones.

    Scoped by the line's `run_id` (STU-917) and never by message id alone
    (STU-1003): the journal is shared, two runs over the same mailbox share most
    of their `needs_reply` set, and a mail whose draft a human deleted is pending
    again — so a run whose drafter died would be counted as having drafted it,
    and `logs/drafts/stages.jsonl` would carry that number into an audit. The
    clock separates them no better: the drafts stage writes after its own report
    is on disk, inside the window of whatever run started meanwhile.

    A line without a run id is unattributable, not a candidate: `redraft` writes
    one for a human, and lines older than STU-917 name nothing. Guessing which
    run owns them is exactly what this scoping exists to stop.
    """
    return {entry["message_id"] for entry in entries
            if entry.get("status") == "ok" and entry.get("run_id") == run_id}
