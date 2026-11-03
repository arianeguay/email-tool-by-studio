#!/usr/bin/env python3
"""Seed the Outlook test mailbox with synthetic mail, and tear it down (STU-845).

    python scripts/seed_outlook.py seed --count 50 --mix "bucket_a=40,bucket_c=60"
    python scripts/seed_outlook.py seed --corpus fixtures-threads --threads
    python scripts/seed_outlook.py seed-reply --message <mailbox id>
    python scripts/seed_outlook.py export-review
    python scripts/seed_outlook.py teardown
    python scripts/seed_outlook.py teardown --run 6f1c...

`--threads` reads the corpus as conversations instead of buckets and lays each one
down as a real reply chain, so a fixture thread groups in the mailbox the way the
fixture says it does (STU-942). Without it the same messages land as unrelated mail.

A round draws the whole corpus unless `--count` asks for a smaller one, and refuses a
mailbox that already holds a seeded round: stacking duplicates every subject and leaves
the golden describing neither round. `--add` opts into stacking, and merges both
artifacts instead of replacing them (STU-893).

Every message is drawn from the deployment's `fixtures/` unless `--corpus` names another
corpus, synthetic by construction, so no real address, subject or body is ever read or
reproduced. Each one is marked with the
`x-email-classifier-seed` header and the `email-classifier-seed` category, and
`teardown` deletes exactly the messages carrying that header.

After seeding, the messages are read back through the ordinary fetch path and
replayed through stage 1. That read-back is frozen twice: as a corpus dump, and
as golden labels keyed by mailbox id — so the eval harness knows the expected
verdict of every seeded message, as it actually landed rather than as the fixture
wished. `make eval-seeded` diffs one against the other (STU-848).

Both files describe the seeded mailbox and expire with it: mailbox ids are new on
every round, and a teardown invalidates them.

A human reviews the round by moving messages into folders, and that folder
membership is the only place the verdict exists. `export-review` reads it back,
naming the fixture each message came from; `teardown` exports before it deletes
anything, and refuses outright when a message sits outside the Inbox unless
`--force` says the review has been read (STU-875).

`seed-reply` answers one seeded message from the mailbox itself, stamped now
(STU-964). It is the only way to stage a superseded draft — a draft a human
replied past — under a `Mail.ReadWrite` grant: the turn is written into Sent
Items rather than sent, and chained so the store files it on the draft's
conversation. Run it after a drafts run, never before: what makes the draft
stale is a reply dated later than it.

`teardown --run` tears down one round by its seed run id, leaving the others
standing — `--add` makes a mailbox holding several rounds a supported state, so a
sweep is not the only way back out (STU-966). The refusal applies to the scope, not
to the mailbox: another round filed out of the Inbox does not block it.

Whatever it deletes, a teardown prunes the golden and the dump to the messages it
left standing (STU-977) — the rule `--add` already applies when it merges them,
applied on the delete side, so both artifacts go on describing the mailbox as it is.

Writing needs `Mail.ReadWrite`, consented once with `make outlook-consent`; a
read-only cache is refused before the first message, not partway through.
OUTLOOK_SEED_MAILBOX must name the test mailbox or the seeder refuses to write.
"""

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from email_classifier.core import schema
from email_classifier.core.types import EmailInput
from email_classifier.eval.harness import load_golden, replay, write_golden
from email_classifier.fetch.outlook import OutlookFetcher
from email_classifier.seed import corpus, outlook
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import load_rules
from email_classifier import deployment

REPO_ROOT = deployment.ROOT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed", help="Send synthetic messages to the test mailbox.")
    seed.add_argument("--count", type=int, default=None,
                      help="Messages to seed (default: the whole corpus).")
    seed.add_argument("--add", action="store_true",
                      help="Stack this round onto the messages a previous one left in "
                           "the mailbox, merging the golden and the dump instead of "
                           "replacing them. Without it, a non-empty mailbox is refused.")
    seed.add_argument("--mix", default="",
                      help="Bucket weights, e.g. 'bucket_a=40,bucket_c=60'. "
                           "Default: the corpus's own proportions.")
    seed.add_argument("--corpus", type=Path, default=REPO_ROOT / "fixtures",
                      help="Corpus to draw from (default: the deployment's fixtures/).")
    seed.add_argument("--threads", action="store_true",
                      help="Read --corpus as conversations and lay each one down as a "
                           "real reply chain, instead of drawing a bucket mix of "
                           "independent messages. --count limits the conversations, "
                           "not the messages; --mix does not apply.")
    seed.add_argument("--golden", type=Path, default=REPO_ROOT / "eval" / "seeded-golden.json")
    seed.add_argument("--dump", type=Path,
                      default=REPO_ROOT / "dumps" / "outlook-seeded.json",
                      help="Where to freeze the read-back as the eval corpus.")
    seed.add_argument("--random-seed", type=int, default=0,
                      help="Draw seed — same value draws the same mix (default: 0).")
    seed.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    seed.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")

    reply = commands.add_parser(
        "seed-reply", help="Answer a seeded message from the mailbox, stamped now.")
    reply.add_argument("--message", required=True,
                       help="Mailbox id of the seeded message the turn answers.")

    review_dump = REPO_ROOT / "dumps" / "outlook-review.json"
    export = commands.add_parser(
        "export-review", help="Record which folder a human left each seeded message in.")
    export.add_argument("--review-dump", type=Path, default=review_dump,
                        help="Where to write the folder membership.")

    teardown = commands.add_parser(
        "teardown", help="Delete every seeded message from the test mailbox.")
    teardown.add_argument("--review-dump", type=Path, default=review_dump,
                          help="Where the review is exported before anything is deleted.")
    teardown.add_argument("--force", action="store_true",
                          help="Delete even the messages a human filed out of the Inbox.")
    teardown.add_argument("--run", default=None,
                          help="Tear down only the round with this seed run id, leaving "
                               "every other round standing. Default: every seeded round.")
    teardown.add_argument("--golden", type=Path, default=REPO_ROOT / "eval" / "seeded-golden.json",
                          help="Golden labels to prune to what the teardown leaves standing.")
    teardown.add_argument("--dump", type=Path,
                          default=REPO_ROOT / "dumps" / "outlook-seeded.json",
                          help="Corpus dump to prune to what the teardown leaves standing.")
    return parser.parse_args(argv)


def write_dump(path: Path, inputs: list[dict]) -> None:
    """Freeze the read-back as the eval corpus, alongside the golden labels (STU-848).

    Written from the ids the seeder just wrote, never from a mailbox listing:
    `/me/messages` spans every folder, so a listing would also pick up whatever
    another round left standing outside the Inbox.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")


def _standing_golden(path: Path, standing: set[str]) -> dict:
    """The golden labels of the messages the mailbox still holds.

    Entries of a round already torn down are dropped: both artifacts describe the
    mailbox as it is now, which is the only state the eval replay can score.
    """
    return {mid: label for mid, label in load_golden(path).items() if mid in standing}


def _standing_dump(path: Path, standing: set[str]) -> list[dict]:
    previous = json.loads(path.read_text(encoding="utf-8"))
    return [item for item in previous if item["message_id"] in standing]


def _merge_golden(path: Path, standing: set[str], labels: dict) -> dict:
    """This round's labels over those of the messages still standing in the mailbox."""
    if not path.exists():
        return labels
    return _standing_golden(path, standing) | labels


def _merge_dump(path: Path, standing: set[str], inputs: list[dict]) -> list[dict]:
    if not path.exists():
        return inputs
    return _standing_dump(path, standing) + inputs


def _prune_artifacts(golden: Path, dump: Path, standing: set[str]) -> list[Path]:
    """Drop what a teardown deleted from both artifacts; return the ones rewritten.

    The rule the merges apply on the way in, applied on the way out (STU-977).
    An unscoped teardown left the golden wholly stale, and wholly stale reads as
    stale; a scoped one leaves it partly stale, which reads as live —
    `make eval-seeded` replays the frozen dump, scores the entries of the deleted
    round against inputs frozen before the delete, passes, and reports a message
    count the mailbox has not had since.
    """
    pruned = []
    if golden.exists():
        write_golden(golden, _standing_golden(golden, standing))
        pruned.append(golden)
    if dump.exists():
        write_dump(dump, _standing_dump(dump, standing))
        pruned.append(dump)
    return pruned


Round = tuple[list[tuple[str, EmailInput]], list[list[EmailInput]]]


def _draw(args: argparse.Namespace) -> Round:
    """The round to write: every (group, message) pair, and the chains to write it in.

    A flat round is one chain per message — nothing to thread. A thread round
    keeps each conversation whole, in arrival order, because that order is what
    `seed_thread` chains the replies along.
    """
    if args.threads:
        if args.mix:
            raise SystemExit("--mix does not apply to --threads: a conversation is "
                             "seeded whole, so there is nothing to weight.")
        threads = corpus.load_threads(args.corpus)[: args.count]
        return [(m.thread_id, m) for thread in threads for m in thread], threads

    buckets = corpus.load_buckets(args.corpus)
    count = args.count if args.count is not None else sum(map(len, buckets.values()))
    drawn = corpus.select(buckets, count, corpus.parse_mix(args.mix), seed=args.random_seed)
    return drawn, [[email] for _, email in drawn]


def _seed(args: argparse.Namespace) -> int:
    drawn, chains = _draw(args)

    mailbox = outlook.assert_test_mailbox()  # raises loudly on the wrong mailbox
    session = outlook.build_session()
    standing = set(outlook.find_standing(session))
    if standing and not args.add:
        raise SystemExit(
            f"{len(standing)} seeded message(s) already in {mailbox} — a second round "
            "duplicates them and leaves the golden describing neither. Run `make unseed` "
            "first, or pass --add to stack onto them."
        )

    run_id = str(uuid.uuid4())
    if args.threads:
        message_ids = [
            message_id
            for chain in chains
            for message_id in outlook.seed_thread(session, chain, run_id, mailbox)
        ]
    else:
        message_ids = outlook.seed(session, [email for _, email in drawn], run_id, mailbox)

    fetched = OutlookFetcher(session).fetch_inputs_by_ids(message_ids)
    landed = [schema.validate(item) for item in fetched]
    labels = replay(landed, load_rules(args.rules), load_policy(args.policy))
    if args.add:
        labels = _merge_golden(args.golden, standing, labels)
        fetched = _merge_dump(args.dump, standing, fetched)
    write_golden(args.golden, labels)
    write_dump(args.dump, fetched)

    mix = Counter(group for group, _ in drawn)
    print(f"Seeded {len(message_ids)} message(s) into {mailbox} (seed run {run_id})")
    if args.threads:
        print(f"  chained into {len(chains)} conversation(s)")
    if args.add:
        print(f"  stacked onto {len(standing)} standing message(s) — "
              f"{len(fetched)} described in all")
    print("  mix: " + ", ".join(f"{group}={n}" for group, n in sorted(mix.items())))
    print(f"  golden labels: {args.golden}")
    print(f"  corpus dump:   {args.dump}")

    diverged = [
        (email, fields)
        for (_, email), actual in zip(drawn, landed)
        if (fields := outlook.divergence(email, actual))
    ]
    for email, fields in diverged:
        print(f"  [WARN] {email.message_id}: mailbox dropped {', '.join(fields)}",
              file=sys.stderr)
    if diverged:
        print(f"  [WARN] {len(diverged)} message(s) did not land verbatim — the golden "
              "labels above describe what landed, not the fixture.", file=sys.stderr)
    return 0


def _seed_reply(args: argparse.Namespace) -> int:
    mailbox = outlook.assert_test_mailbox()
    session = outlook.build_session()
    sent_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    message_id, run_id = outlook.seed_sent_reply(session, args.message, mailbox, sent_at)
    print(f"Answered {args.message} from {mailbox} at {sent_at} (seed run {run_id})")
    print(f"  sent turn: {message_id}")
    return 0


def write_review(path: Path, mailbox: str, records: list[dict]) -> None:
    """Freeze the folder a human left each seeded message in (STU-875).

    Expires with its round, like the corpus dump: the mailbox ids are new on
    every seeding, and only `fixture` still means something afterwards.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"mailbox": mailbox, "messages": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _export_review(session, mailbox: str, seeded: list, path: Path) -> list[dict]:
    records = outlook.review(session, seeded)
    write_review(path, mailbox, records)
    filed = Counter(record["folder"] for record in records)
    print(f"Exported {len(records)} seeded message(s) to {path}")
    print("  folders: " + ", ".join(f"{name}={n}" for name, n in sorted(filed.items())))
    return records


def _warn_unmarked(skipped: list[str]) -> None:
    for message_id in skipped:
        print(f"  [WARN] {message_id} wears the seed category without the seed header "
              "— left in place.", file=sys.stderr)


def _review(args: argparse.Namespace) -> int:
    mailbox = outlook.assert_test_mailbox()
    session = outlook.build_session()
    seeded, skipped = outlook.find_seeded(session)

    _export_review(session, mailbox, seeded, args.review_dump)
    _warn_unmarked(skipped)
    return 0


def _scope(seeded: list[outlook.Seeded], run_id: str | None) -> list[outlook.Seeded]:
    """The round this teardown deletes — one seed run, or every seeded round.

    An unknown run id is refused rather than deleting nothing: a typo that sweeps
    no message reads exactly like a round that came down.
    """
    if run_id is None:
        return seeded
    scoped = [message for message in seeded if message.run_id == run_id]
    if not scoped:
        standing = ", ".join(sorted({message.run_id for message in seeded})) or "none"
        raise SystemExit(f"No seeded message carries run {run_id} — standing: {standing}")
    return scoped


def _teardown(args: argparse.Namespace) -> int:
    mailbox = outlook.assert_test_mailbox()
    session = outlook.build_session()
    seeded, skipped = outlook.find_seeded(session)

    # Every round, even under --run: the review dump is the mailbox as it is now,
    # and a scoped export would clobber the record of the rounds left standing.
    _export_review(session, mailbox, seeded, args.review_dump)
    scoped = _scope(seeded, args.run)
    moved = outlook.moved_out(scoped, outlook.seed_folder_ids(session))
    if moved and not args.force:
        run = f" RUN={args.run}" if args.run else ""
        print(f"Refusing to delete: {len(moved)} seeded message(s) sit outside the Inbox, "
              f"where a human filed them. The review is in {args.review_dump} — read it, "
              f"then re-run with `make unseed-force{run}`.", file=sys.stderr)
        return 1

    deleted = outlook.teardown(session, scoped)
    standing = {message.message_id for message in seeded} - set(deleted)
    round_of = f" (seed run {args.run})" if args.run else ""
    print(f"Deleted {len(deleted)} seeded message(s) from {mailbox}{round_of}")
    for path in _prune_artifacts(args.golden, args.dump, standing):
        print(f"  pruned {path} to the {len(standing)} message(s) still standing")
    _warn_unmarked(skipped)
    return 0


_COMMANDS = {"seed": _seed, "seed-reply": _seed_reply,
             "export-review": _review, "teardown": _teardown}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
