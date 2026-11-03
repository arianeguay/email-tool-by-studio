# `.env` is where the README tells you to put OUTLOOK_CLIENT_ID and friends, but
# nothing read it: the code sees os.environ, which only carries what the shell
# exported. Loading it here covers every target at once.
#
# The environment wins over `.env` (STU-2012). A makefile assignment beats the
# environment, so `op run --env-file .env -- make <target>` resolved each `op://`
# reference and the include put the literal reference straight back. Every key `.env`
# names that the environment already carries is saved before the include and restored
# after it, as a simple variable so a `$` in a secret is never expanded.
ENV_FILE_KEYS := $(shell test -r .env && sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*[?:]\{0,1\}=.*/\2/p' .env)
$(foreach k,$(ENV_FILE_KEYS),$(if $(filter environment%,$(origin $k)),$(eval _ENV_$k := $$(value $k))))
-include .env
$(foreach k,$(ENV_FILE_KEYS),$(if $(filter-out undefined,$(origin _ENV_$k)),$(eval $k := $$(_ENV_$k))$(eval undefine _ENV_$k)))
export
unexport ENV_FILE_KEYS UNRESOLVED_KEYS OFFLINE_GOALS

# A key still holding an `op://` reference is a secret nothing resolved, and the call it
# feeds fails on the reference itself (STU-2012: a Graph login against
# `.../op://Dev/...`). Refused before any recipe runs, so before any LLM or Graph call.
# Only the goals that read no secret may run without `op run`; a new target refuses until
# it is listed here, the direction a forgotten entry should fail in.
# pull-webhook is listed because only its dispatch runs here: it reads WEBHOOK_SSH_HOST and
# opens ssh to the serving host, which reads its own .env (STU-2066).
OFFLINE_GOALS := test test-venv test-ner clean observe-spans-check qa-gate-check qa-coverage-gate-check signature-anchor-check pull-webhook memory-harvest-approve
UNRESOLVED_KEYS := $(strip $(foreach k,$(ENV_FILE_KEYS),$(if $(filter op://%,$(value $k)),$k)))
ifneq ($(UNRESOLVED_KEYS),)
ifneq ($(filter-out $(OFFLINE_GOALS),$(or $(MAKECMDGOALS),default)),)
$(error .env sets $(words $(UNRESOLVED_KEYS)) key(s) to an op:// reference nothing resolved: $(UNRESOLVED_KEYS). Run: op run --env-file .env -- make $(MAKECMDGOALS))
endif
endif
# `export` above sends every variable to every child, `.env` included. All three agents
# interpolate a model and fall back to `defaults.model` or to their own pin when it is
# unset, so a stray value here would silently classify or draft real mail under a model
# no report can name. Each bake-off sets its own per child process, never through make.
unexport CLASSIFY_MODEL DRAFT_QA_MODEL DRAFT_MODEL

# `-include` cannot tell a missing .env from one this account may not open: both arrive
# as nothing, so every key it sets reads as unset and the target blames the variable
# rather than the permission (STU-1122). Since STU-1116 put the serving host's copy at
# 700 email-classifier, the second is the likely one there, and the repair is to run the
# target as that account -- never to export a value by hand. Named here once for every
# target rather than rediscovered per target; `make check-webhook` is what names the
# file, its owner and the repair, being the target the confusion was measured on.
# `-e -o -L` and not `-r` alone: `-r` is false for a file that is simply not there,
# which is the ordinary case on every checkout that never had one.
ifneq ($(shell test -e .env -o -L .env && test ! -r .env && echo unreadable),)
$(warning .env is present and this account cannot read it, so every key it sets arrives unset. Run the target under the account that owns it; `make check-webhook` names that file and that account.)
endif

.PHONY: require-run-id audit-gate audit-file audit-skip run run-batch run-fetch mailbox-full mailbox-poll redraft fetch fetch-outlook fetch-seeded apply-plan apply undo eval-fixtures eval-seed eval-threads eval-threads-seed eval-seeded eval-parity eval-spans eval-spans-seed eval-claims eval-claims-seed eval-ab qa-bakeoff qa-gate qa-gate-check qa-erosion qa-coverage qa-coverage-gate qa-coverage-gate-check qa-live-sources classify-bakeoff draft-bakeoff pilot-gate voice-counts memory-harvest memory-harvest-approve seed seed-threads seed-reply deliver deliver-threads deliver-compare export-review unseed unseed-force serve-webhook renew-watch renew-graph renew-graph-recreate check-webhook check-unverdicted install-webhook-service pull-webhook pull-webhook-here deploy-webhook prune prune-apply recent-mail outlook-consent gmail-consent promote test test-venv test-ner clean reset-state

DUMP     ?= dumps/gmail-dump.json
OUTLOOK_DUMP ?= dumps/outlook-dump.json
CORPUS   ?= fixtures
GOLDEN   ?= eval/golden.json
SEED_CORPUS ?= $(CORPUS)
THREAD_CORPUS ?= fixtures-threads
THREAD_GOLDEN ?= eval/threads-golden.json
STAGE2_GOLDEN ?= eval/stage2-golden.json
SEED_GOLDEN ?= eval/seeded-golden.json
SPAN_GOLDEN ?= eval/ner-spans.json
CLAIM_CORPUS ?= eval/claiming-corpus.json
CLAIM_GOLDEN ?= eval/claiming-spans.json
SEED_DUMP ?= dumps/outlook-seeded.json
DELIVERED_DUMP ?= dumps/outlook-delivered.json
REVIEW_DUMP ?= dumps/outlook-review.json
FORCE    ?=
COUNT    ?=
MIX      ?=
ADD      ?=
QUERY    ?=
LINK     ?=
SOURCE   ?=
HEALTH_JOURNAL ?=
REASON   ?=
MESSAGE  ?=
MAX      ?= 33
# Empty on purpose (STU-1013): `--provider` is an override, and the CLI flag beats
# the provider an agent names itself. Forcing one here re-pinned every stage to
# `claude-code` and undid the classifier's own choice. Set it for a deliberate A/B.
PROVIDER ?=
PROVIDER_FLAG = $(if $(PROVIDER),--provider $(PROVIDER))
# What a path handling real mail runs under (STU-1128). `PROVIDER` still wins; under it
# is the host's own STUDIO_PROVIDER, which `stage2_client.py` already passes as the flag
# — until this line `classify-batch` alone kept falling back to `classifier.agent.yaml`,
# so `make mailbox-full` and a notification classified the same mail under two providers
# and one of them had no credential.
#
# Two flags because there are two questions, and collapsing them is what made the axis
# ambiguous in the first place: the bake-offs and `eval-ab` keep `PROVIDER_FLAG`, since a
# measurement taken under whatever a host happens to run is a measurement of the host —
# the reason `draft_bakeoff.py` names `anthropic` for itself rather than inheriting one.
LIVE_PROVIDER = $(if $(PROVIDER),$(PROVIDER),$(STUDIO_PROVIDER))
LIVE_PROVIDER_FLAG = $(if $(LIVE_PROVIDER),--provider $(LIVE_PROVIDER))
REPEATS  ?= 2
# A worktree has no .venv/ of its own, so fall back to the main checkout's. The
# editable install there pins main's sources, so PYTHONPATH points every target back at
# this checkout — PYTHONPATH precedes the editable .pth entry on sys.path (STU-867).
MAIN_CHECKOUT := $(patsubst %/.git,%,$(abspath $(shell git rev-parse --git-common-dir)))
PYTHON   ?= $(firstword $(wildcard .venv/bin/python) $(MAIN_CHECKOUT)/.venv/bin/python)
# Where the kernel's own pyproject.toml and package live. `.` here; a deployment that
# includes this Makefile from its own checkout names the directory it pins the kernel
# in, and its root stays first on PYTHONPATH for its own modules.
KERNEL ?= .
PYTHONPATH := $(CURDIR)$(if $(filter-out .,$(KERNEL)),:$(abspath $(KERNEL)))$(if $(PYTHONPATH),:$(PYTHONPATH))
# The same interpreter for Studio's own script stages, which otherwise sniff a
# `.venv/` beside the cwd — a worktree has none, and a venv elsewhere is the case
# STU-856 gave one button (STU-1030: the mailbox write is now one of those stages).
STUDIO_PYTHON_BIN := $(PYTHON)
# One detector for a whole batch run, served to the processes it spawns — the trick the
# webhook plays on the push path (STU-938), applied to targets that are not its
# children (STU-1020).
WITH_DETECTOR := $(PYTHON) scripts/with_detector.py
# The suite's own venv, and the one `make test` runs (STU-1059). Deliberately not
# `$(PYTHON)`: that one resolves to a checkout set up per the Setup section, so it
# carries the `ner` extra and every module touching a name loads GLiNER — measured
# 2026-08-28, `tests/test_drafts.py` alone in 80 s there against 5,4 s for the whole
# suite without it. What goes in is what `tests.yml`'s `hermetic` job installs, so a
# branch's default check is the set of tests CI will run.
# It lives in the main checkout for the reason $(PYTHON) falls back there: a fresh
# worktree has no venv, and one built per worktree would pay the install again for
# every branch. Which sources run is still PYTHONPATH's answer, not the editable
# .pth's — the whole point of STU-867 — so a venv the last worktree installed into
# still runs this one's code.
TEST_VENV ?= $(MAIN_CHECKOUT)/.venv-test
TEST_PYTHON := $(TEST_VENV)/bin/python
# Only ever used to create that venv, so it does not inherit the extra-carrying
# interpreter and does not need one to exist at all.
BASE_PYTHON ?= python3
PYTEST_ARGS ?=
JOURNAL  ?=
LIMIT    ?=
QA_BASELINE ?= claude-opus-4-8
QA_CANDIDATE ?= claude-sonnet-5
QA_BAKEOFF_OUT ?= logs/qa-bakeoff.json
QA_GATE_CORPUS ?= eval/draft-qa-rejections.json
QA_GATE_OUT ?= logs/qa-gate.json
QA_EROSION_OUT ?= logs/qa-erosion.json
QA_LIVE_SOURCES_OUT ?= logs/qa-live-sources.json
QA_COVERAGE_OUT ?= logs/qa-coverage.json
QA_COVERAGE_GATE_OUT ?= logs/qa-coverage-gate.json
COVERAGE_CORPUS ?= eval/draft-coverage-cases.json
CLASSIFY_BASELINE ?= claude-opus-4-8
CLASSIFY_CANDIDATE ?= claude-haiku-4-5
PILOT_DELTA_OUT ?= logs/pilot-delta.json
PILOT_GATE_OUT ?= logs/pilot-gate.json
DRAFT_BASELINE ?= claude-opus-4-8
DRAFT_CANDIDATE ?= claude-sonnet-5
BATCH_INPUT ?= logs/classify-batch.input.yaml
MAILBOX_INPUT ?= logs/mailbox-full.input.yaml
REDRAFT_INPUT ?= logs/redraft.input.yaml
APPLY    ?= false
RUN_ID   ?= $(shell ls -t reports/*.json 2>/dev/null | head -1 | xargs -r basename -s .json)

require-run-id:
	@test -n "$(RUN_ID)" || { echo "[ERROR] no report in reports/ — pass RUN_ID=<run-id>"; exit 1; }

# `coverage` aborts on an empty reports/ by design — no report is not a pass. The gate
# asks the narrower question "may a new run start", and before the first run there is
# nothing to gate. It runs before the run, not after: a run's own audit cannot exist yet,
# so what it holds up is the previous one (STU-900).
audit-gate:          ## Refuse to start a run while a prior one has no audit entry (STU-900)
	@ls reports/*.json >/dev/null 2>&1 || exit 0; \
	$(PYTHON) scripts/audit_run.py coverage

# Only push runs (STU-1084). A `make run` is a person at a keyboard who is already
# looking, so the gate holding it costs nothing; what does not converge is the webhook's
# one report per notification batch. The webhook files after every run — this target is
# for a host that served runs while an older build was deployed.
audit-file:          ## File the audit entry every unfiled push run already determines (STU-1084)
	$(PYTHON) scripts/audit_run.py file

# Unlike apply/undo, this one never falls back to the newest report: the marker is
# permanent and committed, and guessing would excuse a run nobody looked at.
audit-skip:          ## Mark a throwaway run as not worth auditing (RUN_ID=, REASON=) (STU-900)
	@test "$(origin RUN_ID)" = "command line" || { echo "[ERROR] pass RUN_ID=<run-id> explicitly"; exit 1; }
	@test -n "$(REASON)" || { echo "[ERROR] pass REASON=<why this run does not earn an audit>"; exit 1; }
	$(PYTHON) scripts/audit_run.py skip --run $(RUN_ID) --reason "$(REASON)"

run: audit-gate      ## End-to-end run on a frozen dump
	$(WITH_DETECTOR) $(PYTHON) run_email.py --dump $(DUMP) $(LIVE_PROVIDER_FLAG) -v

run-batch: audit-gate ## Same run, orchestrated by Studio — LLM fan-out over the grey zone (STU-843)
	@mkdir -p $(dir $(BATCH_INPUT))
	@printf 'corpus: %s\nprovider: %s\n' '$(DUMP)' '$(LIVE_PROVIDER)' > $(BATCH_INPUT)
	$(WITH_DETECTOR) studio run classify-batch --input-file $(BATCH_INPUT) $(LIVE_PROVIDER_FLAG)
	@$(PYTHON) scripts/show_report.py

mailbox-full: audit-gate ## Fetch -> classify -> apply under one Studio run id (APPLY=true to mutate) (STU-846)
	@mkdir -p $(dir $(MAILBOX_INPUT))
	@printf 'query: "%s"\nmax_results: %s\nprovider: %s\napply: %s\n' \
		'$(QUERY)' '$(MAX)' '$(LIVE_PROVIDER)' '$(APPLY)' > $(MAILBOX_INPUT)
	$(WITH_DETECTOR) studio run mailbox-full --input-file $(MAILBOX_INPUT) $(LIVE_PROVIDER_FLAG)

# What a scheduler runs for a mailbox with no push path (STU-2069). Filing first is what
# lets the gate pass: the previous poll's report is filed as unreviewed, not waved through.
mailbox-poll: audit-file ## Scheduled mailbox-full with APPLY=true; files the previous polls first (cron)
	$(MAKE) mailbox-full APPLY=true EMAIL_CLASSIFIER_TRIGGER=schedule

redraft:             ## Delete the draft on one conversation and write a fresh one (LINK=<outlook link>)
	@test -n "$(LINK)" || { echo "[ERROR] pass LINK=<outlook web link to the conversation>"; exit 1; }
	$(PYTHON) scripts/redraft.py "$(LINK)" --out $(REDRAFT_INPUT) $(LIVE_PROVIDER_FLAG)
	$(WITH_DETECTOR) studio run email-draft --input-file $(REDRAFT_INPUT) $(LIVE_PROVIDER_FLAG)

run-fetch: audit-gate ## Read-only fetch then end-to-end run
	$(WITH_DETECTOR) $(PYTHON) run_email.py --fetch --query "$(QUERY)" --max-results $(MAX) $(LIVE_PROVIDER_FLAG)

fetch:               ## Fetch only -> write the JSON dump (STU-391)
	$(PYTHON) scripts/fetch_gmail.py --query "$(QUERY)" --max-results $(MAX) --output $(DUMP)

apply-plan: require-run-id  ## Dry-run a report against the mailbox — no mutation (RUN_ID=...)
	$(PYTHON) apply_email.py run reports/$(RUN_ID).json

apply: require-run-id       ## Apply a report for real — mutates the mailbox (RUN_ID=...)
	$(PYTHON) apply_email.py run reports/$(RUN_ID).json --apply

undo: require-run-id        ## Revert an applied run from its journal (RUN_ID=...)
	$(PYTHON) apply_email.py undo $(RUN_ID)

fetch-outlook:       ## Fetch only from Outlook -> write the JSON dump (STU-325)
	$(PYTHON) scripts/fetch_outlook.py --query "$(QUERY)" --max-results $(MAX) --output $(OUTLOOK_DUMP)

outlook-consent:     ## Consent to Mail.ReadWrite so an Outlook apply run passes preflight (STU-844)
	$(PYTHON) scripts/outlook_consent.py

gmail-consent:       ## Re-consent a revoked or expired Gmail grant on the host (STU-948)
	$(PYTHON) scripts/gmail_consent.py

eval-fixtures:       ## Replay the corpus through stage 1 and diff against golden (STU-816)
	$(PYTHON) -m email_classifier.eval run --corpus $(CORPUS) --golden $(GOLDEN)

eval-seed:           ## Freeze the corpus's current stage-1 verdicts as golden labels
	$(PYTHON) -m email_classifier.eval seed --corpus $(CORPUS) --out $(GOLDEN)

eval-threads:        ## Replay the conversation corpus and diff against its golden (STU-936)
	$(PYTHON) -m email_classifier.eval run --corpus $(THREAD_CORPUS) --golden $(THREAD_GOLDEN)

eval-threads-seed:   ## Re-freeze the conversation corpus's message labels; thread labels are carried
	$(PYTHON) -m email_classifier.eval seed --corpus $(THREAD_CORPUS) --out $(THREAD_GOLDEN)

eval-seeded:         ## Replay the seeded Outlook dump against the golden `make seed` froze (STU-848)
	$(PYTHON) -m email_classifier.eval run --corpus $(SEED_DUMP) --golden $(SEED_GOLDEN)

eval-parity:         ## Round-trip the corpus through both provider projections and diff them (STU-848)
	$(PYTHON) -m email_classifier.eval parity --corpus $(CORPUS)

eval-spans:          ## Diff the person spans the detector finds in the seed corpus against golden (STU-925)
	$(PYTHON) -m email_classifier.eval spans --corpus $(SEED_CORPUS) --golden $(SPAN_GOLDEN)

eval-spans-seed:     ## Re-freeze the golden span set from what the detector finds now
	$(PYTHON) -m email_classifier.eval spans --corpus $(SEED_CORPUS) --golden $(SPAN_GOLDEN) --seed

eval-claims:         ## Diff what claims a span against the freeze the published rate cites (STU-1054)
	$(PYTHON) -m email_classifier.eval spans --corpus $(CLAIM_CORPUS) --golden $(CLAIM_GOLDEN)

eval-claims-seed:    ## Re-freeze it; the deployment's published rate then needs re-measuring
	$(PYTHON) -m email_classifier.eval spans --corpus $(CLAIM_CORPUS) --golden $(CLAIM_GOLDEN) --seed

qa-bakeoff:          ## Score two draft-qa models on one run's recorded drafts (STU-1014)
	@test -n "$(JOURNAL)" || { echo "[ERROR] pass JOURNAL=.studio/runs/<run>.jsonl"; exit 1; }
	$(WITH_DETECTOR) $(PYTHON) scripts/qa_bakeoff.py $(JOURNAL) \
		--baseline $(QA_BASELINE) --candidate $(QA_CANDIDATE) \
		$(if $(LIMIT),--limit $(LIMIT)) -o $(QA_BAKEOFF_OUT)

qa-gate:             ## Replay the frozen known-bad drafts: any verdict but `rejected` fails (STU-1078)
	$(WITH_DETECTOR) $(PYTHON) scripts/qa_gate.py --corpus $(QA_GATE_CORPUS) -o $(QA_GATE_OUT)

qa-gate-check:       ## Ask whether the recorded gate still describes this tree's reviewer, without calling one (STU-1086)
	$(PYTHON) scripts/qa_gate.py --check --corpus $(QA_GATE_CORPUS)

qa-erosion:          ## Does the reviewer still reject on the group's later passes? (STU-1104)
	$(WITH_DETECTOR) $(PYTHON) scripts/qa_erosion.py --corpus $(QA_GATE_CORPUS) \
		--repeats $(REPEATS) -o $(QA_EROSION_OUT)

qa-live-sources:     ## Does a frozen rejection turn on a live source, or on its prose? (STU-1114)
	$(WITH_DETECTOR) $(PYTHON) scripts/qa_live_sources.py --corpus $(QA_GATE_CORPUS) \
		--repeats $(REPEATS) -o $(QA_LIVE_SOURCES_OUT)

qa-coverage:         ## Did the bodies a run shipped answer what their senders asked? (STU-1140)
	@test -n "$(JOURNAL)" || { echo "[ERROR] pass JOURNAL=.studio/runs/<run>.jsonl"; exit 1; }
	$(WITH_DETECTOR) $(PYTHON) scripts/qa_coverage.py $(JOURNAL) $(PROVIDER_FLAG) \
		--repeats $(REPEATS) $(if $(LIMIT),--limit $(LIMIT)) -o $(QA_COVERAGE_OUT)

qa-coverage-gate:    ## The frozen coverage reads the reader must keep returning (STU-1151)
	$(WITH_DETECTOR) $(PYTHON) scripts/qa_coverage_gate.py --corpus $(COVERAGE_CORPUS) \
		$(PROVIDER_FLAG) --repeats $(REPEATS) -o $(QA_COVERAGE_GATE_OUT)

qa-coverage-gate-check: ## Ask whether the recorded gate still describes this tree's coverage reader, without calling one (STU-1156)
	$(PYTHON) scripts/qa_coverage_gate.py --check --corpus $(COVERAGE_CORPUS)

pilot-delta:         ## Draft-versus-sent across the whole pilot, cohorted by serving commit (STU-1182)
	$(PYTHON) scripts/pilot_delta.py $(if $(REPORTS),--reports $(REPORTS)) \
		$(if $(RUNS_DIR),--runs-dir $(RUNS_DIR)) $(if $(OFFLINE),--offline) \
		-o $(PILOT_DELTA_OUT)

pilot-gate:          ## Read the three V1 exit conditions off the window's artifacts (STU-1242)
	$(PYTHON) scripts/pilot_gate.py $(if $(REPORTS),--reports $(REPORTS)) \
		$(if $(RUNS_DIR),--runs-dir $(RUNS_DIR)) $(if $(SINCE),--since $(SINCE)) \
		$(if $(UNTIL),--until $(UNTIL)) $(if $(DAYS),--days $(DAYS)) \
		$(if $(OFFLINE),--offline) -o $(PILOT_GATE_OUT)

draft-bakeoff:       ## Score two drafting models on one run's recorded conversations (STU-1073)
	@test -n "$(JOURNAL)" || { echo "[ERROR] pass JOURNAL=.studio/runs/<run>.jsonl"; exit 1; }
	$(WITH_DETECTOR) $(PYTHON) scripts/draft_bakeoff.py $(JOURNAL) \
		--baseline $(DRAFT_BASELINE) --candidate $(DRAFT_CANDIDATE) $(PROVIDER_FLAG) \
		$(if $(CANDIDATE_PROVIDER),--candidate-provider $(CANDIDATE_PROVIDER)) \
		--repeats $(REPEATS) $(if $(LIMIT),--limit $(LIMIT)) $(if $(TIMEOUT),--timeout $(TIMEOUT)) \
		$(if $(DRAFT_BAKEOFF_OUT),-o $(DRAFT_BAKEOFF_OUT))

classify-bakeoff:    ## Score two classifier models on the deployment's escalated fixtures (STU-1045)
	$(PYTHON) scripts/classify_bakeoff.py $(PROVIDER_FLAG) \
		--corpus $(SEED_CORPUS) --golden $(STAGE2_GOLDEN) \
		--baseline $(CLASSIFY_BASELINE) --candidate $(CLASSIFY_CANDIDATE) \
		--repeats $(REPEATS) $(if $(LIMIT),--limit $(LIMIT)) $(if $(TIMEOUT),--timeout $(TIMEOUT))

eval-ab:             ## A/B the anonymized arm against cleartext on the escalated subset (STU-837)
	$(PYTHON) -m email_classifier.eval ab --corpus $(CORPUS) $(PROVIDER_FLAG) --repeats $(REPEATS)

voice-counts:        ## The counts behind club-voice.skill.md, per reply and per typed occurrence (STU-1138)
	$(PYTHON) scripts/voice_counts.py $(if $(JSON),--json)

memory-harvest:      ## Propose the blocks pasted across Sent Items since SINCE=<YYYY-MM-DD> as club-memory facts (STU-2001)
	@test -n "$(SINCE)" || { echo "[ERROR] pass SINCE=<YYYY-MM-DD>"; exit 1; }
	$(PYTHON) scripts/memory_harvest.py harvest --since $(SINCE)

memory-harvest-approve: ## Write the candidates marked approve: true into club-memory/ (STU-2001)
	$(PYTHON) scripts/memory_harvest.py approve

seed:                ## Seed the Outlook test mailbox with synthetic mail (COUNT=, MIX=, ADD=1) (STU-845)
	$(PYTHON) scripts/seed_outlook.py seed $(if $(COUNT),--count $(COUNT)) $(if $(ADD),--add) \
		--mix "$(MIX)" \
		--corpus $(SEED_CORPUS) --golden $(SEED_GOLDEN) --dump $(SEED_DUMP)

seed-threads:        ## Seed the conversation corpus as real reply chains (COUNT= conversations, ADD=1) (STU-942)
	$(PYTHON) scripts/seed_outlook.py seed --threads $(if $(COUNT),--count $(COUNT)) $(if $(ADD),--add) \
		--corpus $(THREAD_CORPUS) --golden $(SEED_GOLDEN) --dump $(SEED_DUMP)

seed-reply:          ## Answer a seeded message from the mailbox, superseding its draft (MESSAGE=) (STU-964)
	@test -n "$(MESSAGE)" || { echo "[ERROR] pass MESSAGE=<mailbox id of the seeded message it answers>"; exit 1; }
	$(PYTHON) scripts/seed_outlook.py seed-reply --message "$(MESSAGE)"

deliver:             ## Send the corpus to the test mailbox as real SMTP mail (COUNT=, MIX=) (STU-934)
	$(PYTHON) scripts/deliver_fixtures.py deliver $(if $(COUNT),--count $(COUNT)) \
		--mix "$(MIX)" --corpus $(SEED_CORPUS) --dump $(DELIVERED_DUMP)

deliver-threads:     ## Deliver the conversation corpus as real SMTP reply chains (COUNT= conversations) (STU-968)
	$(PYTHON) scripts/deliver_fixtures.py deliver --threads $(if $(COUNT),--count $(COUNT)) \
		--corpus $(THREAD_CORPUS) --dump $(DELIVERED_DUMP)

deliver-compare:     ## Diff a delivered round against its seeded counterpart (STU-934)
	$(PYTHON) scripts/deliver_fixtures.py compare \
		--delivered $(DELIVERED_DUMP) --seeded $(SEED_DUMP)

export-review:       ## Record which folder a human left each seeded message in (STU-875)
	$(PYTHON) scripts/seed_outlook.py export-review --review-dump $(REVIEW_DUMP)

unseed:              ## Delete every seeded message (RUN= one round, FORCE=1 a reviewed one)
	$(PYTHON) scripts/seed_outlook.py teardown --review-dump $(REVIEW_DUMP) \
		--golden $(SEED_GOLDEN) --dump $(SEED_DUMP) \
		$(if $(RUN),--run $(RUN)) $(if $(FORCE),--force)

unseed-force:        ## Delete a reviewed round, past the guard -- read the review dump first (STU-875)
	$(MAKE) unseed FORCE=1

serve-webhook:       ## Push endpoints (classify on new-mail notifications, Gmail + Outlook)
	$(PYTHON) -m email_classifier.webhook.server

renew-watch:         ## Register/renew Gmail push notifications (cron weekly)
	$(PYTHON) scripts/renew_watch.py

renew-graph:         ## Create/renew the Outlook Graph subscription (cron daily)
	$(PYTHON) scripts/renew_graph_subscription.py

renew-graph-recreate: ## Replace it with one carrying the current GRAPH_CLIENT_STATE (rotation)
	$(PYTHON) scripts/renew_graph_subscription.py --recreate

# SOURCE names what is asking, and the journal line carries it so the pilot gate can
# read the hourly runs apart from a hand-run (STU-1082). HEALTH_JOURNAL sends a drill
# to a throwaway path -- and not `JOURNAL`, which this file already spends on the
# Studio run log `make qa-bakeoff` replays. VERDICT=1 prints the probe's own exit on
# stdout and exits 0, for deploy-webhook: make exits 2 on any failed recipe, so the
# sub-make's own exit cannot carry the probe's code (STU-2044).
check-webhook:       ## Alert if the webhook stopped receiving notifications (cron hourly with SOURCE=cron; WAIT= seconds to let it come up; HEALTH_JOURNAL= a throwaway path for a drill)
	$(PYTHON) scripts/check_webhook_health.py $(if $(WAIT),--wait $(WAIT)) \
		$(if $(SOURCE),--source $(SOURCE)) $(if $(HEALTH_JOURNAL),--journal $(HEALTH_JOURNAL)) \
		$(if $(VERDICT),>&2; echo $$?)

check-unverdicted:   ## Alert on Inbox mail no report holds a verdict for, since UNVERDICTED_SINCE (cron hourly with SOURCE=cron; UNVERDICTED_JOURNAL= a throwaway path for a drill) (STU-1502)
	$(PYTHON) scripts/check_unverdicted.py $(if $(SOURCE),--source $(SOURCE)) \
		$(if $(UNVERDICTED_JOURNAL),--journal $(UNVERDICTED_JOURNAL))

# `id -un` until STU-1116, which made whoever ran the install the author of every
# agent prompt: under STUDIO_PROVIDER=claude-code each call is a `claude` process, and
# the CLI reads that account's ~/.claude -- global CLAUDE.md, auto-memory, settings --
# into its system prompt. On the serving host that was 4955 bytes of homelab notes reaching the
# agents drafting replies to parents. Named rather than sniffed, so the guarantee does
# not depend on who typed the command.
#
# The target refuses an account with a login shell rather than warning about one: a
# login shell is the half of "is this a person's home" a machine can answer, and the
# unit it writes is the mechanism -- one installed under a human account is the very
# failure it exists to prevent.
SERVICE_USER ?= email-classifier

# A deploy is an operator action: it needs sudo for systemd, and the service account is
# never given any -- that would hand whoever holds the mailbox grant a path to root. But
# every write into the checkout has to land as the account that serves it, or the next
# restart runs on files the service cannot read. So the two halves are split rather than
# the whole target being run as one user or the other. `?=`, so a host that already runs
# the deploy as that account can empty it.
AS_SERVICE ?= sudo -u $(SERVICE_USER)

install-webhook-service: ## Install and start the systemd unit for this checkout (needs sudo)
	@getent passwd $(SERVICE_USER) >/dev/null || { \
		echo "install-webhook-service: no account '$(SERVICE_USER)' on this host. Create it first:"; \
		echo "  sudo useradd --system --create-home --home-dir /var/lib/email-classifier \\"; \
		echo "               --shell /usr/sbin/nologin $(SERVICE_USER)"; \
		exit 1; }
	@case "$$(getent passwd $(SERVICE_USER) | cut -d: -f7)" in \
		*/nologin|*/false) ;; \
		*) echo "install-webhook-service: '$(SERVICE_USER)' has a login shell, so its ~/.claude is an operator's and the CLI would read it into every agent prompt (STU-1116). Use a --shell /usr/sbin/nologin account."; \
		   exit 1;; \
	esac
	sed -e 's|@USER@|$(SERVICE_USER)|' -e 's|@CHECKOUT@|$(CURDIR)|' -e 's|@PYTHON@|$(abspath $(PYTHON))|' \
		deploy/email-classifier-webhook.service \
		| sudo tee /etc/systemd/system/email-classifier-webhook.service >/dev/null
	sudo systemctl daemon-reload
	sudo systemctl enable --now email-classifier-webhook
	systemctl status email-classifier-webhook --no-pager

# The dispatch picks the machine and writes nothing on either: WEBHOOK_SSH_HOST set
# means the webhook serves elsewhere, so the update happens there. Unset, it asks the
# unit whether this checkout is the one it serves rather than assuming, because a
# workstation carrying a stale unit is a real machine here (STU-1116 predates it).
# The reason it hands the far side `pull-webhook-here` and never `pull-webhook` is in
# the script.
pull-webhook:        ## Update the serving checkout and restart the webhook -- on the serving host, or through WEBHOOK_SSH_HOST
	@scripts/pull_webhook.sh

# Make parsed this file before the pull ran, so a deploy step written inline here
# would be the one the *previous* commit shipped -- a step added by the pull would
# silently not run, and the target would report success (STU-1068). The sub-make is
# a second make process, which reads the Makefile the pull just wrote.
# The reconcile is not a `checkout --`: the host writes audits/skipped.json where the
# report lives and the workstation commits the same entry, so the working copy the pull
# aborts on is usually the merge's own content -- and sometimes is not (STU-1081). It
# fetches, so the merge below is of the version it compared against.
pull-webhook-here:   ## The serving host's half of pull-webhook, which dispatches here (run as the operator)
	$(AS_SERVICE) $(PYTHON) scripts/reconcile_audits.py
	$(AS_SERVICE) git merge --ff-only FETCH_HEAD
	# The reconcile discards the entries this host filed for itself, because they are
	# derived from reports still on disk (STU-1084). Written back here rather than left
	# to the next notification: between the two, `coverage` on this host reads a gap
	# that is not one, and a quiet mailbox makes that window a long one. A sub-make for
	# the reason deploy-webhook is one -- this recipe was parsed before the pull.
	$(AS_SERVICE) $(MAKE) audit-file
	$(MAKE) deploy-webhook

deploy-webhook:      ## Reinstall, restart and verify the webhook on the checkout as it stands (run as the operator)
	# A deployment pins the kernel as a submodule: the pin the merge brought, checked out
	# before anything installs from it. A no-op where the kernel is this checkout.
	$(AS_SERVICE) git submodule update --init
	$(AS_SERVICE) $(PYTHON) -m pip install -e "$(KERNEL)[webhook,fetch-outlook,ner]"
	$(MAKE) install-webhook-service
	sudo systemctl restart email-classifier-webhook
	# Exit 1 is a quiet mailbox, which says nothing about the deploy; 6 and 7 are the
	# client secret's expiry and a missing expiry date (STU-1095), which say nothing
	# about it either -- and blocking on 6 would block the deploy that carries the
	# fix. 10 is an audit entry about to lose its artifacts (STU-1168) and 8 a drift
	# leg blind for several hourly runs, neither about this deploy (STU-2044). 1, 6,
	# 7 and 10 also outrank the drift leg in the probe, so like 8 they leave this
	# deploy unverified rather than failed. What this verifies is 3 -- serving code
	# `main` has moved past, which is what skipping the restart looks like
	# (STU-1017). The hourly cron is what reports the rest, which is where those
	# codes are aimed.
	# WAIT: the unit is Type=simple, so systemd returned as soon as the process
	# forked -- seconds before it binds. Without it the check races the restart and
	# reports the endpoint dead.
	# And `sudo` exits 1 when *sudo itself* fails -- no sudoers entry, an expired
	# timestamp with no tty, a password prompt it cannot ask -- which is the same 1
	# tolerated above as a quiet mailbox (STU-1122). So a deploy whose verification
	# never ran would report success: the exact silence STU-1017 added the exit-3 leg
	# to break. Nothing fires it today only because the restart above is not tolerated
	# and fails first -- a real protection, and an ordering nothing asserts.
	# So the sub-make reports the probe's exit on stdout (VERDICT=1) while everything
	# else goes to stderr: a number is the check's verdict, and no number at all is a
	# privilege drop that never got that far. Probing `sudo -n true` first was the
	# other candidate and answers a different question -- whether sudo worked a
	# moment ago, for a command that is not this one.
	$(AS_SERVICE) $(MAKE) -s --no-print-directory check-webhook WAIT=60 SOURCE=deploy VERDICT=1 \
		| { read verdict || verdict=""; \
		    case "$$verdict" in \
			0|1|6|7|8|10) ;; \
			'') echo "deploy-webhook: the verification never ran -- '$(AS_SERVICE)' failed before reaching it, so nothing checked this deploy." >&2; exit 1;; \
			*) echo "deploy-webhook: check-webhook exited $$verdict, so this host is not serving the commit it was deployed from." >&2; exit 1;; \
		    esac; }

prune:               ## Plan the artifact prune — nothing is removed (STU-932)
	$(PYTHON) scripts/prune_artifacts.py -v

recent-mail:         ## What the push path decided lately, one line per conversation (HOURS=24; run it where reports/ lives)
	$(PYTHON) scripts/recent_mail.py --hours $(or $(HOURS),24)

prune-apply:         ## Remove expired run artifacts, keeping everything the audit log points at (cron daily)
	$(PYTHON) scripts/prune_artifacts.py --apply --quiet

promote:             ## Promote uniform LLM verdicts into rules/learned.yaml (STU-822)
	$(PYTHON) scripts/promote_rules.py

# The reinstall trigger is a copy of pyproject.toml inside the venv: dependencies
# change there and nowhere else, and comparing the file itself needs no hashing tool
# and no timestamp that a checkout reorders.
test-venv:           ## Create/refresh the ner-free test venv `make test` runs in (STU-1059)
	@if [ ! -x $(TEST_PYTHON) ]; then \
		echo "[test] creating $(TEST_VENV)"; \
		$(BASE_PYTHON) -m venv $(TEST_VENV); \
	fi
	@if ! cmp -s $(KERNEL)/pyproject.toml $(TEST_VENV)/pyproject.toml; then \
		echo "[test] installing $(KERNEL)[dev,webhook] into $(TEST_VENV) (no ner extra)"; \
		$(TEST_PYTHON) -m pip install -q --disable-pip-version-check -e "$(KERNEL)[dev,webhook]" && \
		cp $(KERNEL)/pyproject.toml $(TEST_VENV)/pyproject.toml; \
	fi

test: test-venv      ## The suite CI gates on: [dev,webhook], no `ner`, -n auto (STU-1059)
	$(TEST_PYTHON) -m pytest -q -n auto $(PYTEST_ARGS)

# The other suite, and the only one that proves a *detected span*: the ~40 tests that
# assert on a name GLiNER found. It wants $(PYTHON), the interpreter Setup installs the
# extra into, and it costs minutes. Reach for it for a change that can move a span —
# the list is the `paths` filter of tests-ner.yml.
# The card is the other memory, and the smaller one. Serial this is one 1.29 GB copy on a
# 6 GB card, but `PYTEST_ARGS=-n auto` is a worker per core and they race: the fallback of
# STU-984 covers the *load* only, so an inference-time `torch.OutOfMemoryError` has nowhere
# to fall. Measured at ten workers, five detector tests failed that pass alone (STU-1052).
# The GPU is an optimization and never a requirement, so the suite declines it outright.
test-ner:            ## Same suite on the extra-carrying interpreter, person detection included
	@$(PYTHON) -c "import gliner" 2>/dev/null || { \
		echo "[ERROR] no interpreter with the ner extra (looked at '$(PYTHON)')."; \
		echo "        Without gliner those ~40 tests skip, and a green pass missing them"; \
		echo "        looks exactly like a green pass. Install it (pip install -e '.[ner]')"; \
		echo "        or run \`make test\`, which is the suite CI gates on."; \
		exit 1; }
	CUDA_VISIBLE_DEVICES= $(PYTHON) -m pytest $(PYTEST_ARGS)

clean:               ## Remove disposable run artifacts — keeps the durable stores under logs/
	rm -rf reports/
	rm -f logs/frontier.jsonl

reset-state:         ## Destroy the promotion corpus and the application journals — undo becomes impossible (CONFIRM=1)
	@test "$(CONFIRM)" = "1" || { echo "[ERROR] refusing to wipe logs/verdicts.jsonl and logs/applied/ — pass CONFIRM=1"; exit 1; }
	rm -f logs/verdicts.jsonl
	rm -rf logs/applied/
