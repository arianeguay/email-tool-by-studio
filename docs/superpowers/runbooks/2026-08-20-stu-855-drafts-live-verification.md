# Verifying the drafts behaviours on a live mailbox (STU-855)

Four `drafts` behaviours shipped in STU-850 with unit tests over a fake Graph session
and no live evidence. This runbook is how each one was forced against
`seed@example.com` and a real model, so the next person can reproduce it rather
than re-derive it.

Everything here needs `secrets/`, a consented `Mail.ReadWrite` grant and a live-LLM
provider — it is `claude:local` work by definition.

## Setup

```bash
export OUTLOOK_SEED_MAILBOX=seed@example.com   # the seeder refuses without it
make seed COUNT=24                                  # the fixtures-club corpus
make mailbox-full MAX=24 DUMP=dumps/stu855-seeded.json
```

`MAX=24` is what restricts the run to the seeded mail: the seeded messages are the
newest in the mailbox, and `fetch` orders by `receivedDateTime desc`. Use a dump path
of your own — a shared one gets overwritten by whoever else is working the repo.

## Probing one email instead of the whole mailbox

Three of the four behaviours are properties of a single fan-out item, and running
`mailbox-full` for each costs seven minutes. Run the leaf pipeline directly instead:

```bash
studio run email-draft --input-file <item>.yaml --provider claude-code
```

The input file is one fan-out item — the fields `drafts-pre` puts in `pending`
(`message_id`, `thread_id`, `from_address`, `subject`, `date`, `body_text`,
`prompt_fingerprint`, `keymap`, `journal`). Build it with
`email_classifier.orchestrate.drafts.draft_item()` over a message read straight from
the Inbox; the report-driven path needs a classification run the probe does not.

## What each behaviour needs to be forced

| Behaviour | How to force it |
|---|---|
| Real drafts from `mailbox-full` | Nothing — run it and read `logs/drafts/journal.jsonl` against the Drafts folder |
| QA rejection re-runs the group | A drafter prompt that invents facts *only while `group_feedback` is absent* |
| Anti-theatre | A drafter prompt told not to call `mail_drafts-create_draft`, while the tool stays declared |
| Nothing sent | Nothing — count Sent Items before and after |

The QA probe has to switch itself off once feedback arrives, or the group burns all
three iterations on a prompt that cannot converge and the corrected draft never lands.

The anti-theatre probe must keep the tool **declared** and only forbid calling it.
Removing it from the agent's `tools:` makes the model return prose instead of contract
fields, and the stage then fails on `Expected object output, got string` — the output
gate, not the tool-call gate. With the tool declared and uncalled, the failure is the
one this behaviour is about:

```
✗ retry (Expected at least 1 successful tool call, got 0 successful)
```

A tool that fails for real (point the item at a message id that does not resolve) is a
different test: it proves the stage does not report success on a Graph error, and it
costs `max_attempts × max_iterations` LLM calls per item because RALPH re-asks the
model to fix something it cannot (STU-864).

## Reading the evidence

`.studio/runs/<timestamp>-<pipeline>-<id>.jsonl` is the trace. `group_iteration`
carries the iteration number, `stage_context` lists which context keys the stage
received — `group_feedback` appearing on iteration 2 is what proves the critique was
carried — and `stage_complete` holds each stage's output.

The mailbox is the other half, and it is the one that cannot be faked:

```bash
# one draft per conversation, and its id matches the journal
GET /me/mailFolders/drafts/messages?$filter=conversationId eq '<thread_id>'
```

Two journal lines with the same `draft_id` mean the draft was rewritten, not
duplicated — that is `upsert_reply_draft` finding the existing draft on the
conversation.

## Do not

- Do not run `make unseed` while someone else is mid-run: it deletes the seeded
  messages and, since STU-885, the reply draft sitting on each of their
  conversations. A draft whose seeded parent was already gone before the teardown
  is not found by it — there is no marked message left to reach the conversation
  from — so orphans from an earlier cycle still need clearing by hand.
- Do not point a probe at the production mailbox. The seeder refuses, but
  `email-draft` does not — it drafts into whatever mailbox the token holds.
- Do not leave a probe in `.studio/agents/`. Every probe here is a temporary edit,
  reverted before the run that produces the final evidence.
