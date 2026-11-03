#!/usr/bin/env python3
"""Score two classifier models on the deployment's escalated fixtures (STU-1045).

Runs the stage-2 classification the way a run does — one `studio run classify` per
conversation, through `StudioCliClassifier` — once per model per repeat, and scores each
model against the deployment's reviewed golden in `eval/stage2-golden.json`.

    scripts/classify_bakeoff.py --baseline claude-opus-4-8 --candidate claude-haiku-4-5

Cleartext, both arms: the corpus is committed synthetic fixtures carrying no real PII,
whether tokenizing changes a verdict is `make eval-ab`'s question, and requiring the `ner`
extra would put a 1.76 GB detector between two models being compared to each other.

Exits non-zero when the candidate agrees with the golden less often than the baseline
does on the headline axis — that being the decision the harness exists to make. Two
models that tie exit 0: a cheaper model that holds the line is the trade this licenses.
It also exits non-zero when the baseline settled nothing of its own, which is a pass that
measured nothing rather than one that found the candidate wanting.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from email_classifier import classify_bakeoff
from email_classifier.core.contract import load_taxonomy
from email_classifier.eval import ab
from email_classifier.eval.harness import load_corpus
from email_classifier.orchestrate.stage2_client import StudioCliClassifier
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import load_rules
from email_classifier import deployment

REPO_ROOT = deployment.ROOT

_OUT = Path("logs/classify-bakeoff.json")


def default_out(limit: int | None) -> Path:
    """A limited pass never lands on the full bake-off's name (STU-1042)."""
    return _OUT if limit is None else _OUT.with_stem(f"{_OUT.stem}-limit{limit}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=REPO_ROOT / "fixtures")
    parser.add_argument("--golden", type=Path,
                        default=REPO_ROOT / "eval" / "stage2-golden.json")
    parser.add_argument("--baseline", default="claude-opus-4-8")
    parser.add_argument("--candidate", default="claude-haiku-4-5")
    parser.add_argument("--repeats", type=int, default=2,
                        help="Runs per arm. Under 2 there is no noise floor to read a "
                             "gap between the models against.")
    parser.add_argument("--limit", type=int, help="Score only the first N conversations.")
    # Explicit since STU-1133 removed the classifier's own `provider:`, which is what
    # used to put this harness on `anthropic`. Left inheriting, it would have followed
    # `defaults.provider` to `claude-code` without a word, and STU-1049's numbers
    # (opus 26/26, haiku 21/26) were taken on the API. Whether a bake-off should track
    # production's provider instead is its own question, and needs its own measurement.
    parser.add_argument("--provider", default="anthropic",
                        help="Studio provider both arms run under. Empty to fall back "
                             "to defaults.provider.")
    # `classify` runs one agent, so unlike the draft bake-off there is no reviewer to
    # keep on the shared provider: the candidate's `--provider` is all that moves.
    parser.add_argument("--candidate-provider",
                        help="Provider for the candidate arm only, e.g. `ollama` for a "
                             "local model scored against the hosted pin (STU-1767).")
    # Stage 2's production budget is 180s per conversation; a local model on Athena
    # needs ~90s per attempt, so one retry blows it. Raise it to score quality apart
    # from speed.
    parser.add_argument("--timeout", type=int, default=180,
                        help="Seconds allowed per stage-2 conversation.")
    parser.add_argument("-o", "--out", type=Path)
    parser.add_argument("--rules", type=Path, default=REPO_ROOT / "rules" / "rules.yaml")
    parser.add_argument("--policy", type=Path, default=REPO_ROOT / "rules" / "policy.yaml")
    parser.add_argument("--taxonomy", type=Path, default=REPO_ROOT / "rules" / "taxonomy.yaml")
    # Where `studio run classify` is spawned from, i.e. which `.studio/` it sees
    # (STU-1459). `--taxonomy`/`--policy`/`--rules` already let this harness score a
    # second deployment's fixtures; without `--cwd` it would still classify them with
    # this repo's own classifier agent and taxonomy skill, silently.
    parser.add_argument("--cwd", type=Path, default=REPO_ROOT)
    return parser.parse_args(argv)


def arm_providers(args: argparse.Namespace) -> tuple[str, str]:
    """The provider each arm runs under: baseline, then candidate."""
    return args.provider, args.candidate_provider or args.provider


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")

    if args.limit is not None and args.limit < 1:
        print("[ERROR] --limit must be at least 1: 0 would score nothing while naming "
              "the output file after a limit", file=sys.stderr)
        return 1

    if args.baseline == args.candidate:
        print("[ERROR] both arms name the same model: that measures the noise floor "
              "twice and nothing else", file=sys.stderr)
        return 1

    taxonomy = load_taxonomy(args.taxonomy)
    golden = classify_bakeoff.load_golden(args.golden, taxonomy)
    conversations = ab.escalations(
        load_corpus(args.corpus), load_rules(args.rules), load_policy(args.policy)
    )
    message_ids = [mid for c in conversations for mid in c.message_ids]

    # Before the first call, and against the whole escalating set even under --limit: a
    # golden that has gone stale is a wrong number whether or not this pass reads all of
    # it, and both arms cost a stage-2 pass per conversation to find that out afterwards.
    uncovered, absent = classify_bakeoff.coverage(message_ids, golden)
    if uncovered or absent:
        if uncovered:
            print(f"[ERROR] {len(uncovered)} escalating message(s) have no golden verdict: "
                  + ", ".join(uncovered), file=sys.stderr)
        if absent:
            print(f"[ERROR] {len(absent)} golden verdict(s) name a message that no longer "
                  "escalates: " + ", ".join(absent), file=sys.stderr)
        return 1

    if args.limit:
        conversations = conversations[: args.limit]
        message_ids = [mid for c in conversations for mid in c.message_ids]
        golden = {mid: golden[mid] for mid in message_ids}

    print(f"[classify-bakeoff] {len(conversations)} conversation(s) | "
          f"{args.repeats} repeat(s) per arm | {args.baseline} vs {args.candidate}",
          file=sys.stderr)

    def factory(model: str, provider: str):
        return lambda fields: StudioCliClassifier(
            taxonomy=taxonomy,
            provider=provider, cwd=args.cwd, anonymize_fields=fields, model=model,
            # A candidate model the API refuses is this pass's result, not an incident.
            alert_on_provider_failure=False,
            timeout=args.timeout,
        )

    arms = [
        ab.run_arm(model, [], conversations, factory(model, provider), args.repeats)
        for model, provider in zip((args.baseline, args.candidate), arm_providers(args))
    ]
    result = classify_bakeoff.compare(arms[0], arms[1], golden)
    print(classify_bakeoff.render(result))

    out = args.out or default_out(args.limit)
    out.parent.mkdir(parents=True, exist_ok=True)
    baseline_provider, candidate_provider = arm_providers(args)
    payload = classify_bakeoff.as_dict(result) | {
        "repeats": args.repeats,
        "provider": baseline_provider,
        "candidate_provider": candidate_provider,
    }
    if args.limit:
        payload["limit"] = args.limit
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"[classify-bakeoff] wrote {out}", file=sys.stderr)

    return 0 if result.measured and not result.candidate_is_worse else 1


if __name__ == "__main__":
    raise SystemExit(main())
