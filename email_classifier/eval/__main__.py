"""Eval CLI: `run` replays a corpus against golden labels (STU-816), `seed` freezes a
reviewed run report into a golden file, `parity` diffs the Gmail projection against the
Graph one (STU-848), `spans` diffs the detected person spans against the golden set
(STU-925), `ab` diffs an anonymized arm against a cleartext one (STU-837).

`run` exits 1 on any flip so a rules.yaml edit that changes a golden verdict fails loud;
it makes no mailbox access and no LLM call (stage-2 verdicts only ever come from
`--stage2-cache`). `ab` is the one subcommand that does call the LLM, and exits 1 on any
stable divergence.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from email_classifier import deployment
from email_classifier.core.contract import load_taxonomy
from email_classifier.eval import ab, parity, spans
from email_classifier.eval.harness import (
    conversation_labels,
    evaluate,
    labels_from_report,
    load_corpus,
    load_golden,
    render,
    replay,
    write_golden,
)
from email_classifier.orchestrate.stage2_client import StudioCliClassifier
from email_classifier.stage1.policy import ANONYMIZATION_SCOPES, load_policy
from email_classifier.stage1.rule_engine import load_rules

REPO_ROOT = deployment.ROOT


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m email_classifier.eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Replay a corpus through stage 1 and diff against golden.")
    run.add_argument("--corpus", type=Path, default=REPO_ROOT / "fixtures",
                     help="Directory of single-email JSON files, or one JSON array dump.")
    run.add_argument("--golden", type=Path, default=REPO_ROOT / "eval" / "golden.json")
    run.add_argument("--stage2-cache", type=Path,
                     help="Run report supplying cached LLM verdicts for escalated messages.")
    run.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    run.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")
    run.add_argument("--taxonomy", type=Path, default=REPO_ROOT / "rules" / "taxonomy.yaml")

    seed = sub.add_parser("seed", help="Write golden labels from a reviewed run report, or "
                                       "from a stage-1 replay of a corpus.")
    source = seed.add_mutually_exclusive_group(required=True)
    source.add_argument("--report", type=Path, help="Reviewed run report.")
    source.add_argument("--corpus", type=Path, help="Corpus to replay through stage 1.")
    seed.add_argument("--out", type=Path, default=REPO_ROOT / "eval" / "golden.json")
    seed.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    seed.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")

    parity_cmd = sub.add_parser(
        "parity", help="Round-trip a corpus through both provider projections and diff them."
    )
    parity_cmd.add_argument("--corpus", type=Path, default=REPO_ROOT / "fixtures",
                            help="Directory of single-email JSON files, or one JSON array dump.")
    parity_cmd.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    parity_cmd.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")

    spans_cmd = sub.add_parser(
        "spans", help="Diff the person spans the detector finds in a corpus against the "
                      "golden span set, or re-freeze it with --seed."
    )
    spans_cmd.add_argument("--corpus", type=Path, default=REPO_ROOT / "fixtures",
                           help="Directory of single-email JSON files, or one JSON array dump.")
    spans_cmd.add_argument("--golden", type=Path,
                           default=REPO_ROOT / "eval" / "ner-spans.json")
    spans_cmd.add_argument("--seed", action="store_true",
                           help="Write what the detector finds now to --golden instead of "
                                "diffing against it.")

    ab_cmd = sub.add_parser("ab", help="Classify the escalated subset of a corpus under an "
                                       "anonymized and a cleartext policy, and diff them.")
    ab_cmd.add_argument("--corpus", type=Path, default=REPO_ROOT / "fixtures",
                        help="Directory of single-email JSON files, or one JSON array dump.")
    ab_cmd.add_argument("--scope", choices=sorted(ANONYMIZATION_SCOPES),
                        help="Anonymization scope of the tokenized arm "
                             "(default: the policy file's own scope).")
    ab_cmd.add_argument("--repeats", type=int, default=2,
                        help="Runs per arm; a message must agree with itself across all of "
                             "them to be compared at all.")
    ab_cmd.add_argument("--provider", default=None,
                        help="Override the Studio provider the agents name themselves "
                             "(claude-code|anthropic|mock).")
    ab_cmd.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    ab_cmd.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")
    ab_cmd.add_argument("--taxonomy", type=Path, default=REPO_ROOT / "rules" / "taxonomy.yaml")

    return parser.parse_args(argv)


def _run(args: argparse.Namespace) -> int:
    rules = load_rules(args.rules)
    policy = load_policy(args.policy)
    emails = load_corpus(args.corpus)
    golden = load_golden(args.golden)
    cache = (
        labels_from_report(json.loads(args.stage2_cache.read_text(encoding="utf-8")))
        if args.stage2_cache
        else None
    )
    taxonomy = load_taxonomy(args.taxonomy)
    result = evaluate(replay(emails, rules, policy, cache), golden, emails, taxonomy)
    print(render(result))
    return 1 if result.flips else 0


def _seed(args: argparse.Namespace) -> int:
    if args.report:
        labels = labels_from_report(json.loads(args.report.read_text(encoding="utf-8")))
    else:
        labels = replay(
            load_corpus(args.corpus), load_rules(args.rules), load_policy(args.policy)
        )
    if args.out.exists():
        # Conversation labels are reviewed by hand and no replay produces them.
        labels |= conversation_labels(load_golden(args.out))
    write_golden(args.out, labels)
    print(f"{len(labels)} golden label(s) written to {args.out}", file=sys.stderr)
    return 0


def _parity(args: argparse.Namespace) -> int:
    result = parity.check(
        load_corpus(args.corpus), load_rules(args.rules), load_policy(args.policy)
    )
    print(parity.render(result))
    return 1 if result.divergences else 0


def _spans(args: argparse.Namespace) -> int:
    detected = spans.detect_all(load_corpus(args.corpus))
    if args.seed:
        spans.write_golden(args.golden, detected)
        total = sum(len(s) for fields in detected.values() for s in fields.values())
        print(f"{total} person span(s) written to {args.golden}", file=sys.stderr)
        return 0
    result = spans.compare(detected, spans.load_golden(args.golden))
    print(spans.render(result))
    return 1 if result.divergences or result.uncovered or result.absent else 0


def _ab(args: argparse.Namespace, factory: ab.ClassifierFactory | None = None) -> int:
    if args.repeats < 2:
        print("[ERROR] --repeats must be >= 2: one run per arm cannot tell a stable "
              "divergence from model noise", file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    policy = load_policy(args.policy)
    fields = ANONYMIZATION_SCOPES[args.scope] if args.scope else policy.anonymization_fields
    if not fields:
        print(f"[ERROR] anonymization is off in {args.policy} — pass --scope to pick the arm "
              "to test", file=sys.stderr)
        return 1

    conversations = ab.escalations(load_corpus(args.corpus), load_rules(args.rules), policy)
    if not conversations:
        print("[ERROR] no message in the corpus escalates: nothing reaches the LLM to A/B",
              file=sys.stderr)
        return 1
    taxonomy = load_taxonomy(args.taxonomy)
    factory = factory or (
        lambda f: StudioCliClassifier(
            taxonomy=taxonomy,
            provider=args.provider, cwd=REPO_ROOT, anonymize_fields=f,
            # No mail is waiting on an A/B over committed fixtures; the arm's `errored`
            # list is where a provider refusal belongs.
            alert_on_provider_failure=False,
        )
    )

    message_ids = [mid for c in conversations for mid in c.message_ids]
    cleartext = ab.run_arm("cleartext", [], conversations, factory, args.repeats)
    anonymized = ab.run_arm("anonymized", fields, conversations, factory, args.repeats)
    result = ab.compare(cleartext, anonymized, message_ids)
    print(ab.render(result))
    return 1 if result.divergences else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "seed":
        return _seed(args)
    if args.command == "parity":
        return _parity(args)
    if args.command == "spans":
        return _spans(args)
    return _ab(args) if args.command == "ab" else _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
