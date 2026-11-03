"""Did the body the group shipped answer what the sender asked? (STU-1140)

`make qa-erosion` holds the draft still and lets only the reviewer's feedback move, so
by construction it cannot see a failure that travels *through* the rewrite.
`make draft-bakeoff` lets the draft move, but it varies the model too, so a flip has two
things to attribute it to. The run that motivated this measurement went through the gap
between them: the reviewer struck an ungrounded sentence and added that the one grounded
fact was the whole of what could be said, the drafter wrote only that fact, and the
reviewer approved its own instruction coming back — with the parent's actual question,
where to cancel online, never mentioned in the body that shipped.

Read pass by pass, every verdict there is defensible. The sequence is the defect. But the
sequence is not what makes it one: a draft shrinking toward the feedback is what the loop
is *for*, and a group that converges by cutting an invented sentence has done its job. So
this does not flag on the shape of the sequence. It asks the one question that separates
the two, and asks it of the source message rather than of the loop — is everything the
sender asked addressed somewhere in the body that shipped?

That question goes to a reader that has never seen the draft's history, and is not
`draft-reviewer`: the reviewer approved this body, so asking it again asks the party
under suspicion.

The sequence read is still here, and is printed beside the verdict rather than as it:
`converged_by_removal` says the loop is what dropped the answer, which is what makes a
flagged run this issue's rather than a plain drafting miss. It is attribution, never the
flag.

Pure — the journal and the coverage calls are the caller's.
"""

import json
from dataclasses import dataclass

from email_classifier import journal_sequence, qa_provider, qa_studio_version
from email_classifier.draft_delta import answer_part

APPROVED = "approved"
COVERED = "covered"
INCOMPLETE = "incomplete"

# Two ways a read is not a measurement, kept apart because their repairs are not the same
# and a shared name would hide one behind the other (STU-912). `ERROR` is a call that
# produced nothing readable — a dead provider, a timeout. `INCOHERENT` is a reader that
# answered and contradicted itself: it listed an unaddressed request and called the reply
# covered, or the reverse. Neither is a verdict the reader can return, so neither can be
# mistaken for one; both keep the group out of `flagged`, because a read that did not
# land is not a read that cleared the body.
ERROR = "error"
INCOHERENT = "incoherent"

NOT_A_READ = (ERROR, INCOHERENT)

_ITEM_STAGE = "drafts-pre"
# Read off the shared statement rather than restated: which stage heads a pass and which
# closes it is `journal_sequence`'s to say, and two copies drift. The middle one writes
# the draft to the mailbox and says nothing this reader needs.
_DRAFT_STAGE, _, _QA_STAGE = journal_sequence.DRAFT_STAGES
# The echo stage of `draft-qa-replay` and `draft-qa-erosion`. Its presence says the
# journal has no drafting in it at all — see `EchoJournal`.
_ECHO_STAGE = "replay-draft"


class Unattributable(Exception):
    """A drafting execution `journal_sequence` could not place on a conversation.

    Attribution recovers all but the case where two children hold the same stage open
    and neither is forced by what it does next. That execution is one some conversation's
    passes are short by, and which one is exactly the fact not known — so the refusal is
    the journal's rather than a group's: there is no line to name the gap on.

    Short of a pass, a group can report the wrong final body, so reading the rest anyway
    risks a false clear — the one direction this tool must not fail in. Refused for the
    reason a skipped check is not a passed one (STU-912).

    Measured 2026-09-01: zero unplaceable executions across the five production drafting
    journals in this checkout, 191 executions. This is the rare case, not the shape every
    production journal has — which is what it used to be, when the condition was two open
    fan-out items and `draft-items.pipeline.yaml` pins that fan-out at concurrency 2.
    """


class MalformedJournal(Exception):
    """A verdict with no draft in front of it."""


class OrphanGroup(Exception):
    """A drafted thread whose fan-out item is not in the journal."""


class EchoJournal(Exception):
    """A replay or erosion journal: its drafts are echoes, so it shipped no body.

    Refused by name rather than read as empty. Those runs re-serve a recorded draft on
    every pass, so the body at the end is one no group ever wrote toward — and the
    question here is what a group *shipped*. Left to fall through, a journal with no
    drafting stage reads back as a run that simply drafted nothing, which is a different
    thing and the one a caller would act on.
    """


@dataclass(frozen=True)
class Pass:
    """One iteration of the group: what the drafter wrote and what the reviewer said."""

    draft_body: str
    verdict: str
    critique: str
    issues: tuple[str, ...]

    @property
    def answer_chars(self) -> int:
        """Length of the answering part — greeting and closing are fixed furniture, so
        counting them measures the mobilier rather than the answer (STU-1130)."""
        return len(answer_part(self.draft_body))


@dataclass(frozen=True)
class Group:
    """One conversation's finished group: the message that came in, and every pass."""

    thread_id: str
    item: dict
    passes: tuple[Pass, ...]

    @property
    def body_text(self) -> str:
        return self.item.get("body_text", "")

    @property
    def approved(self) -> bool:
        return bool(self.passes) and self.passes[-1].verdict == APPROVED

    @property
    def final_body(self) -> str:
        return self.passes[-1].draft_body if self.passes else ""

    @property
    def answer_chars(self) -> tuple[int, ...]:
        return tuple(p.answer_chars for p in self.passes)

    @property
    def converged_by_removal(self) -> bool:
        """The approved body is shorter than the one before it, and the shortest of all.

        The mechanical half of the issue's claim: the group ended by taking something
        out. It is deliberately not the flag — cutting an invented sentence is the loop
        working — so this only ever qualifies a finding the coverage read produced.

        A single-pass approval is never this: nothing was fed back, so nothing complied.
        """
        chars = self.answer_chars
        if not self.approved or len(chars) < 2:
            return False
        return chars[-1] < chars[-2] and chars[-1] == min(chars)

    @property
    def last_rejection(self) -> Pass | None:
        """The pass whose feedback the approved body was written against."""
        return self.passes[-2] if self.approved and len(self.passes) >= 2 else None


def _events(lines):
    for line in lines:
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _passes(conversation: journal_sequence.Conversation) -> tuple[Pass, ...]:
    """One conversation's group iterations: what was written, and what was said of it.

    An attempt whose drafting stage failed is not one of them. Such a stage completes
    carrying no output at all, so it wrote no body — there is nothing to read and
    nothing for the next pass to be shorter than. A pass with no drafting stage in it at
    all is a different thing and raises: that is a verdict on nothing.
    """
    out = []
    for executions in conversation.passes:
        drafted = next((e for e in executions if e.stage == _DRAFT_STAGE), None)
        if drafted is None:
            raise MalformedJournal(
                f"a {_QA_STAGE} completion with no {_DRAFT_STAGE} in front of it"
            )
        if not drafted.output:
            continue
        body = drafted.output.get("draft_body", "")
        judged = next((e for e in executions if e.stage == _QA_STAGE), None)
        if judged is None:
            # A draft the reviewer never judged: the QA stage died on it.
            out.append(Pass(body, ERROR, "", ()))
            continue
        out.append(Pass(
            body,
            judged.output.get("verdict", ERROR),
            judged.output.get("critique", ""),
            tuple(judged.output.get("issues") or ()),
        ))
    return tuple(out)


def groups_from_journal(lines) -> list[Group]:
    """Every group a run's journal holds, in the order the run started drafting them.

    A group is one child run of the fan-out — one conversation's whole `email-draft`
    pipeline — and its passes are that child's stage executions in its own order.
    `journal_sequence` is what recovers that (STU-1112), and reading it here rather than
    walking the file again is the point: a `mailbox-full` journal is one file every
    conversation writes into at once, so file order is true of the file and false of any
    thread, and there is one statement of how a journal is read rather than two.

    The source message comes from the `drafts-pre` items when the journal carries them,
    and from the run's own input when it does not — a `draft-bakeoff` journal is one
    conversation and states it there.

    A `draft-qa-erosion` or `draft-qa-replay` journal is not an input, and not because it
    cannot be parsed: its drafting stage is an echo of a recorded draft, so the body it
    would hand the reader is one no group ever wrote toward. Those raise here rather than
    being read, which is the honest answer for a journal that has no shipped body.
    """
    events = list(_events(lines))
    if any(e.get("event") == "stage_complete" and e.get("stage") == _ECHO_STAGE
           for e in events):
        raise EchoJournal(
            "this is a draft-qa replay or erosion journal: its drafting stage is "
            f"`{_ECHO_STAGE}`, an echo of a recorded draft, so no body was ever "
            "written toward the reviewer's feedback here"
        )

    items: dict[str, dict] = {}
    root_input: dict = {}
    for event in events:
        if event.get("event") == "pipeline_start" and not root_input:
            root_input = event.get("input") or {}
        elif (event.get("event") == "stage_complete"
              and event.get("stage") == _ITEM_STAGE
              and isinstance(event.get("output"), dict)):
            items = {i["thread_id"]: i for i in event["output"].get("pending", [])}

    found, unplaced = journal_sequence.conversations(events)
    if unplaced:
        where = ", ".join(f"line {e.position} {e.stage}" for e in unplaced)
        raise Unattributable(
            f"{len(unplaced)} drafting stage execution(s) in this journal tie to no "
            f"conversation ({where}), so some group's passes are short by that much and "
            "the journal does not say whose. A group short of a pass can report the "
            "wrong shipped body, so none of them are read."
        )

    groups = []
    for conversation in found:
        passes = _passes(conversation)
        if not passes:
            # Every attempt this conversation made failed, so it drafted no body and
            # named no thread. Nothing to read it against, and nothing shipped.
            continue
        if items and conversation.thread_id not in items:
            # `qa_bakeoff` raises the same way. Falling back to the run's own input hands
            # the reader an empty `body_text`, and a message that asked nothing reads back
            # `covered` — a false clear, the one direction this tool must not fail in.
            raise OrphanGroup(
                f"drafted thread {conversation.thread_id} has no {_ITEM_STAGE} item "
                "in the journal"
            )
        groups.append(Group(
            conversation.thread_id,
            items.get(conversation.thread_id, root_input),
            passes,
        ))
    return groups


@dataclass(frozen=True)
class Read:
    """One coverage call on one group's shipped body."""

    repeat: int
    verdict: str
    unaddressed: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return self.verdict == INCOMPLETE

    @property
    def unreadable(self) -> bool:
        """Not a measurement, whichever way it failed to be one."""
        return self.verdict in NOT_A_READ


def list_field(output: dict, key: str) -> list | None:
    """A reader's list field, whether it arrived as a list or as a JSON string.

    Measured on the STU-1140 corpus, 2026-08-30: the same prompt returns a real array on
    one call and the array serialised into a string on the next, roughly half and half.
    A `list`-only reader dropped those as unreadable — a coherent verdict discarded, and
    a sweep quietly measuring half of what it ran.

    The contract cannot close this: `required_fields` checks presence, not shape. So it
    is decoded once, here, where the output is already being checked — and a string that
    is not a JSON array is still not a list, which is the honest answer to `[object]`.
    """
    value = output.get(key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, list) else None


def read_of(output: dict, repeat: int) -> Read:
    """One reader output as a `Read`, or a non-verdict when it cannot be taken as one.

    `verdict` and `requests` say the same thing twice, so the two are checked against
    each other rather than one being preferred: a reader that lists an unaddressed
    request and calls the reply covered has not thought the question through, and taking
    either half of that would record a judgement nobody made. Measured, not hypothetical
    — this fires on real reads of the STU-1140 corpus.
    """
    requests = list_field(output, "requests")
    verdict = output.get("verdict")
    if requests is None or verdict not in (COVERED, INCOMPLETE):
        return Read(repeat, ERROR, ())
    unaddressed = tuple(
        str(r.get("request", "")) for r in requests
        if isinstance(r, dict) and not r.get("addressed")
    )
    if bool(unaddressed) != (verdict == INCOMPLETE):
        return Read(repeat, INCOHERENT, unaddressed)
    return Read(repeat, verdict, unaddressed)


@dataclass(frozen=True)
class GroupResult:
    """One group, and every coverage read taken of the body it shipped."""

    group: Group
    reads: tuple[Read, ...]

    @property
    def flags(self) -> int:
        return sum(1 for r in self.reads if r.flagged)

    @property
    def readable(self) -> int:
        return sum(1 for r in self.reads if not r.unreadable)

    @property
    def unreadable(self) -> int:
        return sum(1 for r in self.reads if r.unreadable)

    @property
    def unaddressed(self) -> tuple[str, ...]:
        """What the first flagging read called unaddressed.

        One read's phrasing rather than the union of all of them: the reader quotes her
        words back, so two reads of the same missing request differ by a comma and a
        union prints the same finding twice. Nothing is hidden by picking one — every
        read's own list is on its own line, and in the record.
        """
        for read in self.reads:
            if read.flagged:
                return read.unaddressed
        return ()

    @property
    def finding(self) -> str:
        """One line naming what this group measured.

        A lone flag is not one. The reader is a model and repeats disagree, so a single
        incomplete says it is a single read rather than claiming the body left a parent
        unanswered — the shape `qa_erosion` uses against the noise floor STU-1014
        measured for the reviewer.

        Judged over the reads that *landed*, never over how many were asked for: a dead
        call standing beside three agreeing flags is not evidence against them, and
        reporting the group unreadable there would hide the finding behind the weather.
        Only a group where nothing landed is unreadable. What was lost is named on every
        line rather than dropped, so a thin denominator is never mistaken for a whole one.

        That last sentence is what a lone flag beside a dead call has to obey too
        (STU-1188). Judging over what landed is right where several reads land and
        inverts where one does: with a denominator of one, taking the survivor at face
        value *is* reading a thin denominator as a whole one. So the single-read rule
        holds on the count of flags alone, and not on `readable > 1` as it did — a group
        whose reads came back `(error, incomplete)` published `UNANSWERED — flagged in
        1/1 read(s)`, while the same lone `incomplete` beside a `covered` was "not yet a
        finding". Same evidence, and what decided it was whether the other call happened
        to die, which is the weather rather than the body.

        The cost is named rather than discovered later: a group whose other read always
        dies can never reach a finding, at any `--repeats`, and a run asked for a single
        read cannot produce one at all. Nothing is hidden by that — the flag, what it
        called unaddressed, and every read are still on the line, in the table and in the
        record; what it does not do is claim a corroboration no second read gave. A miss
        that only one landed read ever saw is read by a human, not counted by this.
        """
        how = ", ".join(sorted({r.verdict for r in self.reads if r.verdict in NOT_A_READ}))
        lost = f" ({self.unreadable} not a verdict: {how})" if self.unreadable else ""
        if not self.readable:
            return (f"UNREADABLE — {self.unreadable}/{len(self.reads)} read(s) "
                    f"are not a verdict ({how})")
        if not self.flags:
            return f"answered in {self.readable}/{self.readable} read(s){lost}"
        asked = "; ".join(self.unaddressed) or "(none named)"
        if self.flags == 1:
            # Worded off the denominator so the two are never read as one another: a
            # flag that stood alone among reads that landed is a disagreement, a flag
            # that was the only read to land is a measurement nothing else was taken of.
            among = (f"one flag in {self.readable} reads" if self.readable > 1
                     else "the only read that landed flagged")
            return f"{among} — a single read, not yet a finding: {asked}{lost}"
        loop = (" — and the group converged by removal, so the loop is what dropped it"
                if self.group.converged_by_removal else "")
        return (f"UNANSWERED — flagged in {self.flags}/{self.readable} read(s)"
                f"{loop}: {asked}{lost}")

    @property
    def flagged(self) -> bool:
        return self.finding.startswith("UNANSWERED")


def render(results: list[GroupResult], provider: str | None,
           studio_version: str | None) -> str:
    """The table a human reads: one block per group, the sequence beside the verdict."""
    flagged = [r for r in results if r.flagged]
    lines = [
        f"=== Draft coverage: {len(flagged)}/{len(results)} shipped body(ies) left a "
        "request unanswered ==="
    ]
    for result in results:
        group = result.group
        lines.append(f"  {group.thread_id}")
        chars = " -> ".join(str(c) for c in group.answer_chars)
        removal = "  (converged by removal)" if group.converged_by_removal else ""
        lines.append(f"    passes: {len(group.passes)} | answer chars: {chars}{removal}")
        for read in result.reads:
            named = "; ".join(read.unaddressed) or "—"
            lines.append(f"    read {read.repeat}: {read.verdict} | unaddressed: {named}")
        lines.append(f"    {result.finding}")
        rejection = group.last_rejection
        if result.flagged and rejection is not None:
            lines.append("    the reviewer's last instruction before it approved:")
            for issue in rejection.issues:
                lines.append(f"      - {issue}")
    if flagged:
        lines.append(
            "A flag is a reply a parent has to write again. Read the instruction above "
            "it before deciding whether the loop or the drafter dropped the question."
        )
    lines.append(qa_provider.line(provider))
    lines.append(qa_studio_version.line(studio_version))
    return "\n".join(lines)


def as_dict(results: list[GroupResult], repeats: int, provider: str | None,
            studio_version: str | None) -> dict:
    return {
        qa_provider.KEY: provider,
        qa_studio_version.KEY: studio_version,
        "repeats": repeats,
        "groups": len(results),
        "flagged": sum(1 for r in results if r.flagged),
        "results": [
            {
                "thread_id": r.group.thread_id,
                "finding": r.finding,
                "flagged": r.flagged,
                "passes": len(r.group.passes),
                "answer_chars": list(r.group.answer_chars),
                "converged_by_removal": r.group.converged_by_removal,
                "unaddressed": list(r.unaddressed),
                "reads": [
                    {"repeat": x.repeat, "verdict": x.verdict,
                     "unaddressed": list(x.unaddressed)}
                    for x in r.reads
                ],
            }
            for r in results
        ],
    }
