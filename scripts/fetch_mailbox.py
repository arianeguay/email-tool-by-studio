#!/usr/bin/env python3
"""Fetch stage of `mailbox-full` (STU-846): mailbox -> provider-neutral JSON dump.

Script executor interface: reads JSON from stdin, writes JSON to stdout.

Which mailbox is read comes from `email.provider` in policy.yaml — the same
switch the apply path reads, so one run can never fetch one mailbox and mutate
another. The pipeline input's `provider` is the LLM provider, not this one.

The listing is widened to whole conversations before the dump is written
(STU-963): `max_results` caps how many conversations are looked at, not how much
of one is read, so a thread straddling two fetch windows still reconciles whole.

The dump is named after a run id minted here, not after a path the input names
(STU-896): a shared path meant every run overwrote the previous one's corpus,
leaving nothing to replay or cross-check. The id travels on to `classify-batch`,
so the report and the input it consumed share a name — the convention
`make run-fetch` already writes (STU-821).

Input:  { "query": "...", "max_results": N, "dump_dir": "<dir>" }
Output: { "run_id", "dump_path", "count", "provider" }
"""

import json
import sys
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from email_classifier.core import schema
from email_classifier.fetch import window
from email_classifier.fetch.threads import expand_to_conversations
from email_classifier.providers import configured_mailbox, get_fetcher
from email_classifier.stage1.policy import load_policy
from email_classifier import deployment

REPO_ROOT = deployment.ROOT

_DEFAULT_MAX_RESULTS = 100


def main() -> None:
    payload = json.load(sys.stdin)
    ctx = yaml.safe_load(payload.get("additional_context", "") or "") or {}

    policy = load_policy(REPO_ROOT / ctx.get("policy", "rules/policy.yaml"))
    fetcher = get_fetcher(policy.email_provider)
    tz = ZoneInfo(policy.timezone)
    start_date = window.resolve(policy.start_date, tz)
    inputs = expand_to_conversations(
        fetcher,
        fetcher.fetch_inputs(
            query=ctx.get("query") or "",
            max_results=int(ctx.get("max_results") or _DEFAULT_MAX_RESULTS),
            since=window.since_iso(start_date, tz),
        ),
        start_date=start_date,
        tz=tz,
    )

    # Fail loudly before writing if any projected object breaks the schema.
    for item in inputs:
        schema.validate(item)

    run_id = str(uuid.uuid4())
    dump_path = REPO_ROOT / ctx.get("dump_dir", "dumps") / f"{run_id}.json"
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    dump_path.write_text(json.dumps(inputs, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[fetch] {len(inputs)} emails from {policy.email_provider} since "
          f"{start_date.isoformat()} -> {dump_path}", file=sys.stderr)
    json.dump({
        "run_id": run_id,
        "dump_path": str(dump_path),
        "count": len(inputs),
        "provider": policy.email_provider,
        # The floor this fetch honoured (STU-1123). What is not in the dump and is
        # older than this was out of scope, not missed — the two are otherwise
        # indistinguishable from the artifacts a run leaves.
        "start_date": start_date.isoformat(),
        # Which mailbox those ids came out of (STU-1183), for the classify stage to
        # put in the report. Empty rather than null where nothing names one — this
        # is a value a template carries to the next stage, and the reader turns the
        # empty string back into the null the report publishes.
        "mailbox": configured_mailbox(policy.email_provider) or "",
    }, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
