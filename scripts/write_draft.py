#!/usr/bin/env python3
"""Write stage of `email-draft` (STU-1030): put the composed reply in the mailbox.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

The drafting agent used to hold a `mail_drafts-create_draft` tool and call it
itself. It called it on every revision: 28 mailbox writes for 5 `generate-draft`
attempts on run 2498b0b3, each one a Graph write and a journal line, and nothing
in the contract could cap a single tool. A prompt saying "exactly once" is not a
mechanism. The write happens here instead, once per attempt, on the body the
stage before this one settled on — the agent composes, the run writes.

Which message is replied to is a property of the run, so everything but the body
comes from the fan-out item, never from the model.

De-tokenization happens here, at the boundary. Under `scope: full` the agent
works on anonymized mail and writes `PERSON_1` where the parent's name goes; the
keymap restores it on the way out, so the LLM never sees the name and the parent
never sees the token. The same restore applies to `reply_to`/`reply_name`, which
are tokenized for exactly that reason (STU-888).

Each draft is journaled as it is created, by the code that actually called Graph.
That line, not the agent's claim, is what `drafts-post` counts — and it names the
run that wrote it, since the journal is shared and message ids alone hand one
run's drafts to another (STU-917).

A draft already on the conversation is rewritten only when this pipeline wrote it and
nobody has touched it since (STU-1548). `drafts-pre` asks the same question at
selection, but a human can start a draft between that and this write: on run
`4ed439fb` the reviewer's own forward of the parent's message was overwritten 29
seconds before they sent it. Anything else is journaled `held` and the stage fails,
so the conversation is left to the human and `drafts-post` counts it held.

The commitments the body makes (a forward it claims, a follow-up it promises) go to
`logs/drafts/commitments.jsonl` beside the journal, from the same body (STU-1065).

Output: the drafting stage's output plus the `draft_id` the mailbox gave back,
which is the shape `draft-qa` reads as its `previous_stage_output`. Its
`draft_body` stays tokenized — the reviewer reads what the drafter wrote, and the
real names live only in the mailbox.
"""

import json
import sys
from pathlib import Path

import yaml

from email_classifier import commitments
from email_classifier.anonymize import deanonymize, load_keymap
from email_classifier.apply.draft_journal import record
from email_classifier.apply.journal import read_entries
from email_classifier.orchestrate.drafts import edited_since, our_draft
from email_classifier.providers import get_drafter, preflight_write
from email_classifier.stage1.policy import load_policy

_COMPOSE_STAGE = "generate-draft"


def main() -> None:
    payload = json.load(sys.stdin)
    item = yaml.safe_load(payload.get("additional_context", "") or "") or {}
    draft = (payload.get("previous_outputs") or {})[_COMPOSE_STAGE]

    body = (draft.get("draft_body") or "").strip()
    if not body:
        sys.exit("[write-draft] refusing to create an empty draft")

    policy = load_policy(Path(item["policy"]))
    refusal = preflight_write(policy.email_provider)
    if refusal:
        sys.exit(f"[write-draft] {refusal}")

    keymap = load_keymap(Path(item["keymap"]))
    journal = Path(item["journal"])
    run_id = item["run_id"] or None
    message_id, thread_id = item["message_id"], item["thread_id"]
    try:
        drafter = get_drafter(policy.email_provider)
        standing = _humans_draft(drafter, thread_id, journal)
        if standing:
            record(journal, run_id, message_id, thread_id, "held", draft_id=standing)
            sys.exit(f"[write-draft] {thread_id} holds a draft this pipeline did not "
                     f"write or that was edited since ({standing}); leaving it to a human")
        result = drafter.upsert_reply_draft(
            message_id,
            thread_id,
            deanonymize(body, keymap),
            deanonymize(item["reply_to"], keymap) or None,
            deanonymize(item["reply_name"], keymap) or None,
        )
    except Exception as exc:  # journal the miss, then fail the stage
        record(journal, run_id, message_id, thread_id, "error", error=str(exc))
        raise

    # The prompt this body was written under (STU-886): the next run rewrites a draft
    # of ours whose fingerprint is no longer today's, so the row that made it is the
    # only place that answer can come from.
    record(journal, run_id, message_id, thread_id, "ok",
           draft_id=result["draft_id"], updated=result["updated"],
           prompt_fingerprint=item["prompt_fingerprint"])
    # Here and not in `drafts-post`, so a run that dies after this write still leaves
    # the promise listed (STU-1065, on STU-917's reasoning).
    commitments.record_draft(commitments.path_beside(journal), run_id, thread_id,
                             result["draft_id"], body)
    print(f"[write-draft] {thread_id} -> {result['draft_id']}", file=sys.stderr)
    json.dump({**draft, "draft_id": result["draft_id"]}, sys.stdout, ensure_ascii=False)


def _humans_draft(drafter, thread_id: str, journal: Path) -> str | None:
    """Id of the conversation's draft when it is not ours to rewrite, else None."""
    draft_id = drafter.find_draft(thread_id)
    if draft_id is None:
        return None
    ours = our_draft(read_entries(journal) if journal.exists() else [], draft_id)
    if ours is None:
        return draft_id
    draft = drafter.get_draft(draft_id)
    if draft is not None and edited_since(draft["lastModifiedDateTime"], ours["timestamp"]):
        return draft_id
    return None


if __name__ == "__main__":
    main()
