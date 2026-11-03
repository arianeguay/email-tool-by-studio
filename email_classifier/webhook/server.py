"""Push endpoints: classify each new email as the provider announces it.

Two provider routes over one pipeline, each enabled by its own config:

- POST /pubsub (Gmail via Cloud Pub/Sub push; needs $PUBSUB_AUDIENCE): verify
  the Pub/Sub OIDC token -> history.list from the last processed historyId ->
  fetch/project the new messages -> classify -> advance the stored historyId.
- POST /graph (Outlook via Graph change notifications; needs
  $GRAPH_CLIENT_STATE): echo the validationToken handshake, then per
  notification check clientState and fetch/project the announced message ids ->
  classify. No history state: the notification carries the ids directly.
- POST /graph/lifecycle (Outlook, same client state; needs
  $GRAPH_NOTIFICATION_URL to recreate with): the URL Graph announces
  `reauthorizationRequired`, `subscriptionRemoved` and `missed` on, and the only
  place it announces them (STU-937). Without it a lapsed grant stops delivery
  while the daily renew cron keeps succeeding — health reported over an
  unclassified mailbox. Each event is answered in kind: reauthorize, recreate,
  or catch up over the gap from the stored watermark.

Every route widens what it fetched to whole conversations before classifying
(STU-963): a notification names one message, and a report holding only that
message reconciles a reply as a conversation of one, leaving it in the Inbox
above a thread an earlier run archived.

Both run the same stage1/stage2 pipeline as run_email.py and write the same
report + frontier log, then run the same apply path as `make apply` over the
fresh verdicts (STU-849). That apply is dry-run unless `auto_apply` is true in
the policy: by default a notification produces a report and a plan, and the
mailbox is never mutated. `auto_apply` only removes the manual trigger —
`deterministic_can_finalize` and `destructive_actions_allowed` still decide what
may be applied.

Then, over the conversations the fresh report says owe a reply, the same
`drafts` pipeline `mailbox-full` ends on (STU-946) — out of process, because
drafting is a Studio fan-out. It is deliberately outside `auto_apply`: the batch
path gates drafting on nothing either, and a draft is never sent, so even the
classify-only phase of the pilot gets to read what the assistant would have
replied. A drafting failure is logged and dropped, never raised: the worker
would retry it, re-paying a stage-2 pass for a classification already on disk.

Neither handler classifies on the request thread (STU-927). A full pipeline run
is a `studio` subprocess and a live LLM pass — minutes, where both providers
expect an answer in seconds and redeliver when they do not get one, so
classifying inline all but guarantees a redelivery per notification. Instead the
handler authenticates the notification, records what it announced in a durable
queue and acks; one worker thread drains that queue. The queue is the crash
boundary: an item is on disk before the ack and removed only once processed.

That worker takes the queue in batches, not one at a time (STU-939). A provider
announces each message as it arrives, so five landing together would otherwise
be five runs -- five reports, five stage-2 passes, and the same person minted a
different token in each, since the keymap a run seeds is per run. The worker
therefore holds a short window open after the oldest queued item and classifies
whatever accumulated as one run. Only the *leading* run of same-provider items
merges: the queue is FIFO because its order carries meaning, and apply is bound
to the one provider `email.provider` names. Lifecycle events never merge --
each names a different repair.

Delivery is at-least-once on both providers and response codes drive
redelivery: 2xx acks, 5xx retries. Only what runs before the ack can ask for
one — a rejected token (403) or an unreadable envelope (acked and dropped,
since redelivery cannot fix a parse error). Past the ack there is no redelivery
to fall back on, so the worker retries a run that raises, a bounded number of
times with a growing backoff, before dropping the item and logging the ids it
gave up on (STU-947). It does drop it in the end: keeping it would wedge every
later notification behind one poison item. The attempt count rides on the queue
entry, so a run that takes the process down is not replayed forever by the
restart. The Gmail state is advanced only by a run that completed, so a dropped
item leaves its messages in the next notification's history.list window. A
failed mailbox operation does not even raise: it is logged and journalled, and
the state still advances. Holding it back would make the next notification
re-pay for a full stage-2 pass on mail already classified, and a message that
can never be applied would eventually cost the whole gap when the historyId
expires. `make apply RUN_ID=...` retries them.

A lost Graph grant is the one failure that ends the process (STU-930): no retry
can restore it, so the worker alerts through `email_classifier.alerts` and
SIGTERMs itself rather than rediscovering the refusal on every notification.

For the life of the server it also holds one name detector, and serves it to the
processes a run spawns (STU-938). Loading GLiNER costs ~20s and a 2.8 GB
allocation spike on the homelab, and every one of those processes was paying it
for itself -- `drafts_pre`, then a `org_memory-read` and a
`mail_precedents-search_mail` per drafting agent -- to do a fraction of a second
of detection. See `email_classifier.detector`.
"""

import base64
import itertools
import json
import logging
import os
import shutil
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from email_classifier import alerts, deployment
from email_classifier.anonymize import (
    Tokenizer,
    load_keymap,
    record_minted,
    run_keymap_path,
    sent_path,
    save_keymap,
)
from email_classifier.apply import status
from email_classifier.apply.executor import apply_verdicts, render_summary
from email_classifier.apply.journal import ApplyJournal, journal_path
from email_classifier.audit_filing import file_unaudited
from email_classifier.core import schema
from email_classifier.core.contract import load_taxonomy
from email_classifier.detector import Detector
from email_classifier.fetch.grant import GrantLost
from email_classifier.fetch import window
from email_classifier.fetch.threads import expand_to_conversations
from email_classifier.learn.store import VerdictStore, load_observations
from email_classifier.orchestrate.drafts import content_hold, load_holds, reply_targets
from email_classifier.orchestrate.drafts_runner import StudioCliDrafter
from email_classifier.orchestrate.pipeline import run
from email_classifier.orchestrate.provenance import (
    HOST_PROVIDER_ENV,
    LIVE_INPUT,
    collect,
    resolved_provider,
    unusable_provider,
)
from email_classifier.orchestrate.report import report_to_dict
from email_classifier.orchestrate.stage2_client import (
    StudioCliClassifier,
    claude_code_inherits_host_config,
    preflight_anonymize_scope,
)
from email_classifier.providers import (
    can_draft,
    configured_mailbox,
    get_writer,
    preflight_write,
)
from email_classifier.stage1.frontier_log import make_logger
from email_classifier.stage1.policy import load_policy
from email_classifier.stage1.rule_engine import load_rules
from email_classifier.webhook import address, deploy, state
from email_classifier.webhook.queue import WorkQueue

REPO_ROOT = deployment.ROOT

# A club mailbox is quiet overnight and over a weekend; a full day without a single
# notification is not.
_STALE_AFTER_SECONDS = 24 * 3600

# Four attempts over 30s + 60s + 120s of backoff: long enough to outlast the
# upstream blip or token refresh that a redelivery would have covered, short
# enough that a poison item does not hold the queue for an hour (STU-947).
_MAX_ATTEMPTS = 4
_RETRY_BACKOFF_SECONDS = 30.0

# Measured against real SMTP delivery into the staging mailbox (STU-1000): four
# `make deliver` rounds spread their Graph notifications 1.6s, 3.6s and 3.5s over
# five messages and 9.9s over ten -- about a second per message, longest gap
# between two notifications 2.9s. The window is fixed from the *first* item, so
# it has to cover a whole burst at once: 10s batched the ten-message round by
# 0.13s, which is not a margin. 15s covers a burst of roughly thirteen, and is
# still a third of the ~45s one stage-2 call took on that hardware -- the run is
# a subprocess and a live LLM pass, so the wait a lone message pays for the
# window is small against it, while a split burst costs a second run, report and
# keymap.
_DEBOUNCE_SECONDS = 15.0

# sysexits.h. The unit refuses to restart on 1 and restarts on this (STU-1004).
_EX_TEMPFAIL = 75

_log = logging.getLogger("email_classifier.webhook")

# Graph documents the validation token as opaque and free to change shape, but also
# that it never carries HTML or JavaScript.
_MAX_VALIDATION_TOKEN = 256
_MARKUP_CHARS = set("<>&\"'`")


def _coalesce(items: list[dict]) -> tuple[dict, list[dict]]:
    """One unit of work from the leading run of mergeable queue items (STU-939).

    Returns what to run and which queued items it covers, so the caller still
    counts an attempt and clears the queue per item -- a batch is a unit of work,
    never a unit of retry accounting.
    """
    head = items[0]
    provider = head["provider"]
    if provider == "outlook-lifecycle":
        return head, [head]

    run = list(itertools.takewhile(lambda item: item["provider"] == provider, items))
    if provider == "gmail":
        # One `history.list` from the *stored* id already covers the burst; what
        # the merge picks is only where to leave the watermark afterwards.
        # Highest rather than last: Pub/Sub does not promise order.
        work = {
            "provider": provider,
            "history_id": max((item["history_id"] for item in run), key=int),
        }
    else:
        work = {
            "provider": provider,
            "message_ids": list(dict.fromkeys(
                message_id for item in run for message_id in item["message_ids"]
            )),
        }
    return work, run


def _echo_validation_token(token: str):
    """Answer the subscription handshake — bounded, so it is not a reflector (STU-929).

    The route is a public unauthenticated POST: clientState gates what a
    notification does, not who may send one. Echoing any string back verbatim
    serves attacker-chosen content from our own hostname.
    """
    bounded = (
        0 < len(token) <= _MAX_VALIDATION_TOKEN
        and token.isascii()
        and token.isprintable()
        and not _MARKUP_CHARS & set(token)
    )
    if not bounded:
        _log.warning("refusing to echo a %d-character validationToken", len(token))
        return "", 400
    return token, 200, {"Content-Type": "text/plain"}


def _oidc_verifier(audience: str, service_account: str | None):
    """Default token verifier: Google-signed OIDC token for our audience."""

    def verify(token: str) -> None:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(token, ga_requests.Request(), audience=audience)
        if service_account and claims.get("email") != service_account:
            raise ValueError(f"unexpected service account: {claims.get('email')}")

    return verify


def _decode_envelope(payload) -> str:
    """Extract historyId from a Pub/Sub push envelope. Raises ValueError."""
    try:
        data = json.loads(base64.b64decode(payload["message"]["data"]))
        return str(data["historyId"])
    except (TypeError, KeyError, ValueError) as exc:
        raise ValueError(f"malformed Pub/Sub envelope: {exc}") from exc


def _is_history_expired(exc: Exception) -> bool:
    return getattr(getattr(exc, "resp", None), "status", None) == 404


def create_app(
    *,
    service=None,
    stage2=None,
    writer=None,
    drafts=None,
    reply_drafter=None,
    thread_reader=None,
    verify_token=None,
    graph_session=None,
    graph_client_state: str | None = None,
    graph_notification_url: str | None = None,
    rules_path: Path = REPO_ROOT / "rules" / "rules.yaml",
    learned_path: Path = REPO_ROOT / "rules" / "learned.yaml",
    policy_path: Path = REPO_ROOT / "rules" / "policy.yaml",
    taxonomy_path: Path = REPO_ROOT / "rules" / "taxonomy.yaml",
    draft_holds_path: Path = REPO_ROOT / "rules" / "draft_holds.yaml",
    log_path: Path = REPO_ROOT / "logs" / "frontier.jsonl",
    verdict_store_path: Path = REPO_ROOT / "logs" / "verdicts.jsonl",
    keymap_path: Path = REPO_ROOT / "logs" / "anonymization" / "keymap.json",
    report_dir: Path = REPO_ROOT / "reports",
    journal_dir: Path = REPO_ROOT / "logs" / "applied",
    audit_log_path: Path = REPO_ROOT / "audits" / "audit_log.json",
    skipped_path: Path = REPO_ROOT / "audits" / "skipped.json",
    drafts_stage_log: Path = REPO_ROOT / "logs" / "drafts" / "stages.jsonl",
    state_file: Path = REPO_ROOT / "state" / "gmail-history.json",
    liveness_file: Path = REPO_ROOT / "state" / "last-notification.json",
    run_count_file: Path = REPO_ROOT / "state" / "runs.json",
    subscription_file: Path = REPO_ROOT / "state" / "outlook-subscription.json",
    stale_after_seconds: float | None = None,
    tokenizer_factory=None,
    queue_file: Path = REPO_ROOT / "state" / "notifications.jsonl",
    max_attempts: int = _MAX_ATTEMPTS,
    retry_backoff_seconds: float = _RETRY_BACKOFF_SECONDS,
    debounce_seconds: float = _DEBOUNCE_SECONDS,
):
    """Build the Flask app. Config/setup errors fail loud here, before serving.

    The Gmail route is enabled by $PUBSUB_AUDIENCE (or an injected service /
    verify_token); the Outlook route by $GRAPH_CLIENT_STATE (a random secret
    echoed by Graph in every notification). service / stage2 / verify_token /
    graph_session are injectable for tests; by default they are the live
    provider clients, the Studio CLI classifier and Google OIDC verification
    against $PUBSUB_AUDIENCE (the push endpoint URL, optionally pinned to
    $PUBSUB_SERVICE_ACCOUNT). `writer` is the apply-path mailbox client — needed
    for a dry-run too, which reads labels to plan against. `drafts` runs the
    drafting pipeline; by default the Studio CLI, which no test should shell out to.
    `reply_drafter` and `thread_reader` are the mailbox reads `status_labels` asks
    after drafting (STU-2007), built only when the policy turns the flag on.

    GET /healthz is served whatever the enabled routes: it reports the age of the
    last notification received, against $WEBHOOK_STALE_AFTER_SECONDS, plus the
    running totals of notifications acked and runs completed, and the commit this
    process is serving (STU-1017).
    """
    from flask import Flask, request

    policy = load_policy(policy_path)
    taxonomy = load_taxonomy(taxonomy_path)

    gmail_enabled = (
        service is not None or verify_token is not None or bool(os.environ.get("PUBSUB_AUDIENCE"))
    )
    graph_client_state = graph_client_state or os.environ.get("GRAPH_CLIENT_STATE")
    outlook_enabled = graph_session is not None or graph_client_state is not None
    if not gmail_enabled and not outlook_enabled:
        raise RuntimeError(
            "no provider enabled: set PUBSUB_AUDIENCE (Gmail) and/or GRAPH_CLIENT_STATE (Outlook)"
        )
    if outlook_enabled and graph_client_state is None:
        raise RuntimeError("GRAPH_CLIENT_STATE is required for the Outlook route")
    graph_notification_url = graph_notification_url or os.environ.get("GRAPH_NOTIFICATION_URL")
    if outlook_enabled and graph_notification_url is None:
        # Recreating a removed subscription needs the URL it delivers to, and a
        # webhook that discovers it is missing only once Graph drops the
        # subscription is a webhook that never recovers (STU-937).
        raise RuntimeError(
            "GRAPH_NOTIFICATION_URL is required for the Outlook route — the public "
            "URL of /graph, the same one `make renew-graph` subscribes with"
        )

    # No default (STU-1013): the flag this becomes beats the provider an agent names
    # itself, so defaulting it here would re-pin the classifier to `claude-code` on
    # every host that does not set the variable.
    studio_provider = os.environ.get(HOST_PROVIDER_ENV) or None

    if stage2 is None:
        if shutil.which("studio") is None:
            raise RuntimeError("`studio` CLI not found on PATH. Install Studio first.")
        unsupported = preflight_anonymize_scope(policy.anonymization_fields, cwd=REPO_ROOT)
        if unsupported:
            raise RuntimeError(unsupported)
        # This provider drives the local CLI, which builds its system prompt from the
        # user and the cwd it is spawned in (STU-1005). Named per agent since
        # STU-1013, when the classifier pinned `anthropic` for itself and the drafter
        # did not, so one line for the run would have said the wrong thing about one
        # of them. STU-1133 removed that pin and the two agree again — the per-stage
        # line stays, because what made them disagree was one config field and the
        # message would go quietly wrong the next time someone sets it.
        #
        # Whether that inheritance still happens is the installed Studio's answer and
        # not this repo's (STU-1118): #271 seals both halves, so on a host that
        # carries it the old line named four files none of which reach an agent. It
        # is still logged on the sealed path, at INFO — which provider a host runs is
        # worth one line either way, and going silent would leave "no warning because
        # the CLI is sealed" reading exactly like "no warning because nobody looked".
        providers = {
            stage: resolved_provider(REPO_ROOT / ".studio", studio_provider, agent)
            for stage, agent in (("classify", "classifier"), ("drafts", "drafter"))
        }
        # Refuse at startup rather than one conversation at a time (STU-1128): a
        # provider without its credential fails every call the same way, so the server
        # would serve notifications it can only drop. Both stages, because both run
        # under the one STUDIO_PROVIDER and each falls back to its own agent's pin.
        for stage, provider in providers.items():
            unusable = unusable_provider(REPO_ROOT / ".studio", provider)
            if unusable:
                raise RuntimeError(f"{stage}: {unusable}")
        inheriting = [stage for stage, provider in providers.items()
                      if provider == "claude-code"]
        if inheriting:
            leaks = claude_code_inherits_host_config(cwd=REPO_ROOT)
            if leaks:
                _log.warning(
                    "%s runs through the `claude-code` provider and %s: every prompt "
                    "inherits %s/CLAUDE.md, ~/.claude/CLAUDE.md, ~/.claude/projects "
                    "auto-memory and %s/.claude/settings.json. Upgrade Studio, or set "
                    "STUDIO_PROVIDER=anthropic to send none of it",
                    " and ".join(inheriting), leaks, REPO_ROOT, REPO_ROOT,
                )
            else:
                _log.info(
                    "%s runs through the `claude-code` provider; this Studio spawns the "
                    "CLI sealed, so no CLAUDE.md, auto-memory or settings file of this "
                    "host reaches an agent",
                    " and ".join(inheriting),
                )
        # No `anonymize_fields`: the scope is honoured in `_classify` below, by this
        # repo's tokenizer, and asking the engine's middleware for it too would re-mint
        # tokens already written (STU-981). `preflight_anonymize_scope` still gates the
        # start — its `ner` half is what this path now depends on.
        stage2 = StudioCliClassifier(
            taxonomy=taxonomy,
            provider=studio_provider,
            cwd=REPO_ROOT,
        )

    if drafts is None:
        drafts = StudioCliDrafter(provider=studio_provider, cwd=REPO_ROOT)

    if policy.status_labels:
        clash = {f"ec/{b}" for b in taxonomy.buckets} & set(status.STATUS_LABELS)
        if clash:
            raise RuntimeError(f"bucket label(s) {sorted(clash)} collide with a status "
                               "label: the executor would read a status as a filing")
        if reply_drafter is None or thread_reader is None:
            # Built here rather than through `providers`, whose factories read the
            # headless flag from an environment this process never sets (STU-930).
            from email_classifier.apply.outlook import OutlookDrafter
            from email_classifier.fetch import outlook

            session = outlook.build_session(scopes=outlook.WRITE_SCOPES, interactive=False)
            reply_drafter = reply_drafter or OutlookDrafter(session)
            thread_reader = thread_reader or outlook.OutlookFetcher(session)
        holds = load_holds(draft_holds_path)

    if gmail_enabled:
        if verify_token is None:
            verify_token = _oidc_verifier(
                os.environ["PUBSUB_AUDIENCE"], os.environ.get("PUBSUB_SERVICE_ACCOUNT")
            )
        if service is None:
            from email_classifier.fetch import gmail

            service = gmail.build_service(gmail.get_credentials(interactive=False))

    if outlook_enabled and graph_session is None:
        from email_classifier.fetch import outlook

        graph_session = outlook.build_session(interactive=False)

    # `get_writer` asks the provider for a write grant even for a dry-run — which
    # reads labels to plan against. Checking it here turns a missing consent into a
    # startup error naming the fix, instead of an interactive prompt on a headless host.
    if writer is None:
        refusal = preflight_write(policy.email_provider)
        if refusal:
            raise RuntimeError(refusal)
        writer = get_writer(policy.email_provider, interactive=False)

    # Injectable for the same reason the clients above are: the default loads GLiNER,
    # which the suite must not need (STU-857). Per run, not per process — a run's own
    # keymap is what it minted (STU-924), and the shared file is only the seed.
    make_tokenizer = tokenizer_factory or (lambda: Tokenizer(load_keymap(keymap_path)))

    provider = resolved_provider(
        REPO_ROOT / ".studio", getattr(stage2, "provider", None)) or "unknown"
    cached: dict = {}

    def _start_date():
        """The run's date floor (STU-1123), asked for per run and never cached.

        This process serves for days. A floor resolved at startup would keep a
        default-`today` deployment reaching back to whenever it was last restarted,
        which is the one shape of drift a restart is supposed to remove.
        """
        return window.resolve(policy.start_date, ZoneInfo(policy.timezone))

    def _current_rules():
        """Rules + provenance, reloaded whenever either rules file changes on disk.

        `make promote` rewrites the learned layer out-of-process; a long-lived webhook
        that loaded rules once at startup would never see a promoted rule.
        """
        stamp = tuple(
            p.stat().st_mtime_ns if p.exists() else None for p in (rules_path, learned_path)
        )
        if cached.get("stamp") != stamp:
            cached["rules"] = load_rules(rules_path, learned_path)
            cached["provenance"] = collect(
                provider, rules_path, REPO_ROOT, learned_path, policy.anonymization_fields,
                # No dump exists for a push run: naming the source is what stops an audit
                # from cross-checking it against an unrelated dump (STU-859).
                LIVE_INPUT,
                # And which mailbox that live notification came from (STU-1183): the
                # push path is where both mailboxes have been served, and `LIVE_INPUT`
                # is the same string on either. Cached with the rest, unlike the floor
                # below — this one is configuration, and it does not move under a
                # process the way `today` does.
                mailbox=configured_mailbox(policy.email_provider),
            )
            cached["stamp"] = stamp
            _log.info("loaded %d rule(s) from %s + %s", len(cached["rules"]), rules_path, learned_path)
        # The floor is merged on the way out rather than collected into the cache:
        # this entry is only rebuilt when a rules file changes, so a floor stored in
        # it would be the one that held whenever `make promote` last ran (STU-1123).
        return cached["rules"], {**cached["provenance"],
                                 "start_date": _start_date().isoformat()}

    _current_rules()  # fail loud on malformed rules here, before serving

    store = VerdictStore(verdict_store_path)
    app = Flask(__name__)
    queue = WorkQueue(queue_file)
    app.extensions["webhook_queue"] = queue
    processors = {}

    def _clear(item: dict) -> None:
        try:
            queue.done(item)
        except Exception:
            # The item is out of the in-memory queue either way, so this only
            # leaves a stale line on disk for the next restart to replay. Letting
            # it end the thread would leave both routes acking into a queue
            # nobody drains — a silent loss behind a healthy-looking endpoint.
            _log.exception("could not clear %r from %s", item, queue.path)

    def _drain() -> None:
        # A failed run narrows the next pass to one item. A batch is a single fetch
        # and a single run, so one unreachable message would otherwise take the mail
        # queued beside it down with it, and the bound would drop them together.
        isolate = False
        while True:
            queued = queue.take_batch(0.0 if isolate else debounce_seconds)
            work, covered = _coalesce(queued[:1] if isolate else queued)
            attempts = [queue.attempt(item) for item in covered]
            try:
                processors[work["provider"]](work)
            except GrantLost as exc:
                # Left queued on purpose: only a browser restores the grant, and
                # replaying these items once it does is what the queue is for.
                _halt(exc)
                return
            except Exception:
                isolate = True
                # Dropped per item, not per batch: an item is only ever given up on
                # once its own count is spent, so one that rode along with a poison
                # neighbour keeps the tries it has left (STU-947).
                for item, count in zip(covered, attempts):
                    if count >= max_attempts:
                        _log.error(
                            "dropping an acked notification after %d failed "
                            "attempts: %r", count, item, exc_info=True,
                        )
                        _clear(item)
                kept = [count for count in attempts if count < max_attempts]
                if kept:
                    delay = retry_backoff_seconds * 2 ** (max(kept) - 1)
                    _log.warning(
                        "%d of %d queued notification(s) failed — retrying in %gs",
                        len(kept), len(covered), delay, exc_info=True,
                    )
                    # On the worker thread, so later notifications wait it out. That
                    # is the point: whatever is down is down for them too, and the
                    # bound keeps the wait finite.
                    time.sleep(delay)
                continue
            isolate = False
            for item in covered:
                _clear(item)

    def _halt(exc: GrantLost) -> None:
        """Alert a human and take the process down (STU-930).

        Every later notification would hit the same refusal, and each retry
        costs a fetch and a stage-2 pass to rediscover it. Only a browser
        restores the grant, so staying up serves nothing but a log nobody reads.
        SIGTERM rather than an exception: this runs on the worker thread, and
        raising there would end it while both routes keep acking into a queue
        nobody drains.
        """
        alerts.notify(
            "email-classifier: the mailbox grant is gone and the webhook is "
            f"stopping — mail arriving from now on is not classified. {exc}"
        )
        os.kill(os.getpid(), signal.SIGTERM)

    if stale_after_seconds is None:
        stale_after_seconds = float(
            os.environ.get("WEBHOOK_STALE_AFTER_SECONDS", _STALE_AFTER_SECONDS)
        )
    started_at = datetime.now(timezone.utc)
    # Sampled once, here, and not per request (STU-1017): a `git pull` under a running
    # service moves HEAD without changing a line of the code being served, and it is
    # the served code the drift question is about. Read at request time, the payload
    # would report itself current on exactly the deploy nobody restarted.
    deployed_commit = deploy.head(REPO_ROOT)
    kernel_commit = deploy.head(deploy.KERNEL_ROOT)

    @app.get("/healthz")
    def healthz():
        now = datetime.now(timezone.utc)
        last = state.load_last_notification(liveness_file)
        # Never notified since boot is as suspect as a dead subscription, so the age
        # falls back to the uptime — and the stamp outlives a restart, so bouncing the
        # process cannot reset the alert clock either.
        age = (now - (last or started_at)).total_seconds()
        subscription = state.load_subscription(subscription_file) or None
        # Always 200: the process answering IS the liveness signal, and a supervisor
        # restarting it over a stale subscription would only lose the uptime baseline.
        return {
            "status": "stale" if age > stale_after_seconds else "ok",
            "uptime_seconds": round((now - started_at).total_seconds(), 3),
            "last_notification_at": last.isoformat() if last else None,
            "last_notification_age_seconds": round(age, 3),
            # The phase-one gate compares these two to each other (STU-943), as an
            # inequality: the worker batches (STU-939), so runs below acks is the designed
            # shape, and only a `missed` catch-up — deliberately not stamped as a
            # notification — puts runs above acks.
            # Counting `reports/` instead answers a different question — it also holds
            # what `make run` and `make mailbox-full` left. Only the worker below moves
            # `runs_completed`, so a manual run is out of the comparison by construction.
            "notifications_acked": state.load_notification_count(liveness_file),
            "runs_completed": state.load_run_count(run_count_file),
            "stale_after_seconds": stale_after_seconds,
            # Which code produced all of the above (STU-1017). Without it the hourly
            # check cannot see a host running a merged fix's predecessor, and a behind
            # host looks exactly like a current one from out here.
            "commit": deployed_commit,
            # The kernel a deployment serves, which its own commit pins but does not
            # name; equal to `commit` where the kernel is the checkout (STU-1997).
            "kernel_commit": kernel_commit,
            "subscription": subscription,
        }

    def _apply_and_draft(report, payload, report_path: Path, email_dicts: list[dict],
                        notifying_provider: str) -> None:
        """What a fresh report drives in the mailbox: the apply, then the drafts."""
        # Both routes can be live at once, but the writer is the one provider
        # `email.provider` names — applying a Gmail message id through the Graph
        # mailbox would act on the wrong store. The off-policy route classifies only.
        if notifying_provider != policy.email_provider:
            _log.warning(
                "run %s: classified only — apply is bound to email.provider %r, "
                "not the notifying %r", report.run_id, policy.email_provider,
                notifying_provider,
            )
            return

        # The same two calls `scripts/apply_report.py` makes, on the in-memory report:
        # journalled under the run id whether it mutated or only planned, so
        # `make undo RUN_ID=...` reaches a push-applied run like any other.
        dry_run = policy.auto_apply is not True
        journal = ApplyJournal(
            journal_path(journal_dir, report.run_id, dry_run=dry_run), report.run_id
        )
        outcomes = apply_verdicts(
            report.verdicts, writer, policy, journal, taxonomy, dry_run=dry_run
        )
        _log.info("%s\nJournal written to %s", render_summary(outcomes, dry_run), journal.path)

        errors = [o for o in outcomes if o.status == "error"]
        if errors:
            # Not raised: a raise would hold back the Gmail historyId, so the next
            # notification re-pays for a full stage-2 pass and a message that can never
            # be applied eventually costs the whole gap when the historyId expires.
            # The classification is safe on disk; `make apply RUN_ID=...` retries the
            # failures, skipping what the marker says already landed.
            _log.error("run %s: %d verdicts failed to apply — see %s",
                       report.run_id, len(errors), journal.path)

        try:
            _draft(report, payload, report_path, email_dicts)
        finally:
            if policy.status_labels:
                _write_statuses(report, payload, email_dicts)

    def _write_statuses(report, payload, email_dicts: list[dict]) -> None:
        """The reviewer's view of the run (STU-2007), read off the mailbox after drafting.

        Under its own flag and outside `auto_apply`: it moves nothing. Journaled as real
        writes in the run's own apply journal, whatever `auto_apply` says, so
        `make undo RUN_ID=` takes the statuses off with the rest. Logged, never raised,
        for the reason the apply and the drafting are.
        """
        snippets = {e["message_id"]: e.get("body_snippet") or "" for e in email_dicts}

        def look(verdict):
            draft_id = reply_drafter.find_draft(verdict.thread_id)
            return status.ReplyState(
                hold=content_hold(snippets.get(verdict.message_id, ""), holds),
                draft=reply_drafter.get_draft(draft_id) if draft_id else None,
                last_sent=thread_reader.last_sent_at(verdict.thread_id),
            )

        journal = ApplyJournal(
            journal_path(journal_dir, report.run_id, dry_run=False), report.run_id)
        try:
            statuses = status.derive(payload, taxonomy, look)
            failed = status.write(payload, statuses, writer, journal)
        except Exception:
            _log.exception("run %s: status labels not written", report.run_id)
            return
        _log.info("run %s: status labels on %d conversation(s), %d failure(s): see %s",
                  report.run_id, len(statuses), failed, journal.path)

    def _draft(report, payload, report_path: Path, email_dicts: list[dict]) -> None:
        # Drafts last, as in `mailbox-full` (STU-946): without this a notification
        # produces a verdict and nothing to send, and the drafting half of the product
        # is reachable only from a manual run. Not gated on `auto_apply` — the batch
        # path gates it on nothing either, and a draft is unsent by construction, so
        # the classify-only phase of the pilot still reads what it would have replied.
        targets = reply_targets(payload, taxonomy)
        if not targets:
            # Said out loud for the reason `drafts-pre` opens a stage record (STU-917):
            # a run with nothing to answer must not look like a run that never drafted.
            _log.info("run %s: nothing owes a reply — no drafting run", report.run_id)
            return
        if not can_draft(policy.email_provider):
            # Knowable before spawning, and the subprocess only rediscovers it: a
            # provider with no body fetcher fails in `drafts-pre`, once per
            # notification, for as long as the mailbox keeps receiving (STU-1002).
            _log.info(
                "run %s: %d reply target(s) not drafted — email.provider %r has no "
                "drafting path", report.run_id, len(targets), policy.email_provider,
            )
            return
        try:
            drafts.draft(report_path, email_dicts)
        except Exception:
            # Not raised: the worker would retry the whole item, re-paying a stage-2
            # pass for a classification already on disk and already applied.
            _log.exception(
                "run %s: drafting %d reply target(s) failed — the classification and "
                "apply stand; `make mailbox-full` retries the drafts",
                report.run_id, len(targets),
            )

    def _classify(email_dicts: list[dict], notifying_provider: str) -> None:
        emails = [schema.validate(item) for item in email_dicts]
        rules, provenance = _current_rules()
        logger = make_logger(policy, log_path)
        # Seeded from the shared keymap so a parent keeps their token across runs — a
        # webhook classifies one notification at a time, so every run of a thread would
        # otherwise mint the same person a new number (STU-843).
        tokenizer = make_tokenizer() if policy.anonymization_fields else None
        try:
            report = run(emails, rules, policy, stage2, logger, provenance, store,
                         tokenizer, sent_path(keymap_path, logger.run_id))
        finally:
            # In a `finally` because a value that reached the LLM must keep its token
            # even when the run then died: the next notification on that thread is the
            # one that would mint it a second identity.
            if tokenizer is not None:
                save_keymap(keymap_path, tokenizer.keymap)
                record_minted(run_keymap_path(keymap_path, logger.run_id), tokenizer.minted())
        payload = report_to_dict(report)
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{report.run_id}.json"
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _log.info(
            "run %s: %d message(s), %d deterministic, %d llm, %d error(s)",
            report.run_id, report.totals["emails"], report.totals["deterministic"],
            report.totals["llm"], report.totals["errors"],
        )
        # Counted against the report, not against the end of this function: the apply
        # below logs its failures instead of raising, so returning is not the thing that
        # makes a run comparable to the notification that asked for it.
        state.record_run(run_count_file)

        try:
            _apply_and_draft(report, payload, report_path, email_dicts,
                             notifying_provider)
        finally:
            # File this run's own audit entry, and any earlier push run still unfiled
            # (STU-1084). In a `finally`, and after the drafting rather than before it:
            # the drafts leg only reaches `logs/drafts/stages.jsonl` once the fan-out has
            # run, so an entry filed ahead of it reads a cache hit ratio for the classify
            # leg alone — permanently, since the entry then covers the run and no later
            # walk re-reads it. That leg is the one STU-1057 exists for.
            # Logged and swallowed rather than raised: the report is already on disk, so
            # a failure here buys nothing — the next run's walk picks the report up, and a
            # filing that never happens degrades to the blocking gate of today.
            try:
                filed = file_unaudited(report_dir, audit_log_path, skipped_path,
                                       log_path, drafts_stage_log, taxonomy)
                if filed:
                    _log.info("filed %d audit entr(ies), awaiting a verdict: %s",
                              len(filed), ", ".join(filed))
            except Exception:
                _log.exception("run %s: could not file an audit entry — the report stands "
                               "and `audit-gate` will hold on it", report.run_id)

    if gmail_enabled:

        def _handle_gmail(item: dict) -> None:
            notified_history_id = item["history_id"]
            last = state.load_history_id(state_file)
            if last is None:
                state.save_history_id(state_file, notified_history_id)
                _log.warning(
                    "no stored historyId: baseline set to %s; earlier mail was not "
                    "classified (scripts/renew_watch.py baselines explicitly at setup)",
                    notified_history_id,
                )
                return

            from email_classifier.fetch import gmail

            try:
                message_ids = gmail.list_new_message_ids(service, last)
            except Exception as exc:
                if _is_history_expired(exc):
                    # historyId older than Gmail's ~1 week retention: messages in the
                    # gap are unrecoverable via history.list — rebaseline so the next
                    # notification is one we can actually serve.
                    state.save_history_id(state_file, notified_history_id)
                    _log.error(
                        "stored historyId %s expired; rebaselined to %s — run "
                        "`make run-fetch` to classify the gap", last, notified_history_id,
                    )
                    return
                raise

            if message_ids:
                fetcher = gmail.GmailFetcher(service)
                _classify(
                    expand_to_conversations(
                        fetcher, fetcher.fetch_inputs_by_ids(message_ids),
                        start_date=_start_date(), tz=ZoneInfo(policy.timezone),
                    ),
                    "gmail",
                )

            # Advanced over the whole window, the floor's drops included (STU-1123),
            # for the reason the Graph catch-up states: the floor only moves forward
            # under its own default, so mail below it does not come back into scope.
            state.save_history_id(state_file, notified_history_id)

        processors["gmail"] = _handle_gmail

        @app.post("/pubsub")
        def pubsub():
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return "missing bearer token", 403
            try:
                verify_token(auth.removeprefix("Bearer "))
            except Exception:
                _log.warning("rejected push: OIDC verification failed", exc_info=True)
                return "invalid token", 403

            try:
                notified_history_id = _decode_envelope(request.get_json(silent=True))
            except ValueError:
                _log.error("dropping malformed Pub/Sub envelope", exc_info=True)
                return "", 204

            state.record_notification(liveness_file)
            _log.info("queued a Pub/Sub notification at history %s", notified_history_id)
            queue.put({"provider": "gmail", "history_id": notified_history_id})
            return "", 204

    if outlook_enabled:
        from email_classifier.fetch import outlook
        from email_classifier.fetch.outlook import OutlookFetcher

        fetcher = OutlookFetcher(graph_session)

        def _classify_outlook(notified: list[dict]) -> None:
            """Classify the notified mail with its conversations, then move the watermark.

            Advanced after the run, never before: the watermark is where a
            `missed` catch-up resumes, and moving it over mail a failed run
            dropped would hide exactly what the catch-up exists to recover.

            Dated off the notified messages alone, never off the siblings the
            expansion pulled in (STU-963): a thread can hold a message newer than
            the one Graph just announced — a notification we never got — and letting
            it set the watermark would put that message below the resume point of
            the catch-up that exists to find it.
            """
            _classify(
                expand_to_conversations(
                    fetcher, notified, start_date=_start_date(),
                    tz=ZoneInfo(policy.timezone)),
                "outlook",
            )
            if notified:
                state.save_watermark(
                    subscription_file, max(e["date"] for e in notified)
                )

        def _handle_outlook(item: dict) -> None:
            _classify_outlook(fetcher.fetch_inputs_by_ids(item["message_ids"]))

        def _catch_up() -> None:
            """Answer `missed`: classify everything since the watermark.

            The gap itself stays uncapped — a cap would silently truncate exactly
            the mail this path exists to recover — but nothing in it is paid for
            twice or paid for quietly (STU-954). A message the verdict store
            already holds was escalated to stage 2 by an earlier run, and stage 2
            is the whole cost of a catch-up; the watermark only moves on the push
            path, so a manual `make run` leaves its mail below it. What the store
            has no opinion on is re-run for the price of stage 1.
            """
            watermark = state.load_subscription(subscription_file).get("watermark")
            if watermark is None:
                _log.error(
                    "Graph reported missed notifications but no watermark is "
                    "recorded — run `make run-fetch` to classify the gap"
                )
                return
            emails = fetcher.fetch_inputs_since(watermark)
            classified = {o.message_id for o in load_observations(store.path)}
            fresh = [e for e in emails if e["message_id"] not in classified]
            if fresh:
                # Siblings are counted separately in the alert, and deliberately not
                # filtered by the store (STU-963): a thread whose older half is left
                # out of the report is the split the expansion exists to close, and
                # re-reading it is what the gap costs.
                floor = _start_date()
                tz = ZoneInfo(policy.timezone)
                batch = expand_to_conversations(fetcher, fresh, start_date=floor, tz=tz)
                # Counted against what the floor left, not against `fresh`: a gap
                # reaching below the floor otherwise reports more siblings than it
                # has, negatively, at exactly the moment mail is being set aside.
                in_scope = window.in_scope(fresh, floor, tz)
                held_back = len(
                    [e for e in emails if e["message_id"] in classified]
                ) - len([e for e in batch if e["message_id"] in classified])
                # Out-of-band, not just logged: a gap is unbounded by design, so
                # the operator has to learn its size while it is being paid for.
                # `held_back` counts what the store spared *net of the expansion*:
                # a message the store already ruled on comes back anyway when a
                # thread in the gap needs it, and that one is paid for.
                alerts.notify(
                    f"Graph reported missed notifications: classifying "
                    f"{len(batch)} message(s) since {watermark} — {len(in_scope)} in "
                    f"the gap plus {len(batch) - len(in_scope)} thread sibling(s) — one "
                    f"live stage-2 pass each ({held_back} already in the verdict "
                    f"store, skipped; {len(fresh) - len(in_scope)} out of scope before "
                    f"{floor.isoformat()})"
                )
                _classify(batch, "outlook")
            else:
                _log.warning(
                    "Graph reported missed notifications: all %d message(s) since "
                    "%s were already classified", len(emails), watermark,
                )
            if emails:
                # Over everything fetched, not just what was reclassified: a skipped
                # message is one already ruled on, or one the floor put out of scope
                # (STU-1123), and leaving the watermark behind either would re-fetch
                # it on every later catch-up. The floor only moves forward under its
                # own default, so mail below it does not come back into scope; an
                # operator who widens it sweeps with `make mailbox-full`, which reads
                # no watermark.
                state.save_watermark(
                    subscription_file, max(e["date"] for e in emails)
                )

        def _handle_lifecycle(item: dict) -> None:
            event = item["event"]
            subscription_id = item["subscription_id"]
            if event == "missed":
                _catch_up()
            elif event == "reauthorizationRequired":
                response = outlook.reauthorize_subscription(graph_session, subscription_id)
                state.record_subscription(subscription_file, response)
                _log.warning(
                    "reauthorized subscription %s — active again until %s",
                    subscription_id, response["expirationDateTime"],
                )
            elif event == "subscriptionRemoved":
                # Creating one makes Graph call both our own URLs for the
                # validation handshake, and this runs on the worker thread —
                # the server has to be answering requests while it waits.
                response = outlook.subscribe(
                    graph_session, graph_notification_url, graph_client_state
                )
                state.record_subscription(subscription_file, response)
                _log.warning(
                    "subscription %s was removed by Graph — recreated as %s, active "
                    "until %s", subscription_id, response["id"],
                    response["expirationDateTime"],
                )
            else:
                _log.error("ignoring unknown Graph lifecycle event %r", event)

        processors["outlook"] = _handle_outlook
        processors["outlook-lifecycle"] = _handle_lifecycle

        @app.post("/graph")
        def graph():
            # Subscription-creation handshake: echo the token back as plain text.
            validation_token = request.args.get("validationToken")
            if validation_token is not None:
                return _echo_validation_token(validation_token)

            payload = request.get_json(silent=True) or {}
            message_ids: list[str] = []
            authenticated = False
            for notification in payload.get("value", []):
                if notification.get("clientState") != graph_client_state:
                    _log.warning("dropping Graph notification with bad clientState")
                    continue
                authenticated = True
                message_id = (notification.get("resourceData") or {}).get("id")
                if message_id and message_id not in message_ids:
                    message_ids.append(message_id)

            if authenticated:
                state.record_notification(liveness_file)
            if message_ids:
                # The only durable record of *when* a notification arrived: the
                # liveness stamp is overwritten and a queue entry is deleted once
                # run. Timestamped by the log, it is what the burst window is
                # measured against (STU-1000).
                _log.info("queued a Graph notification naming %d message id(s)", len(message_ids))
                queue.put({"provider": "outlook", "message_ids": message_ids})
            return "", 202

        @app.post("/graph/lifecycle")
        def graph_lifecycle():
            """Where Graph announces that delivery itself is in trouble (STU-937)."""
            validation_token = request.args.get("validationToken")
            if validation_token is not None:
                return _echo_validation_token(validation_token)

            # Deliberately not stamped as a notification: a lifecycle event proves
            # Graph can reach us, not that mail is being delivered. Counting it as
            # liveness would let /healthz read healthy on the one signal that says
            # delivery has stopped.
            for notification in (request.get_json(silent=True) or {}).get("value", []):
                if notification.get("clientState") != graph_client_state:
                    _log.warning("dropping Graph lifecycle event with bad clientState")
                    continue
                queue.put({
                    "provider": "outlook-lifecycle",
                    "event": notification.get("lifecycleEvent"),
                    "subscription_id": notification.get("subscriptionId"),
                })
            return "", 202

    threading.Thread(target=_drain, name="webhook-worker", daemon=True).start()
    return app


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        app = create_app()
    except GrantLost as exc:
        # A reboot at 3am that cannot sign in is the same outage as one that
        # dies mid-service, and just as invisible without a signal (STU-930).
        alerts.notify(f"email-classifier: the webhook cannot start — {exc}")
        return 1
    except OSError as exc:
        # At boot, `network-online.target` is reached before DNS answers. Both
        # providers raise GrantLost for an unusable credential, so what reaches
        # here is the network — and starting again is what fixes it (STU-1004).
        _log.error("provider unreachable at startup: %s", exc)
        return _EX_TEMPFAIL
    from waitress import serve

    # Held for the life of the server, not of a run (STU-938). Every process a
    # notification spawns loads GLiNER for itself otherwise — `drafts_pre`, and each
    # `org_memory-read` and `mail_precedents-search_mail` the drafting agents make —
    # at ~20s and a 2.8 GB allocation spike apiece on the homelab, for well under a
    # second of detection. Started whatever the policy says about anonymization: what
    # reaches the detector is decided per call site, not by `anonymization.scope`.
    with Detector():
        serve(app, host=address.listen_host(), port=address.listen_port())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
