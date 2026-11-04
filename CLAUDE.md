# email-tool-by-studio

The email classifier kernel: a deterministic rule engine (stage 1), an LLM stage 2 driven
by Studio pipelines, and a reply drafter reviewed by a second agent. This file describes
the kernel's mechanisms and the reasoning behind them. Everything about one mailbox (its
rules, policy, agents' prose and skills, fixtures, frozen evaluation records and the
measurements taken on it) lives in that deployment's repository; `reference-deployment/`
is the fictional one the suite runs against, and `README.md` explains how the kernel finds
a deployment (`EMAIL_CLASSIFIER_DEPLOYMENT_ROOT`).

Measurements quoted here as "measured on a production deployment" were taken on a real
mailbox the kernel serves; the deployment that took them keeps its own record of them.

## Real data stays out of this repository

This repository is public, and a pushed commit stays reachable by SHA after a squash
(STU-2077). A test, fixture, commit message or PR body uses invented values, never a
name, address, team or timestamp copied from an issue, a report or a real mailbox. On
2026-11-03 two PRs shipped a real sender name and a real team name that way.

`make hooks` installs the mechanism (STU-2078): `scripts/check_real_data.py` refuses a
commit whose added lines or message carry a term the deployments own. The terms never
enter this repository: `~/.config/email-classifier/real-data` lists, one path per line,
each deployment's keymap (`logs/anonymization/keymap.json`, its values) and any plain
term list (team names, hosts), and a hit names the file, the line and the keymap token,
never the term. A `!term` line there drops a keymap value that names nobody (the
detector's false positives land in the keymap too). It fails closed: an installed hook with no config, an unreadable source
or no term at all refuses the commit, since a guard that read nothing passes everything.
A machine that never ran `make hooks` (CI, a cloud session) has no guard, which is why
the rule above is written down as well.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,webhook]"
pip install -e ".[ner]"   # name detection (GLiNER + torch), required as soon as
                          # anonymization.scope is set in the deployment's policy.yaml
```

The `ner` extra is heavy and optional: the suite runs without it (STU-857), but a run
that sends mail to an LLM under a non-empty `scope` stops dead when it is missing. The
tokenizer refuses rather than leaving without a name detector (STU-890). The refusal
lands at preflight, before the fetch and before a run id is opened (STU-908); the
tokenizer's `RuntimeError` stays as the safety net.

"Runs without it" is a claim, and the `hermetic` job of `.github/workflows/tests.yml`
now tests it on every push: it never installs the extra, so the ~40 name-detection tests
skip and the rest must pass. It is also what `make test` does locally since STU-1059, in
its own venv (see "Which tests to run on a branch" below). The command below stays
written here because it tests something narrower than both (STU-994): a venv that has
only `pytest pyyaml jsonschema`, no flask and nothing else. It must exit 0, with no error
and no failure, knowing that it also skips the seven `test_webhook_*` modules, 154 tests
that neither it nor its venv runs, and that this is exactly its value: it answers "does
the suite start with nothing else", not "is the suite whole":

```bash
python -m venv /tmp/hermetic && /tmp/hermetic/bin/pip install pytest pyyaml jsonschema
PYTHONPATH=$PWD /tmp/hermetic/bin/python -m pytest -q
```

What holding it costs: three stubs, in `tests/conftest.py` so that a copy that drifts
cannot stop representing the real thing. `stub_detector` (the model, for a test whose
subject is not who is in the text), `pin_gliner` (the import that `missing_detector`
makes, in both directions) and `requires_gliner` (the skip, for a test that asserts on a
detected name). The last is a marker and not a `skipif`, skipped by
`pytest_runtest_setup`: the condition probes the model as well as the package (the extra
installed with the weights unreachable made these tests fail on a machine that cannot run
them, STU-992), and a `skipif` probe would be evaluated at collection, so running a
single test file would pay 1.3 GB of weights for tests it does not select. The fourth is
local to `tests/test_anonymize.py`: `huggingface_hub` is not a project dependency (it
arrives transitively through `gliner`), so the loading-path tests stub it, but **only
when the extra is absent**. On a machine that has it they read the real module:
otherwise a rename like the one STU-990 had to measure `LocalEntryNotFoundError` against
would pass against a stub that agrees with the old name.

## Commands

```bash
make test       # the suite CI guards: [dev,webhook], no `ner`, -n auto (STU-1059)
make test-ner   # the same, on the interpreter that carries the extra: the ~40 span tests
make run        # end-to-end run orchestrated by Python
make run-batch  # same run, orchestrated by Studio (fan-out over the grey zone)
make mailbox-full  # fetch -> classify -> apply -> drafts -> calendar under a single Studio run id
make mailbox-full PYTHON=.venv-3.12/bin/python  # a venv somewhere other than .venv/
make redraft LINK='<Outlook link to the thread>'  # deletes the thread's draft, writes a new one
make deliver       # sends the corpus into the test mailbox over real SMTP (STU-934)
make deliver-threads  # same, but the thread corpus as real reply chains (STU-968)
make seed-threads  # seeds the thread corpus as real reply chains (STU-942)
make seed-reply MESSAGE=<id>  # replies to a seeded message from the mailbox (STU-964)
make audit-file  # files the entry every unfiled push run already determines (STU-1084)
make audit-skip RUN_ID=<id> REASON='...'  # throwaway run, not auditable (STU-900)
make qa-bakeoff JOURNAL=.studio/runs/<run>.jsonl  # two draft-qa models, one corpus (STU-1014)
make classify-bakeoff  # two classifier models, the escalated fixtures (STU-1045)
make qa-gate    # the frozen drafts the reviewer must still reject (STU-1078)
make qa-gate-check  # has the reviewer moved since? seconds, no call, on any tree (STU-1086)
make qa-erosion  # does the reviewer hold in the group's late passes? (STU-1104)
make qa-live-sources  # does a frozen entry hold on its prose, or on a live source? (STU-1114)
make qa-coverage JOURNAL=.studio/runs/<run>.jsonl  # does the delivered body answer what was asked? (STU-1140)
make qa-coverage-gate  # the frozen readings the reader must still give (STU-1151)
make qa-coverage-gate-check  # has the reader moved since? seconds, no call, on any tree (STU-1156)
make draft-bakeoff JOURNAL=.studio/runs/<run>.jsonl  # two drafter models, one corpus (STU-1073)
make eval-claims   # does the same code still claim the same spans? needs the detector (STU-1054)
make pilot-gate    # a deployment's exit conditions, read off the artifacts (STU-1242)
make pilot-gate OFFLINE=1  # the same, without a Graph call: is there a denominator
make correction-rules  # candidate drafting rules from the reviewer's corrections (STU-1108)
make memory-harvest SINCE=<YYYY-MM-DD>  # pasted answer blocks proposed as club-memory facts (STU-2001)
make memory-harvest-approve  # writes the candidates marked approve: true into club-memory/
make prune         # purge plan for run artifacts, nothing is deleted (STU-932)
make prune-apply   # the cron line: purges for real, silent when there is nothing
make commitments   # promises drafted replies made that nobody settled (STU-1065)
make commitment-settle ID=<id> NOTE='...'  # a human did it, or decided nothing is owed
```

`make redraft` takes up a single thread, named by the link the web mailbox gives:
`scripts/redraft.py` resolves the link, deletes the draft in place (journalled
`deleted`, so `undo` does not offer it again) and writes the item; the Makefile chains
`studio run email-draft` on it, the same leaf as the fan-out. Deleting rather than
rewriting the body: what is wrong with a draft to redo is often what `createReply` put
around it (recipient, quote, stale edit). The cost is real: a drafting run that dies
afterwards leaves the thread with no draft.

`make run`, `run-batch`, `run-fetch` and `mailbox-full` depend on `audit-gate`: a run does
not start while a *previous* run has a report with no entry in `audits/audit_log.json`
(STU-900). Before and not after, because a run's audit cannot exist while it runs; what
the gate holds back is therefore the previous one. The way out is `audits/skipped.json`,
committed beside the log: `make audit-skip` writes `{run_id, date, reason}` there and
`coverage` counts the run as covered. The reason is mandatory, and an entry without one
excuses nothing: otherwise the marker would be the same silence in a second file.
`audit-gate` tolerates an empty `reports/`, which `coverage` refuses elsewhere: the gate
does not ask whether runs are audited but whether a new one may start, and before the
first there is nothing to hold back.

The gate fills nothing in, and that is what made it blocking rather than shrinking
(STU-1084): six issues (STU-841, 860, 897, 921, 1038, 1071) were the same human reading
push-path reports and copying entries by hand, at a rate set by incoming mail. A push
run therefore now writes its own entry (`email_classifier/audit_filing.py`, called after
every webhook run and by `make audit-file`), with everything its artifacts determine:
the report's counters, the violations from `check_report`, `check_log_consistency` and
`check_cache_hits`, the frontier log's `policy_downgrades`. What is not there is what
nobody has read: `verdict` carries the `UNREVIEWED` sentinel, and `coverage` counts the
entry as covered (so the gate no longer holds back) while printing `UNREVIEWED: <run-id>`
and the total. That total *is* the debt: a filed entry that said nothing would be exactly
the silence `audit-skip` refuses. The sentinel matches as a prefix and not by equality: a
reviewer who writes under it instead of replacing it still owes the reading, the
direction in which an audit must fail.

Only the push path files. A `make run` is someone at the keyboard who is already
watching, and the test is the report's `provenance.input_path` (`LIVE_INPUT`, STU-859),
not the machine the command runs on, so the same command is safe everywhere.
A scheduled run files too (STU-2069): an IMAP mailbox has no push path, so a deployment
polls it from cron with `make mailbox-poll`, which files the previous polls and then runs
`mailbox-full APPLY=true` under `EMAIL_CLASSIFIER_TRIGGER=schedule`. The report records
that as `provenance.trigger`, and `schedule` is filed like `LIVE_INPUT`.
`never_fires` and `unexercised_by_this_run` stay null with their `schema_note`: rule
staleness is a claim about the *rule set*, identical on every run that pinned it, and
copying the same two lists onto a hundred entries would be the same transcription done
by machine. The reviewer computes it once over the window with
`scripts/audit_run.py rules`. A filing failure is logged and swallowed: the report is
already on disk, the next run picks it up in passing, and a filing that never happens
falls back exactly onto today's blocking gate. The filing is in a `finally` and **after**
drafting, not before: the `drafts` leg only reaches `logs/drafts/stages.jsonl` once the
fan-out is done, so an entry filed earlier would read a cache ratio for the
classification leg alone, permanently, since the entry then covers the run. That is
precisely the leg STU-1057 exists for. An unreadable report is skipped, not raised: the
write is after the loop, so raising would file nothing at all, and the skipped report
stays unfiled, which is the gate holding back, where someone is watching.

Two consequences elsewhere, both necessary so that filing does not break what it
touches. `retention.audit_protection` now protects only a **reviewed** entry: the shelf
exists so that the evidence behind a *claim* stays re-derivable, and a filed entry claims
nothing. Counting it would freeze every shelf on the host on deployment day (one entry
per notification batch, each exempting its report, its dump and its apply journal from
every age), that is STU-932 undone by what was meant to hold it. The cost is named: a
filed run nobody reads within the report's 30 days loses the artifacts a reviewer would
have started from, and the entry is then all that remains. And `reconcile_audits` no
longer refuses on a filed, unreviewed entry (STU-1084): it is re-derived from a report
still on disk, so it is not lost work, and counting it would fail every pull whose merge
also touches the log, that is every pull that carries the verdicts a reviewer just
wrote. `pull-webhook` rewrites those entries after the merge (`$(MAKE) audit-file`)
rather than waiting for the next notification: in between, `coverage` on the host would
read a gap that is not one.

A second kind of reviewed verdict exists for a backlog too long to read one run at a
time: `Reviewed in window audit audits/windows/<file>`. It replaces the `UNREVIEWED`
sentinel on every entry the window covers, and `scripts/audit_run.py log-check` holds it
to its file (`audit.check_window_verdicts`): the named file must exist and must list the
entry's run id, so a window verdict pointing at nothing fails like the sentinel would. It
is appropriate when the question is about the window as a whole (coverage, integrity
checks, cost, recall across every report in it). What it does not cover is said in the
window file and here: it does not read each verdict for correctness, it does not judge a
single draft's quality, and it does not compute rule staleness per run (that stays
`scripts/audit_run.py rules`, once per window). The retention consequence is the one
every reviewed entry has: `retention.audit_protection` counts it, so the report, dump and
apply journal of every listed run are exempt from every age. A window review is
therefore also a decision to keep those artifacts, and listing a run in one freezes its
shelf entries until the entry changes.

The file therefore has two writers on the serving host: the webhook worker and
`make audit-file`, which `pull-webhook` launches while the first is still serving. The
write goes through a temporary file then `os.replace`: a reader landing in the middle of
a rewrite would read a truncated array, that is the audit trail destroyed rather than
delayed. Two competing writers can still cost one of them its entry; that one repairs
itself, its report staying uncovered and the next sweep filing it again. A half-written
file repairs nothing. Same rule on content: an entry's date is its report's mtime and
never today, or a week of catch-up runs would all be dated the day somebody noticed; and
a family of checks that did not run says so in the `schema_note` (frontier log absent,
unreadable or not naming this run, or a run that escalated without pricing anything, the
STU-1057 leg). `violations` is an integer in both cases, so only words distinguish the
skipped family from the passed one: that is the STU-912 rule, applied where no exit
code exists. The third silence is not a skipped check but a short figure (STU-1157): an
execution of the drafting leg killed before `drafts-post` records nothing, so the run's
spend and call count are floors and the entry says so. The report behind it expires at
30 days, after which the entry is all a reviewer has. The text is the one
`scripts/audit_run.py cost` prints, carried over from `email_classifier/audit.py` rather
than restated, and it is read off the stages log and not off the leg: a killed execution
priced nothing, so a run whose executions all died has no leg at all and would be filed
as a run that never drafted. The notes accumulate instead of replacing each other, since
a run can be silent in several ways at once.

This file makes a round trip between two machines, and that is what blocked deployment
(STU-1081): `audit-skip` runs on the host, where the report lives, and nothing commits it
there; the entry is then committed from the workstation. The host therefore keeps an
uncommitted modification that *is* the content of the merge, and `git pull` refuses it.
`scripts/reconcile_audits.py` runs before the pull of `make pull-webhook`: it compares
each dirty `audits/` file to the incoming version and discards the working copy only if
everything it carries is already there. An entry the pull does not bring stops the
deployment, naming its run id, which a `git checkout -- audits/skipped.json` cannot do,
and that is exactly the blind form an operator would otherwise have run on trust.

`PYTHON` (default: the local `.venv/bin/python`, otherwise the main checkout's) is
exported by the Makefile down to the Studio process, and passed on as
`STUDIO_PYTHON_BIN` (STU-1030): without it a script stage (draft writing is one now)
sniffs for a `.venv/` next to the cwd, and a worktree has none. One knob for a venv
outside `.venv/` (STU-856).

### Which tests to run on a branch (STU-1005)

The whole suite **without the `ner` extra** is 1539 tests in **32 s** serially and
**10 s** under `pytest -n auto` (`pytest-xdist`). That is no longer a wait: it is the
tight loop. Running the full suite on every commit is now the default, and picking files
by hand is no longer justified.

**That is `make test`** (STU-1059). The target holds the venv the rule asked for itself:
`.venv-test/` in the main checkout, created on first call, reused afterwards, and
installed with exactly what the `hermetic` job installs (`[dev,webhook]`, never `ner`).
So what a branch checks by default is the set of tests CI will run, in the same seconds.
Three details that are the target and not the rule: the venv lives in the main checkout
for the reason `PYTHON` falls back there (a fresh worktree has none, and one venv per
worktree would pay the install again on every branch); which *sources* run is still
answered by `PYTHONPATH` and not by the editable `.pth`, so a venv another worktree
installed still runs this one's code (STU-867); and the reinstall trigger is a copy of
`pyproject.toml` placed in the venv. Dependencies change there and nowhere else, and
comparing the file itself needs no hashing tool and no timestamp a checkout reorders.
`TEST_VENV=` moves it, `BASE_PYTHON=` picks the interpreter that builds it,
`PYTEST_ARGS=` passes any pytest flag, a file included.

What the target cannot remove, `tests/conftest.py` removes: the Makefile does
`-include .env` then `export`, so `make test` hands pytest every key of the machine's
`.env`. An autouse fixture clears everything `.env.example` names: the file the operator
fills in is the one the fixture reads, so a key added to one is cleared by the other,
where STU-1074 closed two by hand. Measured 2026-08-29 with a `.env` setting all 25 keys:
**92 tests** fail under the previous coverage, against 0 after. The direction is what
costs: a test that leans on an ambient value is green on the machine that wrote the
`.env` and red in CI, and that is what left `main` red for eighteen hours. A test that
needs a variable sets it itself.

**Never in `.venv/`**, and that is what the target takes out of hands. The interpreter
`$(PYTHON)` falls back to from a worktree is the main checkout's, and it carries the
extra: any module that touches a name loads GLiNER there. Measured 2026-08-28:
`tests/test_drafts.py` alone took 80 s there, against 5.4 s for the *whole* suite in a
venv without the extra; under `make test`, the same file in 0.8 s. That venv wants
`flask` and `pytest-xdist` on top of what the STU-994 command installs: without flask
the seven `test_webhook_*` modules skip as a block, and a green pass with 154 fewer tests
looks exactly like a green pass.

The extra is only worth bringing out for a change that can move a span (the list is the
`paths` filter of `.github/workflows/tests-ner.yml`), and that is `make test-ner`, the
same suite on `$(PYTHON)`. It **refuses** when that interpreter has no `gliner` rather
than letting the ~40 tests skip: it is the only target that exists for them, so a green
pass that did not run them would be the worst possible silence there.

`test-ner` declines the GPU (`CUDA_VISIBLE_DEVICES=`), which is the other memory and the
smaller one (STU-1052). Serially it is a 1.29 GB copy on a 6 GB card, but
`PYTEST_ARGS=-n auto` is one worker per core and they fight over it: the STU-984 fallback
only covers *loading*, so an inference `torch.OutOfMemoryError` has nowhere to land.
Measured at ten workers, five detection tests failed that pass alone. The GPU is an
optimization and never a requirement, and a suite that needed it would contradict the
rule. What it costs when the card would have helped: 201 s against 106 s. `pytest` run by
hand outside the target has no such guard, and the `-n auto` of a tight loop on that
interpreter is also what killed the suite with **exit 137** on 2026-08-28, with no
message, no partial output and no test named, each worker carrying its own detector at
~1.76 GB resident (STU-938).

The 828 s measured before were not spread "everywhere": they were concentrated in two
places, both measured since.

1. **Detector calls**: 400 real GLiNER calls over a full pass, 270 of them in
   `tests/test_anonymize.py` and 107 in `tests/test_eval_spans.py`. Nothing else in the
   suite costs that, and nothing else needs the extra.
2. **Real waits**: four tests set the debounce window to 5 s and paid it in real time,
   25 s of the suite spent sleeping. `tests/conftest.py` now states `BURST_WINDOW`,
   shared, because the value is arbitrary (a test's posts fit in microseconds) and the
   next one to pick one would reintroduce the cost.

So: the hermetic suite is fast, and the `ner` extra is what makes it slow. The two run in
two separate workflows, and that separation is what makes the first usable on every
commit.

`.github/workflows/tests.yml` (job `hermetic`) runs on every push and every PR: **46 s**
end to end on a GitHub runner, 23 s of it tests. `.github/workflows/tests-ner.yml` costs
**~7 min** (~2 min to install torch and the wheels it drags in, ~4 min of tests), so it
does not run on everything: a `paths` filter limits it to what can *move a span*, and
the rest of those files' logic already runs in `hermetic`, where only the name-detection
cases skip. Three other entry points: the weekly cron, the only net against STU-919 (a
drift without a line of the repo changing, which no path filter can see),
`workflow_dispatch` to run it by hand, and the path list itself, written twice because
GitHub Actions does not support YAML anchors. The two copies must stay identical.

There is CI now, and the point the earlier version of this section made still holds in
another form: what CI tests is what CI installs. The fast job installs `[dev,webhook]`,
**flask included**, deliberately. Without it the seven `test_webhook_*` modules skip as a
block, and a green pass with 137 fewer tests looks exactly like a green pass. That is
also why the hermetic command above (`pytest pyyaml jsonschema`) is not CI's command: it
proves the suite runs without the extra, not that it is whole.

And if waiting becomes the problem again, the answer is still parallelism, not less
coverage, but the `ner` job has none left to take, and that is measured (STU-1079,
STU-2031). The runner prints its cores and memory at the head of every run. Memory, not
cores, is the constraint. A third worker costs ~1290 MB, measured on the whole suite
locally with the GPU declined (2192 MB / 119 s at one worker, 2899 MB / 84 s at two,
4189 MB / 85 s at three): it returns one second. On a 2-core 8 GB hosted runner, 3
workers gave an intermittent exit 143; on a self-hosted runner shared with other
services, running out of memory means the OOM killer choosing among those services
instead of a throwaway VM dying. The job therefore stays at `-n 2`, chosen for the memory
of the machine it runs on and not for its cores.

What that 143 leaves in the log is the other half, and nothing placed *after* the stage
can fill it: a runner out of memory does not fail the job, it takes the runner down.
GitHub ends with `The runner has received a shutdown signal` and the job is
**cancelled**, so an `if: always()` stage does not run. Measured: a summary stage never
printed a line. Memory is therefore printed during the run, interleaved with the
progress dots, and the header goes out before pytest, the one line no death can swallow.
`--max-worker-restart=0` repairs none of this and is not in the job: xdist already names
the test of a killed worker, identical output with the flag and without it; what dies
here is the controller, not a worker.

### From a worktree

A freshly created worktree has no `.venv/` and does not need one: `make` falls back on the
main checkout's interpreter and points `PYTHONPATH` at the worktree. Targets therefore go
through `main`'s venv but run the branch's sources (STU-867). `make test` follows the same
fallback with its own venv, `.venv-test/`, which it creates in the main checkout on the
first call: from a fresh worktree the first pass pays a `pip install` and later ones pay
nothing (STU-1059).

```bash
make test       # the suite, from the worktree, with nothing installed by hand
```

Outside `make`, export `PYTHONPATH` yourself. A bare `pytest` or `python scripts/x.py`
silently imports the main checkout's `email_classifier/`: the editable install points
there, and nothing says so.

```bash
export PYTHONPATH=$PWD
$(git rev-parse --git-common-dir)/../.venv/bin/python -m pytest
```

`secrets/` and `.env` have no such fallback: they are gitignored and exist only in the
main checkout, so a task that fetches, seeds, applies or drafts has no token and no
`OUTLOOK_CLIENT_ID` without them. Link them at the start. `.gitignore` names those links
without a trailing slash (STU-961), the only form that matches a symlink: a directory
pattern does not, and `git add -A` used to commit the links.

When `.env` holds 1Password references (`op://...`) rather than values, a target that
reads a secret runs under `op run` (STU-2012):

```bash
op run --env-file .env -- make <target>
```

No `-e` is needed: the Makefile lets the environment win over `.env`. Every key that
`.env` names and the environment already carries is saved before the `-include` and put
back after it. Before that, the makefile assignment beat the environment, so `op run`
resolved every reference and the `-include` immediately rewrote it to the literal
`op://`: `make qa-gate` sent a Graph login to `.../op://Dev/...`, and only `make -e` got
through (measured 2026-11-02: without `-e` the variable arrives as `op://`, with `-e` it
arrives resolved). A key that still carries `op://` is refused at parse time, before any
recipe and so before any LLM or Graph call, with a message that names the command. Only
the targets that read no secret run without `op run`: `test`, `test-venv`, `test-ner`,
`clean`, the four `*-check` targets that compare a committed record, and `pull-webhook`,
whose workstation half only opens ssh to the serving host (`OFFLINE_GOALS`, STU-2066),
and `memory-harvest-approve`, which only writes local files.
A new target is refused until it is added there, which is the direction a forgotten entry
must fail in. A host whose `.env` holds plain values never trips the refusal.

`reports/`, `dumps/` and `logs/` are in the same position as `secrets/`, and the audit
commands need them: `scripts/audit_run.py` `coverage`, `metrics`, `check` and `rules` all
read a report, which exists only on the machine that produced the run; `drafts`
(STU-1029) is the only one that reads `logs/drafts/journal.jsonl` (what a run put into the
mailbox as drafts) and it refuses outright when the file is missing, because a run that
drafted nothing and a run whose journal stayed on another machine cannot otherwise be told
apart. Without the link, `coverage` does not announce a gap: it stops on
`no report found in reports`. With `logs/frontier.jsonl` absent, neither of the two
commands that read it pretends (STU-912): `metrics` prints `policy_downgrades: null` with
the reason, and `check` exits 2. An exit 0 on a skipped family of checks reads as a passed
audit, and a zero copied from a metric that was never computed is a false figure no shape
check catches. Only `log-check` works from `audits/audit_log.json` alone, which the
deployment commits, like `audits/skipped.json`, the durable record of a run someone
decided not to audit. `logs/` carries no `.gitkeep`: a tracked directory cannot be
replaced by a link, and `ln` used to put a `logs/logs` inside it. Every writer creates its
own parent directory.

`club-memory/` is the store of facts that `org_memory-read_memory` reads, gitignored like
the others, and its absence does not show (STU-1162): without it the tool **raises on
every call**, which stops no run, since an agent that loses a source goes on drafting. A
bake-off that ran with the store on one arm and without it on the other measured two
pipelines, not two models; the harness said so itself (STU-1147).

`.studio/runs/` is where Studio writes its journals (STU-1011). Linking it is only a
fallback now (STU-1012): a run writes its tokens into its own artifacts (the report's
`cost`, the `tokens` field of `logs/drafts/stages.jsonl`), so `cost` works from the
`reports/` and `logs/` links alone. What motivated the change is exactly what the link did
not catch: Studio writes next to its cwd, so a run started from a worktree left its
journal *there*, and a removed worktree took the only record of what the run cost with it.
The link only serves to recover a run older than that change, and `.studio/runs/` is
pruned like the other shelves.

## Structure

Two orchestrations of the same run, same report: `make run` (Python orchestrates, Studio
is the leaf) and `make run-batch` (`.studio/pipelines/classify-batch.pipeline.yaml`
orchestrates, Python is the leaf, STU-843). `.studio/pipelines/classify.pipeline.yaml` is
shared and unchanged.

"Same run" means same provider, and that was not true (STU-1128). `STUDIO_PROVIDER` means
"the provider this host runs", and only the push path read it:
`email_classifier/orchestrate/stage2_client.py` passes it as `--provider`, which beats
what the agent names, while `classify-batch` passed nothing and fell back on `.studio/`.
On a host set to `claude-code` with no `ANTHROPIC_API_KEY` (the configuration a production
deployment served from 2026-08-30, when the classifier agent still pinned `anthropic`),
the first worked and the second died. STU-1133 has since removed that pin, so a host that
sets nothing sees both paths agree; they agree by coincidence, the default and the
override naming the same provider, and the gap comes back as soon as a host sets the
variable to something other than `defaults.provider`. What is at stake is that the two
orchestrations can stand in for each other, and a rollback, a catch-up or a manual round
over mail the push path missed are exactly what `mailbox-full` is for.

So the Makefile sets `LIVE_PROVIDER = $(if $(PROVIDER),$(PROVIDER),$(STUDIO_PROVIDER))`,
and every target that touches real mail passes that flag: `PROVIDER=` on the command line
still wins for a deliberate A/B, otherwise the host decides. Two flags and not one,
because there are two questions: the bake-offs and `eval-ab` keep `PROVIDER_FLAG`, empty
by default, because a measurement taken under whatever the host runs is a measurement of
the host, the very reason `email_classifier/draft_bakeoff.py` names `anthropic` instead of
inheriting. Collapsing them into one variable *was* the ambiguity. And since the flag and
the input file's `provider` field come from the same variable, `provenance.provider`
records what resolved and not what the agent asked for: the two hand-made reports of that
day announced `anthropic` while their children died on it.

The failure pointed in the direction that looks like success, and that is what the second
fix targets: the run finished, its four stages green, and the failure was a verdict
carrying `action: None` in an otherwise normal report. `provenance.unusable_provider`
therefore refuses **before the first call**: it reads the resolved provider's block in
`.studio/config.yaml` and names every `${VAR}` the environment does not set. What a
provider needs is already declared there, so no list of "who wants which credential" is
kept here to go stale, and a provider the config does not describe (`mock`, which the CLI
registers itself) passes instead of being refused: a refusal that cannot be grounded is
not a refusal. All three entry points apply it: the webhook at startup, `run_email.py`
before reading mail, `classify-pre` before the fan-out. A dead email stays the STU-392
bias; a dead provider is a dead run, because it fails every child identically and the
`on_failure: continue` that protects the first case then swallows all the others.

`.studio/pipelines/mailbox-full.pipeline.yaml` (STU-846) chains `fetch` -> `classify-batch`
-> `apply` -> `drafts` -> `calendar` as `call:` stages under one run id; resume at a
boundary with `studio replay <run-id> --restart --stage apply`. `fetch` mints the run id
and writes `dumps/<run-id>.json`; the later stages consume its output, never a path from
the input (STU-896). Only `drafts` (STU-850: reply drafts on `needs_reply`, a draft/QA
group, anti-theatre on the draft's creation) and `calendar` (STU-2089) are tolerated in
failure.

### The unit of verdict is the conversation (STU-940)

Classification is per message, the mailbox is per conversation. An Outlook category sits
on a message, but a folder move does not stay there in the reader's eyes: three replies
free to diverge leave a thread split between the Inbox and the Archive, and the second
apply moves a message out from under the first.

`email_classifier/orchestrate/conversation.py` is the only place that states the rule,
and `apply`, `drafts` and the eval all read it there:

- **Bucket**: the highest-priority one any message of the thread carries, in the taxonomy
  order A > B > C > D. A newsletter someone replied to has stopped being a newsletter, and
  the taxonomy's "when in doubt, default to A" says which way a tie falls.
- **Action**: that of the most recent message. What a thread is owed is a claim about
  *now*, which only the last message can make: a "thank you" closes a thread its opening
  question had left waiting for a reply.
- **Clamping**: that action is then held to what the reconciled bucket allows. Without it,
  a notification that arrived last archives the unanswered question above it.

The three are not interchangeable: reconciling the action by precedence would never let a
thread close, and reconciling the bucket by recency would let the last message rewrite
what the conversation is about. Recency is read from the verdict's `date`, never from the
report's order: that is the fetch order, and Graph pages from newest to oldest.

Consequences, each one tested:

- a conversation finalizes only if *all* its messages can: a half-finalized thread is the
  same split, reached through the policy instead of through disagreement;
- the reconciled verdict applies to every message of the thread, so the journal stays one
  line per message per operation and `undo` is unchanged and exact;
- the unit is the conversation **inside a report**, and the fetch is what makes that hold
  beyond it (STU-963): a push run's report would carry only the notified message, so
  `email_classifier/fetch/threads.py` widens every fetch that feeds a report to the whole
  thread. Otherwise a reply on a thread an earlier run archived reconciles as a
  one-message conversation and leaves the thread split. Siblings are reclassified, never
  read back from an earlier verdict: the store is LLM-only and keyed by sender, and a
  report lives in gitignored `reports/`. The price is the stage 2 call of a sibling that
  escalates, on every run that touches its thread;
- apply therefore converges on a **state diff**, not on the marker (STU-963): we alone
  write `email-tool/<bucket>`, so the label a message carries *is* the record of what we did. Equal
  to today's verdict means converged, nothing moves; different means corrected, in both
  directions (`unarchive` is the operation that was missing to undo an archive). This
  holds because the bucket label is written **last**: out of the Inbox then labelled, back
  into the Inbox then unlabelled. A run that dies in between leaves a message without a
  label, unfinished, so the next run completes it; the reverse order would leave a label
  without the move, which the next run would read as converged, stranding the message in
  the Inbox forever. It is also what respects a human move: moving an archived message back
  into the Inbox does not remove its `email-tool/C`, so an unchanged verdict reads it as converged,
  and a changed verdict moves it again, folder included. The trash is the third location
  on the same axis (STU-983), not an exception: `label_ids` reports `TRASH` the way it
  reports `INBOX`, so a thread that a reply brings back to A or B pulls out of the trash
  the half an earlier run had thrown there. What makes this decidable is that
  `mark_for_delete` **removes the bucket label before the move**: we only trash a message
  taken from the Inbox, and we never leave one in the trash carrying an `email-tool/<bucket>`.
  Marker without a bucket means our trash. Which half is ours needs its own record, and has
  one: `email-tool/deleted`, written **before** the move. `email-tool/<bucket>` says where we filed;
  nothing said that we had trashed, and no location label can say *who* did. Nor can it be
  inferred from the absence of a bucket: an applied `keep_visible` also leaves a message
  marked and bucketless, so that test would read as ours every email a human deletes after
  we read it, and resurrect it. The label is read in both directions, and both refuse to
  act: in the trash without it, the deletion is someone else's; outside the trash with it,
  someone put it back (or a run died before the move) and we do not throw it away a second
  time. Same rule as "the human move holds" on the Archive side, same price: a dead trash
  is not taken back. A message in the trash without `email-tool/deleted` at apply time (a human
  deleting between fetch and apply) is neither moved nor stamped. Leaving the trash always
  states its destination: `untrash` restores the previous labels on Gmail and moves to the
  Inbox on Graph, so the plan cannot inherit one;
- a draft is a mailbox output like a move: one per conversation, on its most recent
  message (`drafts.reply_targets`), and `drafts-pre` names the replies that reconciliation
  drops without anyone having decided nothing was owed (`held_replies`: thread not decided
  in full, or action clamped by the bucket), since an email silently left unanswered is
  worse than an email left unmoved;
- an action decided on a bucket outside the taxonomy raises: `email-tool/<bucket>` and the
  taxonomy are one vocabulary, and holding it for review would hold it on every run. The
  list of actions per bucket stays in `email_classifier/core/contract.py` (INV-08); the
  module takes only the clamping fallback from it;
- the unit holds one message earlier: `collapse` (STU-944) makes only one stage 2 call per
  conversation. The head is the most recent escalated message (the "action" half of the
  rule) and `thread_history` is the rest of the thread the run fetched, an already decided
  message included: a reply is projected without its quote (STU-935), and the message that
  carries the other half is often the one a rule decided. The verdict comes back on every
  escalated message of the thread, so the report stays one line per message and
  `reconcile` has no disagreement left to arbitrate. The learning store records them all:
  it is also what tells a `missed` catch-up which mail has already been decided (STU-954),
  and keeping only one would make it pay a stage 2 pass again for the siblings. Not
  counting them as separate evidence is the job of `promote`, which gates `MIN_MESSAGES` on
  distinct conversations and not on messages: a thread is a single judgement, however many
  messages carry it. Each turn is tokenized separately, never the rendered transcript: the
  fragment veto of STU-920 reads position, and joining first would bury every tail in the
  middle of the text.

- `email_classifier/core/`: shared types plus JSON schema validation.
- `email_classifier/providers.py`: provider dispatch (`email.provider` in
  `rules/policy.yaml`) plus the Gmail/Outlook ontology mapping plus `configured_mailbox`,
  the mailbox this host addresses (STU-1183). One declaration, that of the client that
  actually reads. A deployment that moves from one mailbox to another produces reports
  from each that are identical, `input_path: webhook:push-notification` included; measured
  on a production deployment that switched from a staging box to its production box on
  2026-08-30, an audit that carried the ids of one to the other's store got nothing back,
  which reads as deleted mail and not as the wrong mailbox, and three probes went out to
  confirm a migration that had not happened. `provenance.mailbox` records it, null where
  nothing names it: Gmail always (`userId="me"`, and the address would cost a call), a
  delegated grant on `/me`, and any run on a frozen dump, which opened no mailbox today:
  stamping today's mailbox there would put the wrong store back on ids that came from
  another, which the STU-1123 floor rule already avoids for the same reason.
  `scripts/audit_run.py` `check` and `reviewed` refuse outright when the report names a
  mailbox and the host is configured for another; an unnamed side grounds no refusal.
- `email_classifier/stage1/`: deterministic rule engine plus policy plus frontier log.
- `email_classifier/fetch/`: per-provider fetch: `email_classifier/fetch/gmail/` (OAuth,
  read, push), `email_classifier/fetch/outlook/` (Graph, STU-325, plus push subscriptions,
  STU-823); each projects to the neutral schema. On Graph, everything that reads text reads
  `uniqueBody` (the message without its quote), never `body` (STU-935): a reply's quote
  trips a guard on words the sender did not write, mints tokens for names that appear only
  there, and serves the model our own earlier reply as fresh context. Graph publishes no
  preview of `uniqueBody`, so `body_snippet` carries all of it, uncut: a cut would make the
  projection lossy and parity (STU-848) would read it as a disagreement between providers.
  What the projection does not cut, the guard bounds (STU-997): the word lists of
  `not_transactional` were calibrated on a Gmail `snippet` capped at 255 characters, and
  read against 14 kB of marketing copy they no longer biased, they vetoed: `catch_all` on
  26 messages of 26 and `ratio_addressable` at 0 on a production run, on words that live
  in a footer. `email_classifier/stage1/guards.py` therefore reads only the first
  `BODY_SCAN_LIMIT` characters of a body, the length the two providers have in common (the
  cap of Gmail's `snippet`, that of Graph's `bodyPreview`). The choice is made where the
  guard is defined and not in the projection, otherwise the next projection change
  decalibrates it silently; and narrowing it word by word does not converge (STU-892 had
  already tightened `meeting`, and the same newsletter came back five words later).
  `email_classifier/fetch/threads.py` widens a message fetch into a conversation fetch
  (STU-963): each fetcher answers with the *incoming* mail the mailbox still shows of the
  thread (`conversation_message_ids`), drafts and Junk Email dropped on the provider side.
  Deleted Items too, but by the `providers.trashed_by_us` test and not by folder (STU-983):
  "someone threw it away" is only true of a message we did not throw away ourselves, and
  ours say so on themselves (`email-tool/deleted`); the half an earlier run trashed therefore comes
  back when a reply reopens the thread, and what a human deleted stays out, otherwise it
  would be reclassified on every run, vote in the thread's verdict and could be pulled out
  against them. Sent Items too, for a bigger reason: our own reply is not mail to classify,
  and left there it would be scored, could be the thread's most recent message (so decide
  its action and attract a draft addressed to ourselves), and apply would move it out of
  Sent Items. What a sent reply means for the thread is read where it belongs, on
  `last_sent_at` (STU-945). `max_results` caps how many conversations are looked at, not
  how many are read: half a thread is precisely the failure. Deleted Items and Junk Email
  also drop on the GET by id, not only at listing (STU-988): the push path names ids taken
  from a notification and never lists, and the queue is drained out of band (STU-927), so
  the gap between Graph's announcement and the read is the length of the queue; a message
  trashed in that window would be classified, pulled out of the trash by apply and given a
  draft. `parentFolderId` travels on the GET the id costs anyway. On Graph, listing and GET
  by id exclude the same `_NOT_INCOMING` as thread expansion, drafts included (STU-996):
  one definition of "incoming mail", not two. `/me/messages` is the whole mailbox, so the
  listing also served our own replies and the drafts a run had just written. Measured on a
  production run: 7 messages of 26 came from the mailbox owner's address, three of them
  reply drafts dated a few minutes earlier; all seven escalated to stage 2 and came back
  `human_real`. A draft is tested on `isDraft` and not on the folder, the property
  `_conversation_messages` already filters on, and it drops for every caller of the
  listing: a draft is not a precedent of how the organization writes either. On Gmail the
  same filter applies on the GET by id (STU-993), TRASH exempted by the same test as at
  listing, and it is wider than the listing's: `users.watch` is limited to `INBOX`, but
  that governs only the *delivery* of the notification, not `history.list`, which is
  called without a `labelId`, so a `_NOT_INCOMING` label (`TRASH`, `SPAM`, `DRAFT`,
  `SENT`) does reach the push path, and `labelIds` already comes back on
  `format="metadata"`.
- `email_classifier/fetch/window.py`: how far back a run reaches (STU-1123). Before this
  module the answer was `max_results` (33 from the Makefile, 100 from the fetch stage), a
  count of conversations and not a boundary anyone decided: a mailbox the tool had not
  opened was bounded by an accident of arithmetic, and mail older than the day's window was
  neither classified nor recorded as skipped. `policy.start_date` is the boundary instead,
  and the default is **today**, resolved per run and never at policy load: the webhook
  loads its policy once and serves for days, so a date frozen at startup would reach back
  to the last restart. Today is the direction a wrong answer must fail in (a deployment gets
  nothing retroactive until someone writes a date), and the price is named and accepted: a
  run tomorrow does not pick up what it skipped today. Two applications of one boundary,
  and they are not redundant: each provider bounds its own listing in its own language
  (`after:YYYY/MM/DD` on Gmail, `$filter receivedDateTime ge` on Graph), which keeps
  `max_results` from being spent on mail the run will drop; but Graph refuses `$filter`
  next to a `$search`, and Gmail's `after:` is a date and not an instant, so the query
  bound is a hint **never narrower** than the truth (widened by a day on Gmail) and
  `in_scope` is the exact bound, on parsed dates and not on string order. Three things the
  floor does not bound, each one decided: a frozen dump is an already bounded run being
  replayed, and applying today's floor to it would refuse to replay yesterday's corpus,
  the wrong direction for the rollback `--dump` exists for; `apply` and `drafts` consume
  an already bounded report, same argument; and a sibling older than the floor **stays**,
  because `expand_to_conversations` filters the listing and then widens. What the floor
  decides is which conversations a run goes to fetch, and cutting the old half of a
  conversation it went to fetch would rebuild the Inbox/Archive split of STU-963 through
  the policy instead of through disagreement. That order *is* the rule, so `start_date` is
  a required argument of `expand_to_conversations` and not a filter that seven corpus entry
  points remember to call, the shape `pipeline.run` already uses for its tokenizer.
  `provenance.start_date` is what lets a report tell "out of scope" from "missed" for mail
  it does not carry, the two being otherwise indistinguishable; it is carried from the
  fetch stage to `classify-pre` rather than resolved again there, and null on a
  `run-batch` over a frozen dump, which nobody bounded today. Measured live on a test
  mailbox, 2026-09-01: 0 messages under the day's floor, 58 under a floor at 2026-08-01,
  and the only one of the 58 older than the floor is the old half of a thread whose live
  message is in scope, the sibling rule exercised where a fixture cannot answer. The Gmail
  half has only its query-string tests: the cached grant on the development machine no
  longer renews without a browser.
- `email_classifier/stage2/`: LLM placeholder (STU-390).
- `email_classifier/apply/`: per-provider writers: `email_classifier/apply/gmail/`
  (STU-405), `email_classifier/apply/outlook/` (categories plus folder moves on Graph,
  STU-847), plus reply drafters in both (`createReply` on Graph, STU-850; MIME plus
  `threadId` on the Gmail drafts API, STU-853; never sent), plus a policy-gated executor
  (STU-406), plus the apply journal, dry-run and undo (STU-407).
  `undo` also deletes the drafts the run created (STU-854), except those a human has sent
  or edited since; the executor acts per conversation, not per message (STU-940), and
  converges on the `email-tool/<bucket>` already carried rather than skipping every marked message
  (STU-963), both rules stated above. The marker is rewritten only when it is missing:
  putting it back would not change the mailbox but would add a journal line, and undoing
  that run would then strip the marker an earlier run had set.
  The location axis has a third answer, and doing nothing is all of it (STU-1075): a
  message in none of the three places this app puts mail (the Inbox, the Archive where it
  files, the trash) is a message the mailbox holds where we did not put it: the
  `Deletions` dumpster of a hard delete, or a folder a human chose. `label_ids` answers
  `ELSEWHERE` and the executor skips it as `skipped_elsewhere`, marker included. Without
  that, a report that outlives its mail read like a live message that had simply already
  left the Inbox: a `make apply RUN_ID=<an older run>` (which is what a rollback or a
  resume looks like) planned an archive with no move, stamped `email-tool/<bucket>` on mail in the
  dumpster, and journalled every operation `ok`.
  A stage-1 rule may carry `expires_after` (`30m`, `1h`, `2d`: a count and one of `m`, `h`,
  `d`), and its verdict then records `expires_at`, the message date plus that duration
  (STU-2070). Once the expiry of a conversation's most recent message has passed, apply
  archives it with `label_archive`, past the bucket's clamp, since archiving is what the
  rule asked for. Only from the Inbox, or a message we already filed: one a human moved out
  with no `email-tool/<bucket>` of ours stays where they put it. A fetch reaches back to its floor
  and no further, so the `mailbox-full` apply stage (`scripts/apply_report.py`) also reads
  every other report next to its own for conversations whose expiry has passed, each taken
  from the report holding its most recent message. The archive is journaled by the current
  run, and `undo` of that run reverts it. A rule without `expires_after` behaves as before.

- `email_classifier/webhook/`: push endpoints (classification on notification, Gmail and
  Outlook), with both stage 2 passes (classify and drafts) under the single
  `STUDIO_PROVIDER`. The `make` targets pass it too since STU-1128 (the rule is stated
  above), and it no longer has a default (STU-1013): the flag it becomes beats the
  provider an agent names for itself (`stage-executor.ts` overwrites the agent's field),
  so a default here tied the classifier to the CLI on every host that did not set the
  variable. `claude-code` was the only provider that inherited anything, since the CLI
  builds its system prompt from the user and the cwd it is launched in: the checkout's
  `CLAUDE.md`, the home one, auto-memory, and the project settings under `.claude`.
  Studio 0.17.1 closes that (STU-1118): the spawn carries `--setting-sources ''` and
  `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, which take all four out together. Memory discovery
  is gated on the settings sources and not on the cwd, so the spawn has no cwd of its own
  to set. A host still on 0.17.0 still inherits, and the webhook says which of the two
  it serves, from `studio --version`. `anthropic` launches no process, so its prompt is
  only `.studio/` and the engine. That is what a staging host should set (STU-1005), and
  the startup warning now fires on the *resolved* provider, not on the variable: an env
  line someone adds would otherwise bring the inheritance back silently.
  A host that still chooses `claude-code` had, up to 0.17.0, only the system account
  between the agent and the operator's `$HOME`, and that is STU-1116:
  `install-webhook-service` substituted `id -un`, so whoever ran the install decided
  what every prompt inherited. Measured on a production host: several kilobytes of the
  operator's personal notes, including memories about the deployment's organization,
  reaching the agents that reply to senders. `SERVICE_USER` names the account, and the
  target **refuses** an account that has a login shell: that is the half of "is this
  `$HOME` somebody's" a machine can decide, and refusing is what makes the unit a
  mechanism rather than a convention. `ProtectHome=yes` is the other half: a separate
  account takes `~/.claude` out of the prompt, not out of *reach*, and the drafting
  agents run under `--dangerously-skip-permissions` with file tools. The service's home
  is `/var/lib/email-classifier`, outside `/home`, so masking `/home` costs it nothing.
  The account has no sudo (a `sudo tee` of a unit file is a path to root), so the deploy
  splits instead of running whole under one account or the other: `AS_SERVICE` carries
  the writes into the checkout, and the caller keeps systemd.
  That split cost an exit-code collision (STU-1122): `sudo` exits **1** when it is *sudo*
  that fails (no sudoers entry, an expired timestamp without a tty, a password it cannot
  ask for), and 1 is exactly what the deploy's verification line tolerates as "quiet
  mailbox, says nothing about the deploy". A deploy whose verification never ran
  therefore announced success, which is the very silence leg 3 of STU-1017 exists to
  break. Nothing triggered it, and that is an ordering nothing asserts:
  `sudo systemctl restart` runs earlier in the same recipe and is not tolerated, so a
  broken sudo fails there first, and reordering the recipe re-arms the collision. The
  sub-`make` therefore announces its own code on stdout, with everything else it says
  going to stderr: a number *is* the probe's verdict, and no number at all is a `sudo`
  that never got that far. Probing `sudo -n true` first was the other candidate, and it
  answers a different question: whether sudo worked a moment ago, for a command that is
  not this one.
  What the account did not buy is this repository's `CLAUDE.md`: the cwd stays
  `/opt/email-classifier`, so up to 0.17.0 the CLI discovered it whatever account it ran
  under (STU-1115). 0.17.1 takes it out with the other three, and what is left to the
  account is the *reach* that ProtectHome closes and that no prompt fix touches.
  Switching *to* `claude-code` to get off per-call billing only half depends on this
  variable: the CLI authenticates with `ANTHROPIC_API_KEY` as soon as it finds one in the
  environment, so a host that changes the provider and leaves the key keeps paying the
  API. The provider moved, the meter did not. Both are set in the same place and only
  one is named in the runbooks, so the obvious edit looks complete, and the failure is
  silent on the side that costs. Removing the key needs its own check, and not
  afterwards: if it was what authenticated the CLI, every classification then dies with
  nobody watching. `env -u ANTHROPIC_API_KEY claude --print` under the service user
  answers before anyone touches it, or the switch waits. The price of really being on the
  API: no agent in `.studio/` can carry a `temperature`, because Studio passes it through
  as is and every model since Opus 4.7 rejects the whole request on it. That was
  invisible while the only provider was `claude-code`, which never reads it.
  Then apply on the fresh report (STU-849): dry-run while `auto_apply` is false in
  `rules/policy.yaml`; at true, only `deterministic_can_finalize` and
  `destructive_actions_allowed` still decide what moves, and the `logs/applied/` journal
  makes the run undoable. Then `studio run drafts` on the conversations this report says
  are owed (STU-946), the same pipeline `mailbox-full` ends on. Out of process, because
  drafting is a Studio fan-out; and outside `auto_apply`, because the batch path gates it
  on nothing either and a draft is never sent, so a classify-only phase still reads what
  the assistant would have answered. But gated on `providers.can_draft` (STU-1002):
  drafting needs more than a drafter (`drafts-pre` reads bodies, the thread history and
  earlier mail, and only Outlook implements them). The predicate is what the three
  factories refuse on, never a second list; without it the webhook pays a subprocess per
  notification to rediscover a `ValueError`. The corpus goes out as a temporary file and
  not as `dumps/<run-id>.json`: a push run's mail never touched the disk and its
  provenance says so (STU-859), and a dump named after the run would offer an audit a
  corpus the report does not claim. A drafting failure is logged and dropped, never
  re-raised: the worker would replay the whole item and pay a stage 2 pass again for a
  classification already on disk. `undo` takes this run like any other, including one
  that only planned: it mutated nothing but its draft is real, and refusing on the
  missing apply journal would leave in the mailbox the one thing it created.
  A lost grant is the only failure that stops the process (STU-930): the Graph session,
  the Gmail service and the writer are all built with `interactive=False`, so what would
  open a browser raises `GrantLost` instead of blocking a service nobody is watching. An
  argument does not reach a subprocess: the `studio` children the webhook launches build
  their own clients (`drafts-pre` a drafter and a thread reader, the drafting tools their
  sessions), and `EMAIL_CLASSIFIER_HEADLESS` is what tells them there is nobody to
  consent (STU-946). On the Gmail side it was worse than an exception (STU-948):
  `run_local_server` takes a random port and waits forever for a redirect nobody will
  produce, which is indistinguishable from an idle service. The variable is read in one
  place only, `providers._interactive`, so what honours it is whatever builds its client
  through `providers`. `scripts/fetch_gmail.py` made the bare call and could never see
  it, and `make fetch` on a dead grant was the same silence, in front of someone
  watching (STU-1163). It now goes through `get_fetcher` rather than re-deriving
  interactivity: a second derivation is a second place that can go stale.
  `make fetch-outlook` was the same bare call, and what it does on a dead grant had to be
  measured before it was decided (STU-1173): msal does not start a device-code flow, it
  opens a local HTTP server like Gmail, and `acquire_token_interactive`'s `timeout`
  defaults to `None`. Measured on 2026-09-01 on msal 1.37.0, throwaway client id and
  empty cache, each arm killed at 25 s: no exit code, and two behaviours depending on
  what `webbrowser.open` returns. True (a browser, or any binary `BROWSER` names,
  including one that does nothing) is STU-948's complete silence, not one line. False,
  msal warns **once** on stderr with the URI and the port, saying itself that the
  timeout is `None`. So the printed line is not the exit the issue had in mind: both arms
  hang forever, and which one you get depends on a `webbrowser.open` that returns true
  for a binary that merely started (measured: `BROWSER=/bin/false` returns true, the race
  between `Popen` and `poll`). The script therefore goes through `get_fetcher` too.
  `app_only()` returns before `interactive` is read, so on a host that carries
  `OUTLOOK_CLIENT_SECRET` the path is never reached, and what this fixes is the delegated
  host. `scripts/seed_outlook.py`, `scripts/deliver_fixtures.py` and
  `email_classifier/seed/outlook.py` keep the bare call: that is the seeding harness, a
  human runs it at the keyboard, and interactive is the intended behaviour.
  None of this runs on the request thread: the route authenticates, writes what the
  notification announces to `state/notifications.jsonl` and acknowledges, and a worker
  thread drains the queue out of band (STU-927); otherwise the provider redelivers while
  the run is going. The queue is the crash boundary: the entry is on disk before the ack
  and removed only after the run. Past the ack nothing is redeliverable: a run that
  raises is logged and dropped, and the Gmail `historyId` does not advance, so the next
  `history.list` window covers those messages again.
  The worker takes the queue in batches, not one by one (STU-939): the provider announces
  each message as it arrives, so five that land together were five runs (five reports,
  five stage 2 passes, and the same sender minted under a different token in each, since
  the keymap a run seeds is per run). The worker therefore holds a short window open
  after the oldest queued item and classifies at once what has accumulated in it. Only
  the head of the queue merges, and only between items of the same provider: the queue's
  order carries meaning, and apply is bound to the single provider `email.provider`
  names. A lifecycle event never merges, since each one names a different repair. The
  window is fixed from the first item and not pushed back by each arrival: a mailbox
  that receives non-stop must be classified, not deferred for as long as mail keeps
  coming. A batch is a unit of work, never a unit of attempt counting: the count stays
  per item, so one that only travelled beside a poisoned item keeps the attempts it has
  left; and since a batch is a single fetch, a failing run shrinks the next pass to one
  item, which is what isolates the unreachable message the bound exists to drop.
  A batch is also the unit of what a run leaves behind: the report, the shared keymap
  and the run keymap are all written after the last escalation, never as it goes. An
  interrupted batch therefore writes nothing (measured on 2026-08-28 on a production
  host: 17 conversations, 17 stage 2 passes spent for zero artifacts when the service was
  stopped midway). The `finally` covers a run that raises, not the SIGTERM that cuts it:
  stopping the webhook costs the batch in flight, and the tokens already sent to the LLM
  are re-minted on the next pass.
  `GET /healthz` (STU-931) gives the age of the last authenticated notification, stamped
  in `state/last-notification.json`: a deleted subscription produces no error, only a
  silence nothing else distinguishes from a quiet mailbox. `scripts/check_webhook_health.py`
  (`make check-webhook`, cron) exits non-zero when that age exceeds
  `WEBHOOK_STALE_AFTER_SECONDS`. That exit code is no longer the whole alert (STU-1066):
  its only reader was cron mail on a host where nobody reads mail, which is how a 3 for
  a checkout fifteen commits behind `main` told nobody for days. A non-zero exit
  therefore goes through `alerts.notify`, and every run (0 included) is appended to
  `state/health-checks.jsonl`. Deduplication is on the **code alone**: the codes already
  *are* the distinct conditions, each given its number so as not to be confused with
  another, and the summary cannot enter the key, because a staleness age and an
  advancing `main` head both change every hour while the failure does not. So a drift
  left for a day is one notification and not 24; a failure that changes class, or comes
  back after a run that exited 0, notifies again. An unreadable or unwritable journal
  notifies every hour rather than never: a deduplication that cannot remember must
  repeat. The file is also what makes a deployment's unattended-operation criterion
  ("what did the hourly runs exit over five days") answerable from an artifact, which
  nothing recorded before.
  They still had to be told apart (STU-1082): the cron, the post-restart check of
  `make deploy-webhook` and a hand on a keyboard wrote the same shape, so the file
  answered "what did the hourly runs exit" with every run it had: three lines in the
  four minutes after it was created, none hourly, including a failure that never
  happened. A line therefore carries its `source`, and the criterion is read through a
  filter. The default is `manual` and not `cron`: forgetting the flag must under-count
  the hours, never invent them. The `source` does not enter the dedup key, for the reason
  the summary is kept out of it: a failure belongs to the host, not to whoever asks. And
  `--journal` sends a run to a throwaway file: testing the alert channel used to be
  possible only by patching the module constant from a `python -c`, which is exactly
  what produced the false line. A manual run still notifies: someone who runs the probe
  wants to see the alert go out, and it is the throwaway journal, and only it, that
  keeps the evidence of unattended operation clean.
  The URL it probes is derived from `WEBHOOK_HOST` and `WEBHOOK_PORT`, the two the server
  binds (`email_classifier/webhook/address.py`, STU-978): a default pinned to 8080 points
  elsewhere as soon as the host has already spent that port, and the probe then exits 2
  ("endpoint unreachable") every hour forever, the very signal a dead webhook produces,
  from the only job that exists to notice silence. That default survived as a
  *fallback*, and it is the same lie one level down (STU-1087): `WEBHOOK_PORT` reaches a
  Python process only through the Makefile's `-include .env`, so a bare
  `python scripts/check_webhook_health.py` (and every worktree, which has no `.env` by
  construction) composed 8080. On a production host that port was a docker-proxy, which
  answers **404**, and a 404 is positive proof that something is listening and that it
  is not us; the probe reported it as an absence while the webhook was answering on its
  real port. `configured_health_url()` therefore refuses when neither
  `WEBHOOK_HEALTH_URL` nor `WEBHOOK_PORT` is set, under a code of its own, **5**, the
  same shape as STU-1027's refusal: a probe that was not told where to look must say so,
  never invent an address and then accuse the webhook of not being there. `argparse`
  usage errors, which exit 2 on their own, exit 5 too: they said "endpoint unreachable"
  of an invocation that never asked anything. `listen_port()` keeps its default: the two
  sides are not symmetric, since a bind succeeds or fails loudly on `EADDRINUSE`, so only
  the asking side can be silently wrong.
  That refusal then named the wrong condition, and the price is that it sends the
  operator to fix something else (STU-1122): the Makefile's `-include .env` does not
  distinguish a missing file from a file this account is not allowed to open (both
  arrive as nothing), so since STU-1116 set `/etc/email-classifier` to `700
  email-classifier`, a `make check-webhook` typed by the operator on the serving host
  announced "neither is set" while `WEBHOOK_PORT` was in the file `.env` links to. The
  repair that message names is the one that does not work: exporting a port by hand,
  plausibly the 8080 it puts in front of them, that is, the docker-proxy STU-1087 was
  written against. `configured_health_url()` therefore asks the filesystem which of the
  two it is looking at, and the second message names the file, both ends of the link,
  the account that owns it and `sudo -u <account> make check-webhook`. `lstat` and not
  `exists()`: following the link requires entering the directory, so the file that is
  plainly there reads as absent. Only a `PermissionError` counts: a dead link raises
  `FileNotFoundError` and really is "nobody gave the address", and claiming an ownership
  problem there would be the same wrong cause one door further. **Under the same code
  5**, decided and not assumed: the family's rule is that dedup holds on the code alone,
  so a condition folded into another goes unnoticed behind it, but that rule is about
  failures a cron must notify, and no cron path reaches this one, since the serving
  host's cron already runs under the account that can read the file. What is left is a
  diagnosis typed by hand, where the message *is* what the operator acts on, and where
  both conditions give the same verdict (the invocation never asked the webhook
  anything), repaired at the invocation and not on the host. The Makefile names the
  state once for all its targets, as a `$(warning)`: what `-include` swallows is silent
  for every one of them, and only `check-webhook` turned it into a message.
  The probe also carries the one failure it can announce *before* it happens
  (STU-1095): the app-only client secret expires on a date set in
  `OUTLOOK_SECRET_EXPIRES`, and unlike a grant it does not renew without a person (it
  needs someone in the Entra console), so a warning that fires the morning of the expiry
  fires too late. `email_classifier/webhook/credential.py` reads the date from
  configuration: the app-only token does not carry it, and reading it back from Graph's
  application object would need `Application.Read.All`, a tenant-wide permission this
  app should not hold to learn a date. Two more codes, **6** inside the 30-day window and
  **7** when a secret is set and nothing says when it dies, and not a reuse of 5, by the
  rule that gave each code its number: the dedup key is the code alone, so a condition
  folded into another goes unnoticed behind it, and 5 exits *before* the probe, where a
  missing date must not pre-empt the liveness verdict. The leg exists only if
  `OUTLOOK_CLIENT_SECRET` is set: the interactive flow has no secret that expires, so
  without that gate every staging host and every worktree would alert forever about a
  credential it does not have. Precedence is silence, then expiry, then drift: a lagging
  host repairs itself through `make pull-webhook` and comes back the next hour, whereas
  the window only runs out, and a standing drift would otherwise mask the expiry for the
  whole month it exists to give. A missing or unreadable date exits 7 and never 0, the
  shape of STU-1087's refusal; outside the window it is named on the `ok` line, since a
  leg that is wired and silent must not read as a leg nobody wired (STU-912).
  `WEBHOOK_HEALTH_URL` names the webhook this host does not start: a probe launched from
  another machine, and the normal case of `make deliver`, which asks for the same address
  (STU-1019). The round lands on the staging webhook, which stamps *on its own host*, so
  reading the local checkout's `state/last-notification.json` could only see a frozen
  stamp and cry miss. Measured: four rounds of STU-1000, 25 notifications all received,
  four warnings. The question is therefore asked of the webhook's `/healthz`, and an
  address that does not answer says so: "nobody serves here" is worth nothing as a claim
  about what Graph sent. The probe opens the ssh forward itself, as the round does
  (STU-1037): `WEBHOOK_HEALTH_URL` names the address the *serving* host sees, so composed
  from a development machine it hit its own empty loopback and exited 2 ("the webhook is
  dead") while the serving host answered 0 to the same `make check-webhook`. A forward
  that does not open has its own code, **4**, for the reason that gave STU-1017 its 3:
  the probing machine learned nothing about the webhook, and 2 must keep meaning one
  thing. The serving host's cron is unchanged, since it names no `WEBHOOK_SSH_HOST`.
  `tunnel.reachable` now **raises** instead of returning the unforwarded URL: returned,
  every caller composes it and reports its own unreachable case, which blames the webhook
  for ssh.
  The payload also names the served commit (`email_classifier/webhook/deploy.py`,
  STU-1017), sampled **at startup** and not per request: a `git pull` under a running
  service moves `HEAD` without changing a line of the served code, so read from the
  working tree it would announce itself up to date on exactly the deploy nobody
  restarted. The probe measures it against `origin/main` and exits **3**, a code of its
  own, since a lagging host is not an outage and 2 must keep meaning only "the webhook is
  dead". What it cannot measure, it says (`deploy unverified`) and exits 0: comparing
  against a `main` seen weeks ago would answer "up to date" for a lagging host, exactly
  the silence the probe exists to break. That silence is what let a production host
  classify real mail fifteen commits behind `main` for days, including the fix that
  still sent conversations to the LLM in clear (STU-1009); the drift was found by hand.
  That `deploy unverified` puts the host back in exactly that state, and nothing counted
  how long it had been there (STU-1126): 0 stays the right code for one run (comparing
  against a `main` seen weeks ago would announce "up to date" for a lagging host), but it
  does not tell "nobody looked" from "we looked and all is well". Consecutive blind runs
  are therefore counted on `state/health-checks.jsonl`, and the third in a row exits
  **8**. The count is read from a field of its own (`deploy_unverified`) and not from the
  exit code, which is 0 in both cases; a line that does not carry it (from before this
  change, or from a run that never reached the leg: 2, 4, 5) is transparent, neither
  counting nor resetting: it is evidence in neither direction. Only a run that *measured*
  settles the count. Three, because the window brackets both directions: a resolver
  hiccup on a production host is documented as isolated and must not wake anyone, and a
  truly broken remote must be named the same day; the three blind runs of 2026-08-30
  15:59, 16:23 and 17:23, all exiting 0, are what it is sized against. Its precedence is
  the drift's, in the same place: behind silence, and behind expiry, the secret's window
  being the only thing here that runs out while a blind leg keeps reporting for as long
  as it is blind. An unreadable journal counts nothing rather than assuming a streak, the
  direction of STU-912: an impossible measurement is not invented.
  The other half of this leg is a drift that does not end, and nothing covered it
  (STU-1181): dedup on the code alone is right for the *first* notification (the summary's
  age and `main` head move every hour while the failure does not), and it leaves nothing
  to tell a drift being repaired from a drift nobody read. Measured on a production host:
  exit 3 on **27 consecutive hourly runs**, from 2026-08-30T19:45Z to 2026-08-31T23:23Z,
  the summary going from 2 to 22 commits behind, for a single notification; it ended
  because a human read the probe's output in a session about something else, after 27
  hours of real inbound mail drafted by a commit `main` had already replaced. A
  *measured* drift that survives **six** runs therefore exits **9** and notifies again.
  Six and not the three next door, and the number is decided here rather than inherited:
  three sizes a first notification, six a second one, and a second alert that arrives
  while the first is still on screen is what teaches people to swipe the next one away.
  Twelve would be a whole working day of a mailbox whose mail is diurnal. The count is
  read from a field of its own, `deploy_behind`, and never from the exit code, in both
  directions: a run whose leg measured nothing does not write the field and is
  transparent, and a stale age or an expiring secret beat the drift, so the host spends
  those runs exiting 1 or 6 while the drift underneath keeps holding. Only a run that
  measured *up to date with `origin/main`* settles the count. Precedence is unchanged,
  the family's: silence, then expiry, then drift; a lasting drift does not pre-empt a
  window that is running out.
  The **10** is the other deadline nothing watched (STU-1168): an audit entry the push
  path filed protects none of its artifacts (STU-1084), so 30 days after its report was
  written a reviewer loses the report, the anonymization record and the apply journal,
  and the entry becomes counters nobody can verify. `coverage` prints how much is due and
  no schedule runs it; this probe runs hourly and already announces what expires. A
  7-day window and a single line, never one alarm per entry: measured on a production
  host on 2026-09-01, 34 entries expiring on 34 consecutive days, and the dedup key is
  the code alone. The shelf's `keep_last` floor is exempt: the three most recent reports
  survive at any age, so a host that holds only three has no deadline, and a countdown
  against it would never go out. An entry whose report is *already* gone is counted and
  named on the same line without triggering the code: nothing repairs it, and a check
  that fails forever on it is the one nobody reads (STU-1145). Its precedence is behind
  the secret and ahead of the drift, by the rule that put the secret there: a window is
  the only thing here that runs out, and a drift left in front would mask it for the
  whole month it exists to give.
  `POST /graph/lifecycle` (STU-937) is the only address where Graph announces that
  delivery itself is broken (`reauthorizationRequired`, `subscriptionRemoved`, `missed`),
  and without it an expired grant cuts delivery while the daily `make renew-graph` keeps
  PATCHing successfully. The URL is derived from `GRAPH_NOTIFICATION_URL`, now required
  at server startup: it is what the server resubscribes with. Each event has its answer
  (reauthorize then renew, recreate, catch up from the watermark). The watermark (the
  date of the most recent message the push classified, in
  `state/outlook-subscription.json`) only advances after a completed run, so a `missed`
  picks up what a dead run dropped. The catch-up stays uncapped (a cap would cut exactly
  the gap it exists to cover), but it skips what the verdict store has already decided
  and announces through `alerts` what it is about to spend (STU-954): since the watermark
  only advances through the push, mail a manual `make run` escalated ends up in the gap,
  and a stage 2 pass is the whole price of a catch-up. The watermark itself advances over
  everything fetched, skipped mail included. A lifecycle event does not count as
  liveness: it proves Graph can reach us, not that mail is arriving.
  Neither `missed` nor the date floor recovers mail that arrived while the webhook was
  down (STU-1502): on 2026-09-03 a production webhook refused connections from 03:23
  into the next day, no lifecycle event came, and `start_date: today` dropped the three
  messages from every later run. `scripts/check_unverdicted.py` (`make check-unverdicted`,
  hourly cron) lists the Inbox since `UNVERDICTED_SINCE`, a required instant with no
  default (a default of today is what lost that mail), through
  `OutlookFetcher.inbox_received_since` (ids and dates only, drafts dropped), and diffs
  the ids against every `ok` verdict in `reports/`: an errored verdict is mail a run
  looked at and did not decide, so it counts as missing until a later `ok` clears it
  (STU-1992, 21 messages left unclassified by the 2026-09-30 spawn outage). Exit 3 names
  the ids on stdout and alerts with the count and oldest date; 4 is a mailbox it could
  not list and 2 no report on disk, never a clean 0 (STU-912); 5 is no instant. Mail
  older than the reports shelf (30 days) is counted and not judged, its verdict possibly
  pruned with its report, and so is mail from the last hour, possibly still in flight.
  Every run appends to `state/unverdicted-checks.jsonl`, and a run notifies when its code
  changes or when it exits 3 naming an id the previous run did not: a standing miss is
  one alert, a new one is another, and a set that only shrinks is someone handling it.
- `email_classifier/retention.py`: retention of run artifacts (STU-932). `reports/`,
  `dumps/`, `logs/applied/`, `logs/anonymization/`, `dumps/web-cache/` and `.studio/runs/`
  grow by one step per run and were never pruned. One age per directory, and what no age
  reaches is decided by `audits/audit_log.json`: the audit trail is worth far more than the
  raw dumps beneath it. Four protections, each covering a different thing a purge would
  destroy. The log names the run (`audited`). An entry names its `dump` by path, and
  `check` cross-checks the report against it (`audit-input`, STU-859); by path, because the
  dump a run consumed is almost never named after it. A report that neither the log nor
  `audits/skipped.json` covers is exactly what `audit-gate` holds the next run on
  (`unaudited`, STU-900), and the dump goes with it, since an audit that cannot
  cross-check its input is not the one the gate is waiting for. And the apply journal
  still on disk holds its report (`undoable`), because `undo` reads both: the journal
  reverses the mailbox operations, the report names the drafts to delete (STU-854), and a
  journal that outlives its report gives a half-undo that leaves the drafts behind.
  The run id is read from the file name: a name the pattern does not recognize is never a
  candidate, since an unreadable name must fail toward "kept" and not "deleted". That is
  what puts `dumps/outlook-seeded.json` and the shared keymap out of reach without naming
  them. The reports a purge actually reaches are therefore the ones `make audit-skip`
  excused. `scripts/prune_artifacts.py` is the command, dry-run by default, and it refuses
  outright when the audit log is missing: without it every artifact reads as unprotected,
  and the audited runs would go first.
  `.studio/runs/` is the fifth shelf and the only one no protection reaches (STU-1012):
  its names carry Studio's run id, not ours, so age is the whole rule. It gets the same
  30 days as `reports/`, so a run and the journal behind it expire together, and the
  pre-STU-1012 fallback lasts exactly as long as the report it answers for. It became
  prunable the day a run started recording its own cost; until then a directory nothing
  managed carried the audit trail (642 files, 12 MB, going back to 2026-06-17). `runs.db`
  sits there next to the journals, so a shelf carries a `glob`: sweeping the whole
  directory would take the index along with the entries.
- `email_classifier/cost.py`: what a run cost (STU-1011). The report says what a run
  decided, never what it spent. Four rates, not one (fresh input 1x, cache write 1.25x,
  cache read 0.10x, output 5x the input), because a bill made of cache writes and one made
  of output are not fixed the same way, and here it was the writes (0.50 to 0.79 of the
  bill on the four complete rounds measured at the time). That is what named STU-1013, and
  the answer was that the classifier ran on `claude-code`: the CLI builds its system prompt
  from its cwd, so every leaf of the fan-out rewrote the repository's `CLAUDE.md` at 1.25x
  (measured at 23,633 of the 34,100 cache-write tokens of one `classify` call on
  2026-08-28), never read back, because the CLI attaches project memory to the user turn,
  after the cache breakpoint. `.studio/agents/classifier.agent.yaml` therefore named
  `anthropic` for itself, with `prompt_cache: "on"`: `auto` reads a one-shot stage as not
  worth a cache, which is right for a single call and wrong for a fan-out whose leaves
  share the agent's whole prefix. Measured after: $0.032 per call against $0.232, hit
  ratio 0.30 against 0.057, write share of the bill 0.185 against 0.50 to 0.79. A model
  missing from the price table raises: a run silently priced at zero reads as a cheap run.
  That pin no longer exists, and what removed it is not the measurement that had kept it
  (STU-1133). Replayed under the fixed kernel, `CLAUDE.md` did leave the prefix (STU-1115
  took it out with `--setting-sources ''`, STU-1118 shipped it to the host), and the cache
  write fell to 8,551 tokens per call, the order of the 7,349 measured there. The other
  STU-1013 finding still holds and only bites in a fan-out: each leaf is a fresh process
  making one call, so when twelve start together none has written a block the others
  could read. Measured on the same dump, 12 calls per arm: `anthropic` $0.032 and ratio
  0.30, `claude-code` $0.071 and ratio **0.0**; a second corpus (27 calls) says 0.0 again.
  These are the only two multi-call `classify` legs ever measured under `claude-code`.
  The pin went anyway, on the axis this table cannot show: `cost` prices tokens, not an
  invoice. Under a subscription the `claude-code` column is not billed at all, while every
  figure in the `anthropic` column is real money: the pin bought a better cache ratio with
  cash. The price is named rather than discovered later: `make run`, `classify-batch` and
  `make classify-bakeoff` now file `classify` legs with a 0.0 ratio. `make classify-bakeoff`
  keeps `anthropic` as an explicit default, which is what the pin gave it: left to
  inherit, it would have followed `defaults.provider` without a word, and the STU-1049
  figures were taken on the API.
  What `check` says about it changed (STU-1145): it is a note, not a violation, under that
  provider only and only in the form "prefix written, never read". The violation was right
  under STU-1057 and useless here: nothing reachable from this repository fixes it, so
  every batch run would have filed the same line forever, and a check that fails
  identically every time is the one nobody reads on the day it means something. The push
  path is not as safe as it was thought to be either: `STUDIO_PROVIDER=claude-code` beats
  any agent pin, and the exemption of its single-call legs only holds while a burst
  escalates one conversation. The worker takes the queue in batches (STU-939), so two
  conversations in the same window make a two-call leg, and `audit_filing` would file that
  violation with nobody to read it.
  What keeps the check honest is where the provider is *recorded*: by the run, leg by leg
  (`cost.leg`), never re-derived from the tree an audit sits in. It resolves as CLI flag
  over agent field over `defaults.provider`, so a tree re-read weeks later answers for the
  tree. A leg with no recorded provider keeps the earlier behaviour: unknown is not a pass.
  The hit ratio has been a `check` leg since STU-1057, and not only a figure in the `cost`
  report: a leg at 0.0 is a violation, because reading nothing back is a mechanism failure
  (a contract that loses its tool floor, an agent that loses its pin, an `auto` that
  resolves elsewhere), where a low ratio is only a degradation. Zero and not a tuned
  threshold: the only two ratios ever measured are 0.30 and 0.778, and a floor between them
  would be a number nothing supports. A leg under two calls is exempt, since a first call
  can only write its prefix. A run that escalates without pricing anything exits 2 under
  the STU-912 rule, naming which of three conditions produced it (STU-1167). A leg no call
  was made for, every item served from the `map:` resume cache, is healthy, and it could
  not be told apart from a leg whose only call died before reporting its usage, which is
  exactly the failure `unpriced_calls` exists to show. The separation is in the leg the run
  already wrote: `classify-post` *drops* a resumed item instead of counting it unpriced, so
  a fully resumed leg records zero calls and a dead leg records them without pricing
  anything. Reading such a run otherwise meant opening its Studio journal by hand. A
  missing leg stays the third case, a report from before STU-1012. Still no escape hatch
  for a leg that "should not cache": both pin `on`, so the case is hypothetical, and a hole
  built in advance is what silences the check the day a real leg goes dark. The only exit
  is STU-1145's, and its three edges say why it is not one: it rests on a *measured*
  provider that cannot read a prefix back, never on an intent; it covers only the form
  "written, then never read" (a prefix never written stays the model floor, STU-1080, and
  stays a violation everywhere); and a leg that does not record its provider keeps the
  earlier behaviour.
  The model floor is the interesting case (STU-1080). A production run whose classifier
  already carried `prompt_cache: "on"` cached zero, and what did it was the **model**: the
  run fell in the haiku window of STU-1044, and the minimum cacheable prefix is per model
  (4096 tokens for `claude-haiku-4-5`, 1024 for `claude-opus-4-8`). The leg spent 7758
  prompt tokens over 3 calls, ~2586 per call: under the floor, so no block is written and
  the API says nothing. The next run, the same agent on the opus default, cached at 0.68.
  A haiku re-pin would reproduce that silently, and that is what the check catches. Zero
  hits therefore has two forms, and the violation message names the one it measured: a
  prefix written and never read (the leg pays for a cache write for nothing) only when
  `cache_creation` is nonzero, and otherwise a prefix never written, which costs nothing
  extra and does not have the same fix. An audit that believes the wrong sentence looks
  for the wrong defect.
  Both drafting agents carry the same `prompt_cache: "on"` (STU-1051), and what had left
  them without it says what `auto` really governs: the rule does not read the number of
  turns, it reads the *contract*'s tool floor, the only declaration in `.studio/` that
  predicts a second turn before the first has happened. STU-1030 took `email-draft`'s floor
  away with the write tool, and `draft-qa` never declared one, so the stage the group
  replays on every rejection wrote a prefix nobody read. Measured: hit ratio 0.778 against
  0.0, and $0.23 per drafted conversation against $0.662, on a round that did take two QA
  rejections (5 `created` and 2 `updated` in the journal), the half the figure exists to
  exercise.
  The counts now live in the run's own artifacts (STU-1012), one per place the call was
  made: the report's `cost` for stage 2 calls, the `tokens` field of the `completed` line
  of `logs/drafts/stages.jsonl` for the drafting fan-out. Two legs and not one, because
  each writer records only what it saw: a `mailbox-full` run spends most of its tokens
  drafting, so a report that also claimed that half would publish a figure nobody wrote,
  and a dead `drafts` stage still owes the count of what it burned first. `combine` adds
  them, `by_stage` separates them. Each call being counted once, where it was made, there
  is no roll-up left to cross-check; what `rollup_mismatch` guarded is named directly
  instead: a call that reported nothing is counted in `unpriced_calls`, not dropped,
  otherwise a provider limit (STU-1016) that kills twenty escalations of twenty-six would
  price the run on the six that answered. When nonzero, `usd` is a floor, not the bill. A
  leg where *nothing* is priceable does not read $0: it counts as "no leg", and the
  fallback takes over.
  How many calls a leg made could not be read from what it spent (STU-1056): the kernel's
  spawner summed a child run's stages flat (`sumTokenUsage`), so a fan-out item returned a
  single `token_usage` and nothing saying how many calls had bought it, and a *failed*
  item returned nothing at all. `cost.leg` therefore took the caller's count on top of the
  usage: 1 on the classify side, and on the drafting side the count the run derived from
  its own journal lines, two agent stages per group iteration. Both were floors, each blind
  to what the other did not see. STU-1064 removed the cause, and `cost.leg` now reads the
  `stages` the item carries (STU-1210): one entry per stage of the child run, with its
  status, its `attempts` and the `token_usage` it accumulated, **on a failed item as on a
  successful one**. The count is no longer fabricated and nothing fabricates it any more:
  `attempted_calls` and its two-stages-per-line rule went with it.
  Two things the flat form could not carry. A call is an **attempt**, not a stage
  execution: a RALPH attempt is one more LLM call that nothing outside the engine records,
  and that is exactly the gap STU-1058 had measured (13 executions, 19 retries, 32 calls
  reported as 13, a factor of 2.46). Replayed on today's rule, the same 13 items give 32.
  And a dead item **is priced**: what it burned before dying is in its record, so
  `unpriced_calls` keeps only the stages that truly reported nothing, instead of standing
  in for every failure. What this still does not fix: a stage that reported *some* usage,
  covering a call that reported none, stays invisible, the stage's sum being all that
  exists at that level.
  Which stages count is no longer named (STU-1494): `leg` sums the `attempts` of every
  recorded stage. They had to be named under Studio 0.18.0, where the engine pushed an
  `AgentRun` for a script stage too and a `write-draft` returned `attempts: 1`, a third
  call per attempt; Studio 0.20.2 (STU-1245) excludes `script:` runs, so a stage with no
  agent returns 0, as `ChildStageUsage` documents. A host under 0.20.2 still counts the
  script stage as a call. The tuples `DRAFTING_AGENT_STAGES` and `CLASSIFY_AGENT_STAGES`
  remain for `journal_calls` and `call-floor`, since a journal line carries no `attempts`;
  a test re-reads each against its pipeline. A stage's usage is summed as before: a nested
  `call`/`map` stage carries there, and nowhere else, what its own children spent.
  An item with no record at all keeps the pre-STU-1064 floor: one call, unpriced if the
  item did not return flat usage either. Two causes produce it (a Studio too old to return
  it, and an exception raised before the child run starts, which truly made no call), and
  nothing here tells them apart; a zero would let the first read as a free leg, which
  `unpriced_calls` exists to prevent. `unrecorded_items` counts them, and it is the field
  that keeps this fallback from being worse than what it replaces: a *successful* item on
  a host under Studio 0.18.0 carries its `token_usage` and no `stages`, so it prices at one
  call with `unpriced_calls` at zero, half the calls of a drafting leg, reading as a
  measurement and not as a floor, which the STU-912 direction forbids. The field also says
  when a leg dates from: absent, it predates STU-1210 and counts stage executions; present,
  it counts attempts, and `scripts/audit_run.py` `call-floor` reads it that way and prints
  it as `reported_unit`, without which the same `undercount` reads two opposite ways and
  the artifact does not say which. A deployment's host install therefore requires Studio
  0.18.0.
  The `stage2_client` path is the only orchestration Studio does not hand this record to:
  its escalations are `studio run classify` subprocesses, not `map:` items, so the `stages`
  it reads have a reader's shape (`stage_name`, `tasks`) and not that of the boundary
  STU-1064 opened. It therefore builds the record itself (STU-1244), but no longer derives
  the attempts (STU-1493): Studio 0.21.0 writes `attempts` on the `StageRun` (STU-1287),
  and that is the field it reads. It used to derive them from the highest `attempt` of the
  `agent_runs`, a second statement of `ChildStageUsage` with no reason left to exist. A
  stage without `attempts` (a host under 0.21.0) drops the whole record to the floor of one
  call per escalation, `unrecorded_items` saying so: a record built without attempts would
  price at zero calls, which reads as a free leg. Without the record the leg fell to the
  floor (one call per escalation where `classify-batch` counted attempts), and the two
  orchestrations no longer agreed on `llm_calls` for the same corpus: STU-1128's
  substitutability on a second axis.
  The record also travels on **failure**, and that is the half that matters (STU-1016):
  the CLI prints its whole record *then* exits 1, so an `rc != 0` and a "no successful
  `classify` stage" both have one to price, and a provider limit is exactly the case where
  the child burned its three attempts, the notice being the agent's answer. A
  `studio_run_output_invalid` carries it too: the call was made and billed, only its answer
  is rejected. What stays at the floor is named rather than discovered later: unreadable
  stdout and a timeout truly have no record, and `unrecorded_items` counts them.
  Verified live on Studio 0.18.0 on 2026-09-01, a `map:` over three items (one successful,
  one dead in a script stage, two dead by exhausting their RALPH attempts): the failed item
  does carry its `stages`, `llm_calls` equals the sum of the agent stages' `attempts` (8)
  and `unpriced_calls` counts only those without `token_usage` (6). Under the old rule the
  same items returned 4 calls of which 2 unpriced: an item's three attempts folded into
  one, and a dead but priced item counted as unpriced.
  How far below this floor sits is measured (STU-1058), and the answer depends entirely on
  whether the run failed. `scripts/audit_run.py` `call-floor` asks the run's Studio
  journal, the only place a RALPH attempt is written: `stage_start` opens an execution and
  `stage_retry` falls *between* attempts and never after the last, so a stage's calls are
  its starts plus its retries. The starts, not `stage_complete.attempts`, which is the same
  value where it exists and disappears with the execution a run did not finish, exactly the
  shape a killed drafting stage leaves. The difference splits into two causes, additive by
  construction: a retry is a call no execution count can see, and the rest is an execution
  no line of the run counted. Measured on 2026-08-29 on the only two drafting runs whose
  journal survived:
  - 22 conversations, 3 QA rejections (the multi-attempt path is in the sample): 50
    reported, 50 made, gap **0**.
  - every item's `generate-draft` died: 13 executions for 19 retries, 32 calls; the rule
    reports one per item. The true count is 2.46x the floor.
  The sentence this paragraph used to carry (**a retry only lands on a stage that fails**)
  is false, and with it the "exact floor on a run that succeeds" that an audit read as
  "nothing to fix" (STU-1083). The sweep that supported it read only the 792 journals in
  the repository, while a production host writes its own journals outside the checkout,
  gitignored on both sides, so the sample excluded exactly the class of run that now
  produces most reports. Replayed on 2026-08-29 on both, a stage counting as "without
  failure" when every one of its executions exited `success` (a `rejected` is a group
  iteration, which the count already carries):
  - **repository**, 890 journals: 33 stages carry a retry, 7 on a `failed` stage and **26
    without failure**, all `draft-bakeoff` runs written after the original sweep, which
    was therefore exact on its sample and wrong about the world.
  - **production host**, 131 journals: 41 stages carry a retry (33 `failed`, 2 `cancelled`
    and **6 without failure**, all `generate-draft`), 6 of that host's 10 drafting runs,
    19 retries for 42 executions.
  The counterexample is a drafting run with 7 starts and 7 `success` on `generate-draft`,
  nothing failing, where `call-floor` still reads 5 calls reported against 17 in the
  journal. The gap of 12 does not read as one block, and the half that jumps out is the
  wrong one: 9 executions are missing because the host was still serving code older than
  STU-1056, where `llm_calls` equals `drafted` exactly; that is deployment drift (STU-1017),
  not floor. The **3 retries** are the floor, and they landed on a run nothing made fail.
  The two production runs on STU-1056 code say it without that noise: gap 1 for 1 retry,
  and gap 0 for 0 retries; `unrecorded_executions` zero on both. On today's code, a
  drafting run's gap *is* its retry count, and a run that succeeds has some.
  So: **a run's status says nothing about its floor**, and `call-floor` is read on every
  run, never only on the one that failed. What stays true is the lower bound of a run that
  fails: 40 to 60% of the true count, and the same figure on the classification side on
  the largest sample available (26 executions for 18 retries, 44 calls against 26).
  This whole paragraph describes a leg written **before** STU-1210, and must be read that
  way: retries are now in the leg's count, so a run's gap is no longer its retry count but
  what no recorded execution covered. That is why `call-floor` no longer breaks the gap
  down by cause: subtracting retries from a count that contains them yields a negative
  number, and a negative cause is worse than a missing one. It prints `undercount` and,
  beside it, the `ralph_retries` the journal saw, which remain the share retries explain,
  for a leg old enough to count only executions.
  `call-floor` reads **both** legs since STU-1246, one line each under `legs`. It used to
  read one, which was defensible while the classification leg counted one call per item:
  the figure was a floor of known shape, whose size was self-evident (one call per
  escalation, retries invisible). STU-1210 and STU-1244 turned it into the same claim as
  the drafting one: both classification orchestrations now report RALPH *attempts* read
  from the child's per-stage record, and nothing cross-checked them against the journal
  that recorded those attempts on its side. The subprocess path is the more interesting of
  the two: it is the one where this repository **translates** the record instead of
  reading it at the spawner's boundary. The journal side needed no new rule
  (`journal_calls` already takes a tuple of stages and `CLASSIFY_AGENT_STAGES` exists), but
  the reported side needed a second source: the drafting leg falls back on the executions
  in `logs/drafts/stages.jsonl`, the classification leg is a single block the run wrote
  into its own report.
  Exit 2 became "no leg measured" rather than "the leg is missing", and the price is named:
  a run whose drafting leg recorded nothing now exits 0 on its classification line. Each of
  the two is a legitimate whole run (a classification-only `run-batch` has no drafting to
  measure), and a rule that exited 2 there forever would make this the command nobody
  reads on the shape it reads most (STU-1145's argument). What keeps that from reading as a
  pass is the line itself: `reported_calls` and `reported_unit` null, and the leg named on
  stderr. Exit 3 fires per leg, and the message names which.
  `journal_calls` had to stop counting the fan-out stage itself. The `map:` of
  `classify-escalations` carries the child stage's name, so the deepest-level rule only
  drops the parent while a child ran: a run whose items all come out of the resume cache
  leaves the parent's line alone at the bottom and counted a call nobody made (measured:
  0 reported against 1). `map_start` and `map_complete` name that parent explicitly, stage
  and depth, so the level is taken from what the journal says about it rather than from a
  shape sniffed on it; a journal too old to carry the event keeps the depth rule, what it
  loses there being a gap that does not exist rather than a count declared wrong.
  Drafting is untouched: its `map:` is called `drafts` and never collided.
  Measured on 2026-09-02 on the 20 runs of a development checkout whose journal survived:
  no exit 3, four measurable classification legs. 12 against 12, 0 against 0, 27 against
  28 (the single RALPH retry being the whole gap, on a leg older than attempts), and **0
  against 36**: twelve children that exhausted three attempts each, and a leg that
  recorded none of them.
  A run that recorded no leg has no floor at all any more, and that is what `call-floor`
  now says (STU-1092): `reported_calls` comes out `null` and the command exits 2, instead
  of recomputing. What was left to recompute from was `logs/drafts/journal.jsonl`, whose
  line is a mailbox write and not a call: before STU-1030 the drafter held the write tool
  and rewrote on every revision, so one run left 81 `ok` lines for 14 conversations, which
  the factor of 2 doubled into 162 reported against the 48 calls of its journal. The gap
  was negative on two of the three recomputed production runs, and on none of the seven
  that recorded their leg. A run old enough to have recorded nothing is also old enough
  that any recomputation is a guess about code that no longer exists, so the STU-912 rule
  decides: a skipped measurement must not read as a passed one. The floor is guarded in
  passing: exit 3 when what is reported exceeds the journal, a floor above its own ceiling
  being a wrong count and not a low one.
  And the gap falls entirely into `unpriced_calls`, measured: a `map:` item that fails
  reports no `token_usage` at all (8 items of 8 on that run). The dollars, for their part,
  are not undercounted by retries: the kernel's stage-executor accumulates usage on every
  attempt, so a stage's total already carries its discarded attempts. What is wrong is the
  count, and only where there is nothing to price: the field whose whole job is to say how
  far the bill is a floor.
  `usd_per_drafted_conversation` is the only per-unit figure the pipeline governs
  (STU-1014): cost per email moves with the number of threads that deserve a reply, a
  property of the mailbox. It is therefore the drafting stages over what `drafts-post`
  says it drafted, and a run that drafted nothing returns `null`: a real amount divided by
  a denominator the run never had is an invented figure, the same reason a model with no
  price raises. The denominator is read on the very line that carries the drafting spend,
  never elsewhere: both halves of the figure then necessarily come from the same run.
  Three stage names for two sources (the Studio journal separates `generate-draft` and
  `draft-qa`, the run's artifacts report the pair as a single `drafts` leg), disjoint, so
  a single list answers both without counting anything twice.
  A run id can carry several *executions* of the drafting leg, and reading all of them is
  what was missing (STU-1111): a run killed mid-fan-out resumes through `studio run drafts`
  under the same run id (it has to, the per-draft journal scopes on it, STU-1003), so
  `logs/drafts/stages.jsonl` carries several lines for one run and every reader took the
  last. A production run thus published the 4 conversations its resume drafted, and the
  $8.97 the resume spent, as the run's: $2.244 per conversation, above the audit's 1.50
  threshold for a reason that says nothing about the drafter. `cost` and `call-floor`
  therefore sum every execution. An execution that left no terminal line stays what
  STU-917 made it (a stage killed before `drafts-post`, which records nothing), so it is
  counted and named (`unrecorded_drafting_executions`) instead of being silently dropped
  from the sum: spend and call count are floors, and the denominator comes out `null`
  rather than the figure of the executions that closed, carrying the run's name. That is
  the rule that already makes a run that drafted nothing return `null`, applied to a run
  whose denominator is not in the file. In passing, `call-floor` reads *all* the Studio
  journals that name the run instead of refusing when there are two: each taken alone is
  half the ceiling, and that is how that run's 20 reported calls came out as 3 against the
  14 of its resume's journal. Over both, it is a single undercount for the run: 65 calls
  made, 45 not reported, 7 of them RALPH retries. The sum is per journal and not over the
  concatenation, `journal_calls` counting a stage at the deepest level its name appears: a
  journal that nested the fan-out one level lower would carry off the other's stages.
  The Studio journal remains the fallback for a run older than this change, and what it
  cost is what motivated STU-1012: Studio writes it next to the cwd, so a run started from
  a worktree left the only record of its cost there, and a removed worktree took it away
  (seven of the eight latest reports). On that path, a `call:`/`map:` stage re-emits its
  children's tokens, so only the deepest level counts each call once, and
  `rollup_mismatch` cross-checks that choice against the total the root pipeline
  reported: a disagreement invalidates the figure rather than publishing it.
- `email_classifier/journal_sequence.py` + `scripts/audit_run.py` `sequence`: which
  conversation started which stage, in a journal the fan-out wrote from several places at
  once (STU-1112). A drafting run's journal is a single file every conversation writes to
  at the same time, all under the **root** run id: `stage_complete` carries `stage`,
  `status`, `output` and `depth`, and nothing that names a conversation. Reading it top to
  bottom therefore gives an order that is true of the file and false of any thread: a table
  traced that way put four `draft-qa` passes on a group capped at three, and the wrong
  count became a premise of STU-1104 before anyone saw it.
  The attribution is in the journal, in the two fields nobody read together:
  `stage_context` carries the run id of the **child** the stage belongs to, and
  `map_item_complete` carries that same id beside the item's `map` and `index`. A child's
  stages being strictly sequential, its execution of a stage opens at its `stage_context`
  and closes at the next event that child emits: its next `stage_context`, or its
  `pipeline_complete`.
  That leaves the case file order does not settle: a `stage_complete` of a stage two
  children have opened. It is settled by looking at which one moves on. A candidate whose
  own next event falls before the *next* completion of that stage cannot be waiting for
  that one, so the completion in hand is its own. Exactly one candidate forced that way is
  the answer; everything else is reported unattributed rather than guessed. A mismatched
  verdict puts one conversation's approval on another's body and the table reads exactly
  as right as a correct one, which is the command's whole reason to exist, and that is the
  STU-912 rule.
  Measured on 2026-09-01 on the eight multi-conversation journals available from a
  development checkout and a staging host: of 112 completions of `generate-draft` and
  `write-draft`, the 110 whose fan-out item finished land on the child whose item names
  the thread their own output names, zero disagreements. Those two name their thread and
  are therefore verifiable; `draft-qa` names nothing, and the rule exists for it. The
  naive rule (take the child named by the next id-carrying event) gets 109 of 110 right
  and fails on the one where a neighbour's verdict falls between a completion and its own
  child's next `stage_context`. No conversation of a measured production run comes out
  with more passes than the group's `max_iterations: 3`.
  `qa_coverage.groups_from_journal` builds its groups on this since STU-1158, so it is
  also the only journal reading the coverage harness knows.
- `email_classifier/alerts.py`: out-of-band alert channel (STU-930). A CRITICAL log line,
  plus a plain-text POST to `$ALERT_WEBHOOK_URL` when the host configures one (ntfy, Home
  Assistant, Slack). Without it, a dead grant only shows as the mailbox's silence, days
  later. `notify` never raises: the caller is already handling a failure.
  Pushover is the second channel (STU-1066), and on a production host it was the only one
  actually there: `ALERT_WEBHOOK_URL` had never been set, so *every* alert of the system
  (lost grant, provider limit, lifecycle event, a catch-up's spend warning) was only a
  journald line on the one machine that runs with nobody watching. Pointing the first
  channel at it does not work: its endpoint wants form fields (`token`, `user`,
  `message`), and a raw-body POST is a 400. The two channels are tried independently: a
  host that sets both gets both, and a dead channel does not take the other down. The
  message is truncated to 1024 characters, since Pushover refuses longer with a 400: an
  alert carrying a traceback must not become a silent delivery failure, exactly the
  silence the module exists to end.
- `email_classifier/detector.py`: the name detector, loaded once per server and not once
  per run (STU-938). `_model()` already caches for the life of a process, which is the
  whole batch on `make run`; the push path has no such process. A notification that owes
  a reply starts `scripts/drafts_pre.py`, then one `org_memory-read` and one
  `mail_precedents-search_mail` per drafting agent, each an ephemeral Python that reloads
  GLiNER. Measured on a production host, warm HF cache: 19.9 s of `from_pretrained` plus
  5 s of imports, 2.8 GB peak, for 0.33 s of detection. The webhook therefore serves a
  detector on a Unix socket and exports `EMAIL_CLASSIFIER_DETECTOR_SOCKET`, which the
  `studio` children and their tool subprocesses inherit. The batch path did not inherit it
  (its targets are not children of the webhook), so `scripts/with_detector.py` opens the
  same service for the duration of a command, and `run`, `run-fetch`, `run-batch`,
  `mailbox-full` and `redraft` prefix it (STU-1020). Three of those five entries are a
  `studio run`, a Node process: the context manager cannot live "in each script". `run`
  and `run-batch` already tokenize in a single process and gain nothing; they carry it
  anyway, since a rule with no exception list does not go stale the day an agent gains a
  tool. The price is memory: a batch run holds the same 1.76 GB as the webhook, and the
  two stack if they overlap. `anonymize._person_spans` is the only client: the tokenizer,
  the redaction of the org memory and the precedents sweep all go through it. Loaded
  lazily: 1.76 GB resident, spent only when something actually asked for a name, and not
  on a mailbox that never escalates. Two rules carry the safety: a server that fails
  **raises** instead of returning an empty list, since an empty list means "nobody here"
  and would leave every name in the text in clear (STU-890); and a socket that cannot be
  found is not a failure: the client reloads locally, so tests and any command run outside
  a `make` target are unchanged. The price is that a stale
  `EMAIL_CLASSIFIER_DETECTOR_SOCKET` silently brings back the old slowness, hence the
  WARNING.
- `email_classifier/learn/`: store of LLM verdicts + promotion into deterministic rules
  (STU-822).
- `email_classifier/anonymize.py`: PII tokenization that stays stable across the whole
  batch (STU-843). Studio's middleware does not reach the child runs of a fan-out, and
  since STU-1009 nothing relies on it: `pipeline.run` used to delegate to it, so
  `make run` and the push path sent every name in clear (the kernel-side detector anchors
  a name on a salutation, the one place a French email does not put one, STU-890) while
  `provenance.anonymized_fields` announced six fields. Formats (email, phone, SIN, card)
  come from regexes; names come from GLiNER at 0.3, the `ner` extra, and the module
  refuses to tokenize without it (STU-890).
  A name the keymap already holds does not go back through the detector (STU-1046): the
  detector's recall depends on how much text surrounds the name, so a first name claimed
  in one message went out in clear in the salutation of the next. Measured on a
  production deployment's real corpus: 4 of the 25 occurrences of two first names the
  keymap knew, all well short of the 384-wordpiece cut. A keymap value is a fact: it is
  matched directly, case-sensitively and on whole words (`marc` the common noun is not
  `Marc`, and `Marcel` is someone else), as a `person` candidate so that an address
  containing the name stays a single `EMAIL_n`. Shrinking the detector window was the
  other answer and costs more than it returns: measured on the same deployment's archive,
  a smaller window claims more of everything, and the false-positive rate rises with it.
  A word of a value the keymap holds only whole is redacted too (STU-1060): a full name at
  the top of a form notification did nothing for the bare first name the sender signs at
  the bottom, and both being in the same window, the first one's mint was not yet
  available to the second. The component pass therefore runs *after* the windows, once
  everything the text caused to be minted is in the keymap, wherever it came from. The
  component carries the token of the whole value and never mints one of its own: a
  second token would give one person two identities, the very failure STU-920 exists to
  prevent. A component two people share names neither, so it goes out as `PERSON_X`
  rather than asserting one, the shared keymap accumulating from run to run. Split on
  whitespace: `Marie-Claude Roy` offers `Marie-Claude` and not a bare `Marie`, which
  nobody wrote alone. The announced price (precision) is zero, and that is checked rather
  than argued: span observation reads only `person_spans`, which this rule does not touch,
  so `make eval-claims` stays green and a published archive rate cannot move.
  A signature block is the third lever, measured then kept (STU-1224): a confident full
  name higher in the text *suppresses* a bare first name lower down. On a body a
  production run sent in clear, the detector scored the signature 0.19 against 0.96 for
  the full name of the sender's daughter two lines above, and the same signature rises
  back to 0.81 when the daughter is named with one word. 15 of 23 short first names
  replayed in this form fall under 0.3, so this is a class and not one name.
  `_signature_spans` therefore claims, without a model call (like `_known_person_spans`),
  a capitalized word alone on its line between a closing formula **and** a phone number,
  an address or a mail-client footer. Both sides and not one: measured on a production
  archive, the strict form adds a handful of spans and no false positive, while one side
  alone, or a lowered 0.15 threshold, adds an order of magnitude more and raises the
  false-positive floor and ceiling (the lowered threshold is STU-1046's smaller window
  under another name). What this does not cover is said: a first name suppressed *in
  prose* stays unclaimed and unmeasured, since a precision sweep cannot see what nobody
  claimed. What is left to the detector alone is the name no message of the batch has
  yet made it claim.
  Weights load on the GPU when there is one and on the CPU when it is full (STU-984):
  the GPU is an optimization, never a requirement, and a one-shot process declines it up
  front (`stay_on_the_cpu`, 9.9 s on CPU against 12.0 s on GPU to tokenize a single
  file). The fallback is logged at WARNING with the CUDA error, the card and what was
  left free: a run that became several times slower without saying so cannot be told
  from a broken run. Measured with the card held at 8 MiB free (STU-991):
  `tests/test_anonymize.py`, corpus deselected, passes in 943 s, against 421 s for its 49
  tests when the card frees up midway, and a deployment's whole-corpus "no person name
  survives" test does not finish under 1800 s while other suites compete for the cores.
  That is the price of the fallback, not a fault in it.
  The vouched-word list (common words plus the vocabulary of the skill the policy's
  `vouched_skill` names) removes the little that GLiNER over-claims, and
  `email_classifier/precedents.py` uses it too. The veto holds word by word (a single
  unvouched word keeps the whole span), and a word carrying a digit names nobody: without
  that, `U15` was on no list and `The U15 girls` minted a `PERSON_n` on the
  organization's own vocabulary (STU-986). Vouching the categories from the skill would
  cover only the spelling it publishes (`12U`), and the sender who writes picks the other
  one. Word by word rather than a clean veto: `Alicia 12U` is the span the detector
  returns, and it stays tokenized whole, over-redacting the category rather than letting
  the name leak.
  A role address (`no-reply@`, `notifications@`, the `_ROLE_LOCALS` list) passes in
  clear: it identifies nobody, and its domain is the signal that tells a vendor notice
  from phishing (STU-902). A name glued to the end of the text goes out as `PERSON_X`
  with no keymap entry: Gmail cuts `snippet` mid-word, and minting `Carolin` next to
  `Caroline` gives the same person two identities for the life of the keymap (STU-920).
  Only a name minted by the current batch keeps its token at that position: a keymap
  written before this rule carries the fragments themselves, and reloading it would give
  `PERSON_9` back to `Carolin` forever (STU-922). The same load drops a `PERSON_*` entry
  the veto refuses today. No lexical rule tells a fragment from a real name (measured:
  GLiNER claims `Carolin` and `Camil` in isolation and drops `Florence`), so fragments
  already on disk are deleted by hand, once.
  The shared keymap accumulates: it describes the machine, not a run, and an audit that
  cites its entry count as a run's overstates it several times (STU-924: 97 attributed to
  a run whose text reaches 36). Each run therefore also writes
  `logs/anonymization/<run-id>.json`, the entries it minted itself. Merged, not
  overwritten: `mailbox-full` tokenizes twice under one run id, and `drafts`, which seeds
  from the shared keymap, counts the entries of `classify-batch` as reused; the union is
  the run's. That file is the denominator of a *precision* measurement; the shared keymap
  is only the seed that keeps a token stable from one run to the next. Recall is no
  longer counted on it (STU-1036): a production run minted zero entries (every value came
  from an earlier round), so a replay "that reproduces exactly the minted entries" proved
  nothing. Each run also writes `logs/anonymization/<run-id>-sent.jsonl`, the payloads as
  it handed them to stage 2, and that is what `scripts/audit_run.py recall` decides on:
  the assembled payload and not the tokenizer's output, an out-of-scope field being
  exactly where a leak sits. Three writers, the same as `record_minted`, and one run id
  covers two stages, hence the append. `logs/anonymization/` takes the age and
  protections of `reports/` along the way (STU-1036): without a shelf, the evidence of a
  recall expired at 30 days in `.studio/runs/` while the report it describes was held
  indefinitely by `audit-gate`. `redraft` writes none: it has no run id, and no report to
  attribute it to.
- `email_classifier/club_memory.py`: the `org_memory-*` tools (renamed from
  `club_memory-*` by STU-1464, with `club_web-search_site` becoming `org_web-search_site`:
  a generic kernel does not ship a tool named after one organization, and `org-` is the
  prefix the skills of `reference-deployment/` already carry; the module,
  `email_classifier/websearch.py` and the `club-memory/` store keep their names, the tool
  identifier being the only part of the agents' contract). A store of facts learned by
  email (STU-873): one fact per Markdown file under `club-memory/` (gitignored) plus a
  `club-memory/MEMORY.md` index of one line per fact. The index travels in the fan-out item; the file
  costs a tool call. Both go through the batch tokenizer before the prompt (STU-879),
  because a memory edited by hand holds real names and a value the keymap does not know
  goes out as an unindexed placeholder, but only between tokens (STU-981): running the
  detector again over an already written `PERSON_53` re-mints it under a number the
  keymap does not know, and the fact's source came back as `PERSON_X` with nobody to name
  it. The skill and the live site win over a memory: a memory that contradicts them is
  flagged, never deleted by the agent.
- `email_classifier/websearch.py`: the `org_web-search_site` tool, a live search over
  the pages of `rules/web_sources.yaml` (the only allow-list: the agent chooses the
  words, not the destination), cached 1 h under `dumps/web-cache/` (STU-871).
  `fetch(url, max_age=0)` reads past the cache, which a site-freshness check needs
  (STU-1094): a fan-out warms the cache for an hour, so a record published from it is a
  measurement of the past.
- `email_classifier/precedents.py`: the `mail_precedents-search_mail` tool, a search
  over the mailbox's earlier mail (sent replies included). Each hit goes through
  `email_classifier/anonymize.py` and then a reverse redaction pass (a capitalized word
  becomes `NAME_X` unless `anonymize.vouched()` vouches for it), with unindexed
  placeholders: a precedent is read, never restored. It grounds the wording, never a fact
  (STU-872).
- `email_classifier/orchestrate/conversation.py`: the thread reconciliation rule
  (STU-940) and the `collapse` that feeds it (STU-944), stated above; pure, no I/O.
- `email_classifier/orchestrate/batch.py`: the logic shared by the script stages of
  `classify-batch` (fingerprints, fan-out items, verdict assembly).
  `anonymize_escalation` is the single statement of what a scope tokenizes before stage
  2, called by both orchestrations (STU-1009): `pipeline.run` refuses to start if a policy
  sets a scope and the caller brings no tokenizer, because the branch that sends the
  mailbox in clear must not be the one you get by forgetting.
  `prompt_fingerprint` covers a YAML source by its **parsed document** and everything else
  by its bytes (STU-1149). The fingerprint travels in the fan-out item, which *is* the
  `map:` resume key, so what it covers is what an unchanged corpus pays for again at
  stage 2. Hashing the bytes of `.studio/agents/classifier.agent.yaml` re-priced the whole
  grey zone over a `# ...` line no model reads, and an agent file is exactly where a
  deployment writes its measurements: of the last three commits that touched one
  production deployment's classifier agent, one changed 34 lines, all 34 of them
  comments. The scope the fingerprint claims is "everything that shapes a verdict except
  the email", so the bust was the fingerprint missing its own contract, and the cost was
  only the symptom. Mapping keys are sorted and sequences left as they are, decided once
  for every YAML rather than inherited: reordering an agent's keys changes nothing that
  reaches a model, while `required_fields` is a sequence and stays order-sensitive.
  `.studio/invariants.md` and `.studio/skills/taxonomy.skill.md` keep their bytes, and not
  by omission: they reach the prompt verbatim, so a comment there is prose the model
  reads. An unreadable YAML falls back to its bytes instead of raising: the function must
  never under-cover, and a broken file is the loader's error, better named by it.
- `email_classifier/orchestrate/drafts.py`: the same role for `drafts`: selecting the
  `needs_reply`, the fan-out items (STU-850). The item carries `body_text`, the full body
  that `drafts-pre` re-reads for this subset only (STU-852). On Graph it is the same
  `uniqueBody` classification already read (STU-935); the call remains because the path
  is provider-agnostic, and Gmail's `snippet` really is an excerpt.
  What leaves the selection is decided by the conversation's state, not by the mere
  presence of a draft (STU-884, then STU-945). An unsent draft a human may have started
  editing always blocks (overwriting their work stays the worst case). A thread whose
  latest reply from us is later than the email is not answered twice. A draft a human
  overtook by replying from the mailbox is journalled `superseded` and left in place,
  never rewritten. What remains is exactly the case in the title: new mail arrived after
  our sent reply.
  The symmetric case is journalled too, as `vanished` (STU-998): a draft deleted by hand
  leaves no trace (`undo` and `redraft` both write `deleted`, a human who clicks writes
  nothing), so two `draft_id`s on one conversation from one run to the next look exactly
  like the "one draft per conversation" guard having given way. That is how an audit of a
  production run read it, and it was not that: in both cases cited, the earlier corpus
  carried the drafts and the later one no longer did, three older messages having slid
  under the cap of 25 in their place. The loss is named once (STU-1028): a `vanished`
  row carries a `draft_id` too, so left as the thread's last word it would report the
  same loss on every later run, and `drafts` is precisely the stage tolerated in failure
  (STU-850), so the run that names the loss is often the one that dies before replacing
  the draft. A count of `vanished` rows would then stop being a count of lost drafts, the
  only thing the row exists to make measurable. It therefore settles the loss the way
  `deleted` settles it, and the next one waits for a draft made and then lost in turn.
  Sending a draft also takes it out of Drafts, and that was most of the count
  (STU-1549): of 22 `vanished` drafts on a production host on 2026-09-20, Graph still
  held 19 as sent (`isDraft` false with a `sentDateTime`), 2 answered 404 and 1 neither.
  `drafts-pre` therefore asks the mailbox where the draft went before writing the row
  (`drafts.sent_at`): a sent draft is journalled `sent` with its send time, which settles
  the loss the way `vanished` does; `vanished` keeps meaning deleted or unreachable, and
  `scripts/audit_run.py drafts` counts the two apart. Both halves of the test are needed,
  measured: 3 drafts still `isDraft: true` carried a `sentDateTime` on 2026-09-27. On
  Gmail a sent draft changes id and answers 404, so it stays `vanished` there.
  A draft this pipeline wrote and nobody touched is not a human's, and that is the third
  answer (STU-886). `blocked` only asked whether a draft existed, which defeats the
  prompt fingerprint, whose job is exactly the opposite: `_PROMPT_SOURCES` covers both
  agents, their skills, the contracts and the tools so that editing one re-drafts every
  pending email, and an email held behind `blocked` is never in the fan-out to notice. A
  fee corrected in the org-facts skill therefore stayed wrong in every draft written
  before the correction. Ownership is read in two halves, both needed: an `ok` row of the
  journal names the draft (`our_draft`, keyed by `draft_id` and never by run, since the
  run that wrote it is by construction an earlier one, and only an `ok` row is a write,
  `superseded` and `vanished` being reports made by code that did not compose it), and
  its `lastModifiedDateTime` has not moved since (`edited_since`, taken out of `undo`
  where it has answered the same question since STU-854: two definitions of "a human owns
  this draft" would answer it two ways). Only then does the fingerprint decide: different
  from today's, the verdict is `redraft` and the email goes back into the fan-out,
  `upsert_reply_draft` rewriting in place. `write-draft` therefore journals the
  fingerprint it wrote under, the only place the answer can come from. A row that names
  none reads as older than today, the one place this answers a question it cannot
  measure: it costs one re-draft per conversation still carrying a draft from before the
  field, paid once, and it cannot overwrite anything a human owns, since the edit test
  runs first. Reading it the other way would freeze exactly the drafts this unfreezes.
  `vanished` stays bounded to `DRAFT` alone and never `DRAFTABLE`: a re-drafted
  conversation carries exactly the draft this report would call lost.
  `last_sent_at` reads this from a `$select=id,receivedDateTime` on Sent Items, not from
  `thread_history`: the selection also asks it of threads it is about to skip, and a turn
  body is what it would pay for them. The item also carries `reply_to`/`reply_name`
  (STU-888), empty unless a deployment's envelope rewriter fills them (a contact-form
  notification, see `email_classifier/envelope.py`). They are parsed before the
  tokenizer, so the address keeps the token of its copy in `body_text`, and `write-draft`
  restores both at the mailbox boundary. And `thread_history` (STU-941), the rest of the
  conversation as `{date, direction, body}` from oldest to newest, without which "ok but
  what about Saturday?" has no answer. `direction: sent` comes from the message's
  membership in Sent Items, the only way to know a turn is ours without asking the grant
  who we are; the listing is the whole mailbox and not the Inbox, because a turn an
  earlier run applied has left it. The bodies are tokenized with the batch keymap like
  `body_text`, never flattened like a precedent: it is the same exchange, so its
  `PERSON_1` is the person the draft addresses.
  The draft's register comes from the deployment's voice skill (`.studio/skills/org-voice.skill.md` in
  the reference deployment) and not from the agent's prose (STU-1007), each of its rules
  a count over the deployment's sent archive. The voice skill is in `_PROMPT_SOURCES`
  through the drafting agents' declared skills: a fixed list would let an absent skill be
  edited without re-drafting a single pending draft. `anonymize.vouched()` reads only the
  skill the policy's `vouched_skill` names (the org-facts skill), which is what keeps the
  synthetic names of the voice exemplars from becoming vouched vocabulary.
  The item carries a second field that is not the email (STU-1119): `written_period`,
  `day` or `evening`, the organization's clock when the draft is written. The drafter
  cannot read a clock and the item is all that reaches the prompt; it is the writer's
  time and never the reader's, the only one of the two a run knows. The boundary is
  `evening_from_hour`, measured on the deployment's archive and moved out of this module
  into the deployment's voice baselines (rules/voice.yaml, STU-1472); on a deployment
  with no measured cutoff `period_at` never guesses a split it has no basis for. Two
  values and not a timestamp: the item is the resume key, so a timestamp would break the
  cache on every call where a period flips once a day.
- `email_classifier/orchestrate/stage2_client.py`: the stage-2 boundary, one
  `studio run classify` per conversation, the error returned as data. What a failure
  logs is what the child says about *itself* (run status, stages, errors, attempts),
  never its raw `--json` (STU-1010): that record re-emits the input, `from_address`,
  `subject` and `body_snippet` included, and on a serving host the line goes to
  journald, which neither the policy, nor `scope: full`, nor the keymap reaches. An
  allow-list and not a deny-list, because a deny-list would have to know every place a
  future Studio version decides to re-emit the input. What is not a record (a record cut
  in half by a kill, a traceback, a usage line) keeps its tail, with the values we sent
  removed: dropping it would bring back the failure with no output that STU-820 fixed.
  The `thread_id` stays: it is the handle by which a failure is diagnosed, and it names a
  conversation, never a person. `error_detail` goes through the same filter, since it
  lands in the report, on the same disk.
  A failure the *provider* caused, and not the contract, carries its own tag,
  `provider_unavailable`, and goes through `alerts.notify` (STU-1016). Under
  `defaults.provider: claude-code`, an exhausted session limit comes back as the agent's
  *answer*: RALPH reads a string where the contract wants an object, retries twice
  against the same wall, the stage fails on a contract violation, and the webhook logs
  and drops the message on purpose. Same class of failure as a lost grant (STU-930),
  except that a grant raises and a limit answers; in both cases the silence cannot be
  told from a quiet mailbox. The notice is read on the whole stream, mail removed, and
  not on `_studio_output`: the allow-list does not let it through, it lives in
  `agent_output_raw` next to the re-emitted input, and widening the allow-list would
  publish the email with it. Only the matched pattern is reported, never the text around
  it, and one alert per classifier: a limit answers the same way for every conversation
  of the run, and one alert per unclassified email would bury the single fact behind it.
  Under `anthropic` (STU-1013) the same failure takes another shape, and it had to be
  measured rather than guessed: there is no CLI to answer in prose, the API refuses, and
  the engine writes the error on the *agent run*. Measured with an invalid key:
  `401 {"type":"error","error":{"type":"authentication_error", ...}}`, in **one**
  attempt and not three (RALPH does not replay a transport error, so nothing in the run
  absorbs it). What is matched is the envelope (the HTTP status and the `type`), never
  the message: the shape is what was observed, and a list of messages would be a guess
  about the ones not seen. `_run_record`, whose default error is the prose
  `anthropic 529 overloaded`, is the regression test: no envelope, not the provider.
  None of this reaches the `classify-batch` path: the failure arrives there as an
  engine error message and not as agent output, and what that message says about a
  limit has not been measured. One shape has been, and it is handled upstream rather
  than here (STU-1128): `Provider "x" failed to initialize`, which the runner's registry
  raises on a missing credential. It names not an email but the run's configuration, so
  the refusal is at preflight; one more pattern here would catch it once per
  conversation, after the spend.
  The `--anonymize` branch was checked against Studio's `--json` masking (STU-1546).
  Studio 0.22.0 masks keymapped values in the run record (STU-1528), and the branch is
  reached only by `eval ab`'s anonymized arm (a run tokenizes first, STU-1009), so
  nothing on the push path could have moved. Measured 2026-09-25 under Studio 0.28.0,
  `claude-code`, scope `full`, three escalated fixtures x 2 repeats x 2 arms: 0 stable
  divergences, and all three verdicts equal the deployment's frozen stage-2 golden. The
  anonymized arm's `reasoning` carries the real first name and no `PERSON_n` token, so
  the parse reads deanonymized output and `--include-cleartext` is not needed. No 0.21.0
  build was available to replay against; the cleartext arm, which never passes
  `--anonymize`, and the golden are the baseline. A run outside a `make` target needs
  `STUDIO_PYTHON_BIN`, or the contract validator looks for `.venv/bin/python` in the
  worktree and every attempt fails.
- `scripts/classify_pre.py` / `scripts/classify_post.py`: the two script stages of
  `classify-batch`.
- `scripts/drafts_pre.py` / `scripts/drafts_post.py`: the two script stages of `drafts`;
  `scripts/write_draft.py` is the third, the one that writes the draft into the mailbox
  (real Outlook drafts, never sent). It sits in `email-draft`, between `generate-draft`
  and `draft-qa`, because the write belongs to the run and not to the model (STU-1030):
  the agent held the tool and called it again on every revision (28 Graph writes for 5
  attempts on one production run, three drafts produced). A prompt that says "exactly
  once" is not a mechanism, and the contract cannot cap one tool among others
  (`minimum`/`maximum` count every tool alike). One execution of the stage is one write,
  so a journal row counts one attempt; and anti-theatre leaves with the tool, the
  `draft_body` the reviewer judges being by construction the one Graph received.
  `drafts` is the only stage of `mailbox-full` tolerated in failure, so it is the only
  one whose failure has to be *written* (STU-917): `drafts-pre` opens the run in
  `logs/drafts/stages.jsonl` with `started` and the `needs_reply` count before touching
  the mailbox (a stage killed where Python cannot catch it still leaves a trace, and a
  `started` with no terminal row *is* that death), `failed` carries the reason, and
  `drafts-post` closes with `completed`. Without this, a dead stage and a mailbox with
  nothing to answer leave the same artifacts: none.
  Every row of the per-draft journal names its run for the same reason: the file is
  shared, two runs on the same mailbox share most of their `needs_reply`, and scoping by
  message id alone gave 9 of the previous run's drafts to a run that had made none. The
  run id travels in the fan-out item, hence in the resume key; the price is close to
  zero, since `drafts-pre` already drops any mail that carries a draft (STU-945) and a
  replay of the *same* run keeps its run id. The `drafted` count `drafts-post` writes on
  the `completed` row is scoped on the same field (STU-1003): counted by message id, a
  draft a human deleted puts its email back in the next run's fan-out, which then claims
  a draft it did not make, and that figure goes straight into the file the audit reads.
  The date separates nothing either, and errs on the side that looks right: the stage
  writes its rows *after* its report is on disk, so inside the window of the run that
  started in the meantime. A row with no run id (from before STU-917, or from `redraft`,
  which answers a human) is attributable to nobody, and the audit counts it as such
  rather than guessing. `undo`, for its part, still scopes by the report and not by this
  run id: the draft an earlier run put on a thread this one also marks `needs_reply` is a
  draft this one would have made.
  The terminal row finally carries what `drafts-pre` decided (STU-1186), STU-917's
  argument one field further: `needs_reply` larger than `drafted` has two opposite causes
  (a conversation held on purpose, so nothing was owed, or a reply the leg selected and
  lost, so a sender is waiting), and the row did not tell them apart. A production run
  wrote `needs_reply: 1, drafted: 0` in 1.6 s without a call, and it took reading the
  Studio journal (gitignored, pruned at 30 days, STU-1012) to find the thread already
  carried a draft. `selection` therefore carries `selected` and two maps, and they are
  two because they do not fall on the same side of `needs_reply`. `held` counts the
  conversations `reply_targets` did select and the mailbox state held back, under the
  `conversation_verdict` that holds them (`blocked`, `answered`, `superseded`: three
  distinct reasons, and "already drafted" would be false of the one where the mailbox
  carries no draft), so `selected` plus `held` makes `needs_reply` and `selected` minus
  `drafted` is what the leg lost. `unreconciled` counts what `reply_targets` never
  selected (`held_replies`: a thread not decided in full, a clamped action), outside
  `needs_reply` by construction, since clamping can only divert an action *away from*
  `needs_reply`. Adding them is exactly the arithmetic the separation exists to prevent.
  A row without the field reads `unrecorded` and never "zero held": `started` and
  `failed` are written by a stage that did not decide, a row older than the field has
  nothing to say, and returning zero would claim a reply was lost where nothing was owed,
  the direction of STU-912. `scripts/audit_run.py drafts` therefore reads both files, one
  entry per execution and never folded (STU-1111): a resume re-selects from what is left,
  so summing the `needs_reply` would count the same conversations twice.
- `email_classifier/commitments.py` + `scripts/commitments.py`: what a drafted reply
  commits someone to (STU-1065). A body that says a message was forwarded, or that an
  answer will follow, is true only because a human does it before sending, so the
  obligation rides on the draft staying a draft. `write-draft` appends each such sentence
  to `logs/drafts/commitments.jsonl` beside the journal, once the mailbox accepted the
  body, so a run that dies before `drafts-post` has already recorded it (STU-917). An entry
  is keyed by conversation, draft and sentence, so a re-run adds nothing; a rewrite of the
  same draft that drops the sentence marks it `withdrawn` (the group's next attempt fixing
  what the reviewer rejected is the usual case); otherwise only `make commitment-settle`,
  with a mandatory note, closes it, never a later run's silence. Detection is a pattern
  list over claimed handoffs and promised follow-ups in French and English, not a field
  the drafter fills: the drafter is the party told not to promise, so it would be asked to
  report its own defect, and a contract field would make every deployment's drafter prose
  a fingerprinted edit. The price is recall: a promise phrased outside the patterns is not
  listed, so an empty list means no matched sentence, never no commitment. The sentence is
  stored as the drafter wrote it, tokens included. A draft deleted by `undo` or `redraft`
  leaves its entries open, the direction that over-reports.
- `email_classifier/calendar_plan.py` + `scripts/calendar_pre.py` /
  `scripts/calendar_post.py`: appointments read out of classified mail and planned as
  calendar events (STU-2089), the `calendar` child of `mailbox-full`, a no-op unless the
  policy carries a `calendar:` block (`subtypes`, `rules`, `entity`, `timezone`,
  `default_minutes`).
  The pre-stage selects the report rows whose subtype is listed, minus the message ids
  either journal already holds, tokenized with the batch keymap; the
  `appointment-extract` agent reports the date and wall-clock times as written and the
  sentence that states them, and never an offset. The module does what can be checked:
  the evidence is in the message, a stated weekday matches the date, a missing year is
  the next occurrence after the message, times are placed in the policy timezone. A
  failed check or a low confidence goes to `logs/calendar/review.jsonl`, never to the
  plan. `logs/calendar/events.jsonl` is append-only, keyed `ref:<booking reference>` or
  `msg:<message id>`, last row wins; a reschedule or cancellation finds its event by
  reference, then by sender domain and previous start, and more than one candidate is a
  review, not a pick. A deterministic verdict carries no subtype, so it is selected only
  when `rules` lists the id of the rule that decided it (STU-2102); calendar-pre refuses
  an id no deciding rule has. `email_classifier/apply/home_assistant.py` writes the plan
  (STU-2090) under the `apply` flag the mailbox writes take: every `planned` row no later
  `applied` or `failed` row of its key settled, so what a dry run planned is written by
  the next run that may write (the planner never reads that message again). A dry run
  prints each operation and sends and appends nothing. Create is the
  `calendar.create_event` service, which returns no uid, so the event is read back over
  its window and found by the `[mail:<id> ref:<ref>]` marker in its description; move and
  delete are the websocket `calendar/event/update` and `calendar/event/delete`, the
  instance listing no service for either. A uid comes only from an `applied` row: zero
  or several marker matches, or a move or delete with none, is a `failed` row with its
  reason and nothing is guessed. `HA_URL`/`HA_TOKEN` missing with something to write
  refuses and alerts. The websocket client is the `home-assistant` extra, imported only
  when a command is sent, and the suite fakes both transports.
- `email_classifier/placeholder.py`: the drafter's `[À COMPLÉTER : ...]` marker
  (STU-2002) read outside the body (STU-2049). Drafts are plain text, so nothing in the
  Drafts list told a draft that needs input from one that does not. `holds_placeholder`
  is the one test: the Outlook drafter sets the category `email-tool/à-compléter` in the PATCH
  that writes the body, and sends the whole set, so a rewrite without the marker clears
  it (only an untouched draft of ours is ever rewritten, so no one else's category is
  lost). `audit_run.py reviewed` runs `flag_sent` on the Sent Items bodies it already
  reads and alerts when a reply went out with the marker. Gmail and IMAP do not mark:
  an IMAP keyword is an ASCII atom, so `email-tool/à-compléter` cannot be one, and a second
  ASCII spelling of the same marker is a vocabulary nobody reads there yet.
- `email_classifier/apply/status.py`: one status category per conversation, on its
  latest inbound message (STU-2007), under the policy flag `status_labels` and outside
  `auto_apply`, since it moves nothing. With apply only planning, a draft in Drafts or
  nothing was the whole trace, and nothing read the same as a failure. Six names:
  `email-tool/brouillon` (a draft waits), `email-tool/à-compléter` (that draft carries the STU-2049
  category), `email-tool/pas-de-réponse`, `email-tool/déjà-répondu` (a sent reply is newer than the mail,
  or than the draft), `email-tool/à-toi` (a `content_hold`, or a reply the clamp dropped) and
  `email-tool/erreur` (an undecided conversation, or a reply owed that has no draft and no sent
  reply after drafting). The status is read off the mailbox once the drafting has run,
  through `conversation_verdict`, never off what the stage claims. The write is a state
  diff on these six names only: a human's category is never read or removed, the latest
  message's previous status is replaced, the thread's older messages lose theirs, and a
  message in the trash or elsewhere is left alone. Each write is journaled `ok` in the
  run's `logs/applied/<run_id>.jsonl` even while apply plans into `.plan.jsonl`, so
  `make undo RUN_ID=` takes the statuses off. The executor resolves only `email-tool/<bucket>`,
  so a status never reads as a filing, and the webhook refuses to start on a taxonomy
  whose bucket label would collide. Outlook only, refused at policy load elsewhere: an
  IMAP keyword is an ASCII atom and these names are accented, and Gmail has no thread
  reader.
- `email_classifier/apply/unsubscribe.py`: one-click unsubscribe from bulk senders the
  owner never reads (STU-2071), under the policy flag `auto_unsubscribe` (off by
  default), run by `apply_email.py run` and the `mailbox-full` apply stage right after the
  executor. A sender qualifies when one of the run's messages from it is an `ok` verdict
  in bucket C carrying `List-Unsubscribe-Post: List-Unsubscribe=One-Click` and an `https`
  URI in `List-Unsubscribe`, and the Inbox, Archive and Trash together hold at least two
  messages from that exact address with no `\Seen` flag on any of them. Only the RFC
  8058 POST goes out, redirects refused: a `mailto:` would send mail as the owner and a
  GET is a page visit. Each sender is posted to once whatever the answer, journaled
  (`sender`, `host`, `status`, `run_id`) in the deployment's `logs/unsubscribed.jsonl`,
  which is what the next run reads to skip it. A dry-run names the senders and posts
  nothing. The apply summary and the stage's `unsubscribed` output list them. IMAP only,
  refused at policy load elsewhere: it is the one writer that reads a sender's history.
- `email_classifier/mail_link.py`: an Outlook web link to the id it names. Outlook
  writes a `conversationId` in the URL when the pane shows a thread and a message id when
  it shows a single message, in the same form, so the mailbox decides
  (`fetch_input_by_reference`).
- `scripts/redraft.py`: the entry point of `make redraft`: resolves the link, deletes the
  draft in place, writes the item `email-draft` consumes.
- `scripts/fetch_mailbox.py` / `scripts/apply_report.py`: the script stages of `fetch`
  and `apply` (provider dispatch by policy; apply in dry-run unless `apply: true`).
- `email_classifier/voice_counts.py` + `scripts/voice_counts.py`: a typed habit against
  a pasted block (STU-1138). Every rule of the org-voice skill
  (`reference-deployment/.studio/skills/org-voice.skill.md` in the reference deployment)
  counts replies, and the tacit premise is that a reply carrying a form is someone who
  chose it. The non-breaking space before a colon is where that is false: measured on a
  production deployment's sent-mail archive, 42 replies against 79 with an ordinary
  space, a quarter of spaced colons, which reads as a minority habit worth a rule, and
  **40 of the 42 are the same pasted registration line**. STU-1131 only saw it by dumping
  the contexts and reading them by hand, which is not a mechanism. `make voice-counts`
  therefore counts the occurrences nobody pasted: two occurrences in two replies are one
  block when the text around them repeats word for word. Not a fixed window: as far as
  the repetition actually runs, the only version of "the same block" that survives a
  paste carrying a different link. A block counts for **nothing**, not for one: someone
  typed it once, but in none of these replies, and counting it as one puts boilerplate on
  the same footing as a form chosen here. The per-reply count is published beside it, so
  the author of a rule reads `42 replies, 2 distinct` and decides. The threshold is 40
  characters of shared context, and the finding does not depend on it (the 2 hold from 25
  to 80); what the threshold moves is the share of formal address that lives in a pasted
  block (356 to 571), which no rule depends on. The pattern runs on the **raw** body,
  never a normalized one: a non-breaking space is a `\s` to `re`, so normalizing first
  would erase the very mark being counted. Editing the skill turns `make qa-gate-check`
  red, and the repair is `make qa-gate`.
- `email_classifier/memory_harvest.py` + `scripts/memory_harvest.py`: the answer blocks
  the reviewer pastes into sent replies, proposed as club-memory facts (STU-2001). The
  drafter had none of them, so a recurring question got a draft without the dates or the
  referral the reviewer then pasted by hand. The detection is `voice_counts`' own, anchored
  on every line start, and a block's text is the longest run two of its replies share,
  cut back to a whole line: an edited paste never yields a line cut mid-date, and a reply
  that does not carry the text neither counts nor dates it. Sent Items are read through
  `sent_since` on the drafting readers (Graph `uniqueBody`, IMAP `Sent`), never a quoted
  chain. Nothing reaches the store unread: `make memory-harvest` writes
  `club-memory/harvest-candidates.json`, `make memory-harvest-approve` refuses while any
  entry lacks `approve: true|false`, and every decision is recorded with the text it was
  made on (`club-memory/harvest-decisions.json`), so a rerun proposes only new blocks and
  blocks whose text moved, and a rejected signature is not proposed again. A block is
  known by its name, the slug of its first words. Approved facts are written in clear
  through `club_memory.remember`; the index and `org_memory-read_memory` tokenize them on
  the way to the prompt like any hand-edited memory (STU-879).
- `email_classifier/tree_guard.py`: which tree a `--check` answers for, and the refusal
  when it is two (STU-1109). The checks of the family (in this repo `scripts/qa_gate.py`
  and `scripts/qa_coverage_gate.py`, and a deployment's own record checks) derive a
  fingerprint *now* and compare it to a record committed in *their* tree. Run bare from a
  worktree, the two halves come from different trees: the script and the record are the
  worktree's, while `email_classifier` resolves through the editable install to the main
  checkout, so everything derived from the package is `main`'s. Measured 2026-08-30 in a
  worktree, in the same second: run bare, a span-rate `--check` announced its curated
  keep-set moved and exited 3; under `PYTHONPATH=$PWD`, unchanged. The drift was `main`'s
  `_ROLES` (STU-1103, merged mid-session) read against the worktree's record, and nothing
  had moved. This is not an exotic invocation: STU-1091 made the direct call the
  documented way to tell a 3 from a 2, which `make` flattens. Both directions are
  reachable and the silent one is worse: a worktree whose record is stale but whose main
  checkout still agrees with it announces `unchanged`, the very silence STU-1047 exists
  to break. The refusal is therefore that of STU-1027 and STU-1087: exit **5**, both paths
  named and the repair in the message (`PYTHONPATH=$PWD`), rather than answering from
  whichever tree the import reached. 5 and not a reuse, by the rule that gave each code
  its number: 4 is already taken in the family. The test is mechanical and installs
  nothing: the module's `parents[1]` against the script's `REPO_ROOT`. `make` is not
  touched, every target already exporting `PYTHONPATH` (STU-867). The price of stating
  this once rather than three times is a one-merge window: a worktree carrying these
  scripts on top of a main checkout that predates them raises
  `ImportError: cannot import name 'tree_guard'` instead of exiting 5, the same refusal
  spelled worse, loud and naming both files.
- `email_classifier/seed/`: a seeder of synthetic mail into the Outlook test mailbox
  (STU-845); the teardown takes away the reply draft left on each seeded conversation
  (STU-885). A human review lives only in the folders it moved the messages to, so
  `make export-review` freezes it in `dumps/outlook-review.json` (folder and source
  fixture, the mailbox id expiring with the round), and `unseed` exports it before
  deleting, then refuses while a message is outside the Inbox (`make unseed-force` after
  reading it, STU-875). `make unseed RUN=<seed run id>` tears down one round only and
  leaves the others standing (STU-966): `--add` makes a multi-round mailbox a supported
  state, and the sweep was the only way out. The run id comes from the
  `x-email-classifier-seed` header every message already carries; the review is still
  exported whole, since a review exported at the round's scope would overwrite the file
  of the rounds that remain, and the outside-Inbox refusal looks at the scope, not the
  mailbox. A run id that nobody carries is refused, since sweeping nothing reads like a
  torn-down round. "Standing" means the mailbox and not the Inbox (STU-976): a round a
  review filed elsewhere is still there, described by the golden and the dump, so the
  anti-duplicate guard refuses on top of it and `--add` keeps it in both artifacts.
  Counting only the Inbox let a bare `make seed` rewrite the golden under messages still
  present, and `make eval-seeded` score fewer than the mailbox holds, without saying so.
  The teardown prunes both artifacts to what it leaves standing (STU-977), by the same
  rule `--add` applies when merging them: without it a torn-down round stayed described
  there, and a round-scoped teardown left a half-stale golden, which reads as live,
  `make eval-seeded` replaying the frozen dump and passing on messages the mailbox no
  longer has.
  `seed_thread` seeds a conversation as a real chain (STU-942): the chaining goes through
  `PidTagInReplyToId` and `PidTagInternetReferences`, Graph's header allow-list rejecting a
  POSTed `In-Reply-To` as it rejects `List-Unsubscribe`, and it is the store that derives
  the `conversationId` and the index from them. Each round mints its own `Message-ID`s:
  reused, they would glue the new chain onto the old one.
  `seed_sent_reply` (`make seed-reply`) writes a mailbox-owner turn into Sent Items on the
  same thread, dated now (STU-964): the only way to stage a `superseded` draft under a
  `Mail.ReadWrite` grant, which can send nothing and whose bare folder write would open a
  new conversation. The round is read from the parent's seed header, so `unseed RUN=`
  takes both away, and a parent without that header is refused: a reply the mailbox owner
  never wrote, in a real thread, is not withdrawn. The teardown's outside-Inbox refusal
  now counts Sent Items as a seeding folder: a message where the seeder put it is not a
  review verdict.
- `email_classifier/seed/deliver.py`: the same mailbox, but over SMTP from an external
  account (STU-934). `make deliver`, because a folder write does not cross the transport
  and so invents no `Received` chain, no authentication results, no spam score. Two fields
  survive no delivery: `from_address` (an authenticated relay rewrites a `From` it did not
  emit, and the fixtures' senders have neither SPF nor DKIM) and `date`
  (`receivedDateTime` belongs to the store). `make deliver-compare` quantifies both against
  the seeded round, joined by subject. In exchange, delivery carries the headers a folder
  write cannot set at all (STU-858). Arrivals are categorized so `make unseed` sweeps a
  delivered round like a seeded one: Graph `$filter` on a category, not on a header.
  `make deliver-threads` delivers a conversation from the thread corpus as a chain
  (STU-968). Here the chaining is bare RFC 5322 (`Message-ID`, `In-Reply-To`,
  `References`), not the MAPI properties seeding had to use, Graph's header allow-list
  applying only to a POST. The `Message-ID` is minted per round for the reason it is
  minted in the seeder: reused, it makes the second chain a continuation of the first.
  What the round reports is read from the `thread_id` of the ordinary read-back, never
  from the headers sent: the receiving store is what is under test, and a chain the relay
  stripped looks identical at departure. The headers are necessary and not sufficient,
  measured: sent all at once, the chain lands split, `In-Reply-To` and `References` intact
  and not rewritten by the relay; sent once the parent is visible in the Inbox, it groups,
  with no extra delay. The store resolves the reference against what it already holds and
  does not revisit the grouping when the parent arrives a second later, so a conversation
  goes out turn by turn, each reply waiting for *its* named parent, never for a count,
  which a straggler from another thread of the same round would satisfy.
- `email_classifier/qa_bakeoff.py` + `scripts/qa_bakeoff.py`: judging the judge
  (STU-1014). Measured on a production deployment's run of 2026-08-28, `draft-qa` was
  $10.09 of $29.85, a third of the bill for a pass that *evaluates* a draft instead of
  writing it. What bars a cheaper judge is not the config line (the agent now carries its
  own `model`, not the one from `defaults`) but the proof that it still rejects what the
  other rejects, and that proof requires *the same* drafts: rerunning the pipeline writes
  different ones, and two judges scoring two corpora agree on nothing. A finished run's
  Studio journal already carries them (one `generate-draft` output per attempt, plus the
  fan-out item behind each), so the corpus costs no drafting and *is* the traffic the cost
  was measured on. `make qa-bakeoff JOURNAL=…` replays them through
  `.studio/pipelines/draft-qa-replay.pipeline.yaml`: `email-draft` without its drafter, a
  script stage that re-emits the recorded draft so the reviewer's `previous_stage_output`
  keeps the shape production gives it. The draft arrives by environment variable and not
  in the input file, otherwise the reviewer's `input` would carry a key production does
  not have; the item's `run_id` is replaced, not removed, for the opposite reason: Studio
  journals the input, and a replay that kept the id would add one more log under that id,
  which makes `audit_run.py cost` refuse for ambiguity. **Both** arms run live: a verdict
  recorded for a group's second attempt was rendered on a prompt carrying the feedback the
  first one drew, and a single-stage replay has nothing to rebuild it with. Drafts are
  keyed by thread *and by attempt*: the fan-out interleaves and `stage_complete` carries
  the root run's id, so order does not say which child wrote what; it is the
  `generate-draft` output that names its thread. What the measurement gave is a
  deployment's model choice and is recorded with that deployment.
- `email_classifier/qa_provider.py`: which provider produced a QA measurement (STU-1175).
  None of the five harnesses named it in its artifact, and the price was not
  hypothetical: diagnosing STU-1174 needed the provider of the gate pass of 2026-08-31,
  and the only way to get it was to read the Makefile and reason about what
  `defaults.provider` was that day. A figure that cannot say what produced it cannot be
  verified later. This is the rule `cost.leg` already follows and that STU-1145 settled:
  the provider is recorded **by the run**, never re-derived from the tree an audit sits
  in. It resolves as CLI flag over agent field over `defaults.provider`, so a tree re-read
  weeks later answers for the tree. The resolution itself is not rewritten here: it is
  `provenance.resolved_provider`, called with the agent the harness drives
  (`draft-reviewer` for `qa-gate`, `qa-erosion` and `qa-live-sources`, `coverage-reader`
  for `qa-coverage` and `qa-coverage-gate`). A second statement of the resolution order
  would be a second answer to the same question, which `email_classifier/qa_replay.py`
  and `email_classifier/coverage_read.py` exist to prevent on the verdict side.
  **Resolved, not requested**: what `--provider` passed if it passed anything, otherwise
  what `defaults.provider` resolved to at run time. The three reviewer harnesses pass
  nothing at all, so they take the default, the very value a host or a later commit
  moves; and the coverage pair takes `--provider`, whose empty form *asks for* the
  default, so `logs/qa-coverage.json` used to record a flag that could say nothing. A
  field left empty because nobody passed a flag is the same silence as no field. What the
  tree cannot answer comes out `null` and says so in the summary, the direction of
  STU-912: a measurement nobody could take must not read as a measurement that came back
  empty.
  On the two `eval/` records, the provider joins the body the `digest` covers (STU-1153),
  so a merge cannot stitch one run's provider onto another's `sources`. It is **not** a
  leg of `sources`, and that is the only design decision that matters here: legs are what
  a tree rebuilds, the provider is a property of the run. Making it a leg would turn every
  host difference into a red push no tree carries the repair for. `check` therefore
  reports it and never compares it, and a record stamped before STU-1175 carries none and
  says so (`provider unrecorded`) rather than reading as measured here. Rewriting it by
  hand is exactly the assembled record the digest refuses, so the field only arrives with
  the next pass.
- `email_classifier/qa_gate.py` + `scripts/qa_gate.py`: the drafts the reviewer must
  *keep* rejecting (STU-1078). The verification of STU-1070 was not replayable: the Studio
  journal that carried the draft was on the staging host, the source thread came out of
  `dumps/outlook-delivered.json` (gitignored, on the 30-day shelf), and the item lived in
  a scratchpad passed to a bare `studio run`. The question "does the reviewer still reject
  the forwarding claim" could therefore not be asked on any other checkout, and was about
  to stop being askable on that one. A guard whose regression case exists only in a
  terminal is a guard that can switch itself off silently: exactly the STU-1070 failure,
  one level up.
  The deployment's `eval/draft-qa-rejections.json` therefore freezes the case: per entry,
  the fan-out item, the draft, the expected verdict and the prompt line it exercises.
  `make qa-gate` replays them through `draft-qa-replay` and fails on anything that is not
  `rejected`. The file is committable for the same reason a de-identified corpus is: the
  mail is de-identified, and a draft nobody ever sent names nobody.
  It is **not** a golden, and there is no re-freeze target: an entry asserts that a draft
  is bad, so a run that starts approving one *is* the finding. A target that could
  re-freeze would be the mechanism for erasing the evidence. The corpus grows by the rule
  that made the first entry necessary: a guard's case is added the day that guard is
  written or repaired.
  No `--model`: the reviewer runs on the pin in `.studio/agents/draft-reviewer.agent.yaml`,
  because the question is whether *production* still rejects. One pass per entry, no
  repeats: an isolated `approved` could be a draw, but a guard that re-asks until it likes
  the answer is not a guard, and the finding deserves a human read either way.
  A verdict only counts if the reviewer that gave it is the production reviewer, and that
  cannot be read from the verdict (STU-1139): `make qa-gate` printed "4/4 still rejected"
  and exited 0 on a run where `mail_precedents-search_mail` raised on every call (the
  machine's Graph tenant setting was a placeholder that day, so the tool never reached
  Graph). The reviewer had said so in prose ("both ... tools were unreachable"), and prose
  is not a mechanism: the next run may say nothing. What the record asserts is that
  *production* rejects these drafts, and production has three tools where that run had
  two.
  Where to read it had to be measured, because the issue had the wrong half: the record
  `studio run --json` prints carries no tool calls. `StageRun` only has the `tool_calls` of
  `agent_runs`, an integer, which counts only *successful* calls. The journal carries them:
  a `tool_call_complete` line names the tool and carries `error` when it raised. Not the
  `tool_calls` array of `stage_complete`, which the engine attaches only to a successful
  stage: a tool that raises on every call leaves the contract's tool floor unmet (the
  validator counts only successful calls) and takes the stage down with it, so that array
  is absent in exactly the case being looked for. All calls, not one: a tool that answered
  once was reachable, and a tool nobody called was declined, which the production reviewer
  also does. Over-firing here is what erodes a guard until people ignore it. A journal
  that cannot be found does not read as a tooled reviewer either, in the direction of
  STU-912. Exit **4**, a code of its own by the rule that gave each code its number, and
  read **before** the verdicts: a reviewer deprived of a source answers neither question,
  whichever side it fell on. The deployment's `eval/draft-qa-gate.json` is not stamped;
  `logs/qa-gate.json` is, and names the tool.
  Only the guard drew anything from this, and the three harnesses that publish *numbers*
  threw it away (STU-1147): `qa_bakeoff` read `.verdict` and dropped the other half of the
  `Judgement`, `judge_passes` returned `_replay(...)[0]`, and `draft_bakeoff` drives its
  own pipeline and read no journal. This was not hypothetical: two published measurements
  were taken on a machine where `mail_precedents-search_mail` raised on every call (the
  "zero flips every time" of STU-1104, and the `$0.199` against `$0.120` that moved the
  drafter's pin). Neither said so, and neither could. The first no longer holds, and not
  only for that reason: replayed on a tooled reviewer, the group erodes (STU-1148, below).
  The second has not been replayed. All three therefore name the tool in their summary
  and in their JSON artifact, and `qa_replay.unreachable_in` is the shared reading: written
  a second time in `draft_bakeoff`, it would be a second definition of a tool's health,
  which this module exists to avoid on the verdict side.
  **Annotate, do not fail**, decided and not assumed. What makes the guard exit 4 is the
  *record* it would file: a committed file that other trees reread as a claim. Nothing
  rereads `logs/qa-bakeoff.json`, `logs/qa-erosion.json` or `logs/draft-bakeoff.json`: a
  human reads the number the day it prints, and each one's exit code already answers
  another question (do the arms agree, is the candidate worse). Failing would make them
  unrunnable on the very machine that takes the measurements, which STU-1146 left a
  development machine to be. The word is then the whole mechanism, the STU-912 rule where
  no exit code carries it.
  The exposure is not the same per harness, and the word says so. A bake-off compares two
  arms: the same tool down on both sides mostly cancels out, so the comparison can survive
  what the absolute numbers cannot. Down on one side only, it is two different reviewers
  and there is no comparison at all. That is why it is reported **per arm** and not
  pooled. Erosion asks one reviewer the same question three times, so the missing source
  is in every pass and it is the "no flip" reading that is in question: a conclusion, not
  a comparison. The two readings that find *nothing* therefore carry the tool in their own
  sentence (`finding`), and the other three do not: an unreadable run, a cold approval and
  a repeated flip are findings whatever the sources, and the table names the tool beside
  them anyway. On the `draft_bakeoff` side the reading also covers the *drafter's* tools
  (both arms draft live), and a trace line from before this change rereads as
  `(no run journal)` rather than silence: unrecorded is not answered.
  That it can fail is measured, not assumed (2026-08-29): the reviewer at `b5e324b^` (its
  state the night it approved the STU-1070 draft) approves it again, the one on `main`
  rejects it. Which half of the STU-1070 fix answers is worth writing down, because the
  issue had the wrong half: removing the voice skill from the reviewer's skills leaves the
  rejection in place. What holds is the prose rule the same commit rewrote, which names the
  claim in the past tense. The skill is what the reviewer measures register against; the
  sentence is what rejects that draft.
  What a cold pass proves is narrower than it looks (STU-1101): a draft a production group
  had approved with a promise to follow up is **already rejected** when replayed cold on
  the exact prose that approved it in production, byte for byte, so not deployment drift.
  What approved it was the group: three iterations of reviewer feedback, the first of which
  told the drafter to say the organization would confirm or come back on how to proceed,
  and the third of which defended the result as "correct and grounded as a fallback".
  `draft-qa-replay` is a single cold stage, so erosion that only appears at the n-th
  iteration is invisible to it by construction. That is STU-1104, and it is a hole in the
  guard that STU-1078 exists to keep from going silent. An entry therefore freezes the half
  a replay reaches, never the whole round. An entry that does reach the whole of it is one
  whose draft is **approved** replayed cold on the pre-fix tree and rejected on the fix
  (STU-1106): it demonstrates the production approval, not only a cold judgement.
  A draft can also fail without a single false sentence (STU-1113): every sentence true,
  sourced, no promise and no claimed action, and the question asked never mentioned. The
  frozen draft must not also carry a defect another entry already rejects: freezing that a
  second time gives a guard that cannot fail, green the day the relevance rule disappears.
  The diagnosis there contradicted both halves of the hypothesis. The drafter *addressed*
  the request in the first two passes and stopped because it was told to: the reviewer
  wrote, in pass 2, that one sourced fact was "the whole of what can be said". And the
  reviewer is not blind cold: the same original draft, promise removed, is rejected naming
  the unanswered question. What breaks is elsewhere: it reads "no source covers the
  question" as permission to **drop** it, while its own approval clause only allows it to
  **say so**. With the two confused, it prescribes the omission and then rereads its own
  instruction as sufficient. The fix is therefore on the reviewer alone, in three clauses:
  a request stated as a difficulty is a question ("I can't find where to do it" asks
  where), and the test is that the question asked is addressed somewhere, never that the
  draft raised nothing else (keeping the first would reject good drafts, which the issue
  had seen); a missing source is the reason to name the question and say it is
  unpublished, not to move on; and a named fix never says a fact is all that can be said
  while a request remains unanswered. Measured after the fix: the same group converges in
  two passes on a draft that addresses the request instead of omitting it.
  What this says about `make qa-erosion` is worth writing down: it was the first measured
  flip it cannot see, by construction. It re-serves the *same* draft every pass, while the
  erosion here travels through the rewritten draft, the reservation STU-1104 already named.
  The "no actor" half of that clause fell (STU-1142), and the archive is what made it fall.
  The reviewer had approved "... is not information we can confirm for the moment" (actor
  and horizon, what a deferral is made of), which read as drift toward the sentence
  STU-1101 rejects. The sent mail said the opposite. Measured on a production
  deployment's archive: of the 29 replies that state an answer the organization does not
  have, **22 take the organization as subject** and 7 the fact, and 26 carry a horizon
  ("for the moment", "yet", "currently"). The wording the clause prescribed ("is not yet
  published") appears **once in 831 replies**, in a parenthesis beside a real answer. The
  approved draft was therefore the majority form, and tightening on the rare one would
  have rejected the way that mailbox writes. The form was never the defect: the defect is
  an answer that will come later, that is the STU-1101 test, and that is what the clause
  now names. "We will get back to you on that" still fails, on the answer it promises and
  not on its subject.
  `email_classifier/qa_replay.py` is the one statement of how `draft-qa-replay` is driven
  and where the verdict is read from, shared with `qa_bakeoff`: two callers reading a
  verdict two ways would measure two different things.
  **Who runs it, and when** (STU-1086): nobody by hand, and nothing on a schedule. The
  guard is `claude:local` (an Opus call, and the reviewer's tools read the mailbox), so no
  push can run it. What a push can run is the question of whether it needs re-running,
  in the form of the deployment's observe-spans check (STU-1047) taken as is: a pass writes
  the deployment's `eval/draft-qa-gate.json` with a digest per file the reviewer read,
  `make qa-gate-check` compares them to the tree it sits in (the script exits 3 when a leg
  moved and 2 when nothing is recorded, which `make` flattens to 2), and the fast suite asks
  the question on every push. Editing the reviewer's prose, one of its two skills, the
  `draft-qa` contract, the replay pipeline, one of its three tools or the corpus therefore
  fails the push naming the file, and `make qa-gate` is the only repair.
  That repair needs a machine, and which one is written down before it is needed
  (STU-1146): `mail_precedents-search_mail` is one of the reviewer's three tools, so the
  guard only runs where an app-only grant reaches the mailbox (`OUTLOOK_TENANT_ID` naming
  the tenant and `OUTLOOK_CLIENT_SECRET` set). Elsewhere the guard exits 4 and stamps
  nothing, so a red push stays red there: a machine with only the Anthropic key can ask
  the question and never answer it.
  The record is written only on a pass: it asserts that these sources were verified, and
  a run that found an approved draft verified the opposite. Leaving the stale record in
  place is what keeps the check red until someone actually replays.
  The prompt can move under the record without a file in the deployment changing
  (STU-1249): Studio 0.19.0 stopped injecting a skill file's frontmatter into the system
  prompt (STU-916), so the skills lost their `name:`/`description:` and anything else the
  frontmatter carried. No leg could see it: `sources` fingerprints the *files* the agent
  declares, and none moved. The check stayed green about a reviewer the host no longer
  served, the silent form STU-1078 and STU-1086 exist to prevent. It was the second time
  the prompt moved under the record with no fingerprint answering (the first was the
  `claude-code` provider pouring the repository's CLAUDE.md into the prompt, STU-1174),
  and the first time the cause was a kernel version: STU-1241 is what would record it.
  A pass is not the only way to write this file, and the third is not a run at all
  (STU-1153): it is `git merge`. Two branches that each edit a *different* reviewer source
  each re-stamp on their own tree, and git merges the two `sources` maps cleanly when the
  keys do not overlap. The record then describes a reviewer nobody replayed, every leg
  agrees with the merged tree, and the check is green. The conflicting form is the lucky
  one: it stops a human. Measured three times in one session on STU-1121, and two PRs that
  both re-stamped: the one that merged second had to replay rather than resolve. The
  record therefore carries a `digest` of its own body, written by `as_record` and checked
  by `check` before any leg comparison: exit **6**, a code of its own by the rule that gave
  each code its number, and the repair stays `make qa-gate` and nothing else, which is
  what STU-1078 and STU-1086 rest on. The whole body and not `sources` + `run_id` alone:
  every field comes from the same run, so binding them all is both stronger and shorter
  than a list of the ones that matter, and a merge that stitches one run's `held` onto
  another's `sources` fabricates just as much. A record with no `digest` falls under the
  same code, and that is permanent, not transitional: a merge can remove the key as easily
  as it rewrites the map, and a file that cannot answer for its provenance has none. Keys
  are sorted before hashing: reordering the file asserts nothing. A `.gitattributes`
  marking the file unmergeable was the other candidate: it forces a conflict but does not
  prevent a hand resolution, so it is weaker for the same effort.
  The fingerprinted sources are the reviewer's, not `_PROMPT_SOURCES` (in
  `email_classifier/orchestrate/batch.py`), which is wrong in both directions: it carries
  the drafter, whose replay loads no file (so editing it would bill an Opus call to
  re-answer a question that did not move), and it misses the replay pipeline, the replayed
  draft's contract and the site tool, which the replay runs. What the reviewer declares is
  reread from the agent, not copied: a skill or tool it gains fails in the suite rather
  than as a silent guard later.
  No cron, and why is worth writing down, because it was the obvious candidate. An hourly
  round next to `make check-webhook` spends one call an hour re-asking a question whose
  inputs are all committed files. Nor is it the STU-919 shape, which is what justifies the
  weekly cron of `.github/workflows/tests-ner.yml`: there the weights are re-downloaded,
  here the model is a dated snapshot (`claude-opus-4-8`) and what moves under a pin is the
  draw, which the guard already refuses to re-ask (one pass per entry, no repeats). What no
  fingerprint would see is an entry whose guard depends on a live source (the
  organization's site, the prior mail). Whether an entry does is measured per entry by
  `make qa-live-sources` (STU-1114), and the deployment states which entries were measured
  and names them: a general sentence written when the corpus had one entry would widen
  silently on a green tree as the corpus grows by its rule (STU-1166). Narrating the scope
  rather than filing a record, because a sentence that names its scope asks nothing of
  anyone: a convention is what breaks (STU-1105), and there is none left. What stays
  mechanical is where the reader is when adding the next entry: the `corpus` leg of
  `make qa-gate-check` turns red, and its message names `make qa-live-sources` in addition
  to `make qa-gate`, the only leg that carries that second repair. A frozen record was the
  other candidate, and STU-1114 already declined it for its own output: freezing "nothing
  diverged" under fingerprints reads like a reviewer someone replayed. It is an entry that
  diverges that would reopen the question, not the calendar.
- `email_classifier/qa_erosion.py` + `scripts/qa_erosion.py`: does the reviewer still hold
  in the group's late passes (STU-1104). `make qa-gate` replays each frozen draft in a
  single cold stage, so it only answers for pass 1. STU-1101 is the case that made the
  rest of the group interesting: a draft the reviewer rejects cold was approved in
  production after two rejections of its own, and the critique it had written earlier came
  back as the reason to keep the sentence.
  `.studio/pipelines/draft-qa-erosion.pipeline.yaml` puts the echo stage of
  `draft-qa-replay` inside the `email-draft` group: the same draft is re-emitted every
  iteration, so the only thing that moves between passes is the reviewer's critique coming
  back to it as `group_feedback`, and a verdict that flips has only one thing to attribute
  it to. `max_iterations: 3` is reread from the pipeline, not copied, and a test compares
  the two groups field by field: a K axis production does not have would measure something
  else.
  Two premises of the issue were corrected before measuring, both reread in a production
  run's journal. The approval fell at pass **3**, not 4: that thread carries exactly 3
  `write-draft` outputs, so 3 `draft-qa` passes, and the four-row table came from a journal
  where 14 conversations interleave under the root run id (which is what `qa_bakeoff`
  already states by keying per thread rather than per position). And `group_feedback` is
  **replaced** on every iteration (`group-orchestrator.ts` in the Studio engine), never
  accumulated: pass K carries the critique of K-1 and a counter, not K turns of prose. So K
  runs from 0 to 2, and what is exercised is a reviewer re-served its own last words, the
  exact STU-1101 shape.
  Measured six times, and the answer changed. The first three saw **no flip** (2026-08-29
  on the 2 cases of the time, twice, 12 reviewer passes per measurement; 2026-08-30 on 3
  cases, once, **18 passes**). Those re-measurements were not caution: the number is a
  claim about *one* reviewer, so every parent merge that moves what the reviewer reads
  expires it. One followed a commit that moved `email_classifier/anonymize.py`, in no fingerprint of
  `qa_gate.SOURCES` and rightly so for the prose, but `mail_precedents` runs live during
  the replay and passes its hits through redaction. Another followed a commit that moved
  the reviewer's prose and its two skills, exactly what the measurement exercises.
  The next three came together on 2026-09-01 (STU-1148), after the two merges the expiry
  rule names (STU-1136, the language rule, and STU-1142, the unanswered-request clause
  rewritten in three places), on 4 cases. **The group erodes.** Three passes of the same
  protocol, 24 replays, 67 reviewer passes: **5 flips, all at K=1**, none at K=2. Two
  passes at one commit and the third after the STU-1147 merge, so that tool health was
  stated by the harness and not computed by hand: `unverified: 0`, no unreachable tool on
  any case. The three read together because nothing the reviewer reads moved between them,
  verified and not assumed: the diff is empty on the reviewer agent, the two skills, the
  `draft-qa` contract, the two pipelines and the corpus. What moved is the harness, which
  adds a line and touches no verdict. **No case was spared**: a case that held 4 times in
  4 in the first two passes flipped in the third, so "that case resists" was the number of
  replays and not a property of the case.
  The control is inside the measurement, and it is what makes the flips attributable: at
  K=0 the verdict is `rejected` **24 times out of 24**, the served draft is byte-identical
  from one pass to the next, and the reviewer is the same. Nothing but its own critique
  moved. The STU-1014 noise floor does not need invoking: the cold pass *is* the witness,
  and it did not contradict itself once.
  Those three passes were taken under Studio 0.17.0, where `claude-code` poured the
  repository's CLAUDE.md into the reviewer's prompt, a file that described erosion, its
  two forms and its cases by name (STU-1174). Replayed under 0.17.1 (two passes of the same
  protocol): **3 flips in 16 replays**, and the cold pass `rejected` **16 times out of 16**,
  `unverified: 0` on both. 3/16 against 5/24 is the same order: **the group still erodes,
  without the cheat sheet**. What does not hold is the sub-claim "all at K=1": one of the
  three fell at **K=2**. Which cases flip changed again, which says on another tree what
  the next paragraph says: it is the number of replays and not a property of the case, and
  the verdict is read on the aggregate.
  The per-case line said "inside the noise floor" on each of the four, which was right per
  case and wrong in total: one flip in two repeats is indistinguishable from a draw, five
  in twenty-four against twenty-four agreeing cold passes is not. The verdict is read on
  the aggregate, never on the lines, and a single pass of two repeats is not enough to
  read it, which those three passes show by contradicting each other case by case while
  agreeing on the total.
  That verdict is now printed and not copied by hand (STU-1161): the reading only reached
  the docs because a human overrode four lines that all said the opposite, which is not a
  mechanism. `qa_erosion.RunReading` is the only thing that sees another cell:
  `CaseResult.finding` never does, and its `EROSION` requires a case to clear *its own*
  bar, so the shape that occurred (one flip per case spread across cases and passes)
  cannot reach it. Its line goes under the header and above the cases: the failure
  repaired is a reader who stops at the table, and the conclusion is what they see first.
  The per-case lines stay (they are how to find which case to go read), and the line for
  an isolated flip now points at the run line instead of concluding in its place.
  The bar is the run's cold pass, not a count or a rate: a threshold nobody measured is the
  very objection that left the cache-ratio leg of `check` at zero. Two facts carry it, and
  neither is a threshold. A flip is **paired**: each replay carries its own K=0, so a flip
  is a replay where the reviewer contradicted itself on the byte-identical draft with only
  its own critique in between; it is not a replay compared with the twenty-three others.
  And the agreement of the cold pass from one replay to the next is what rules out a
  reviewer flipping a coin on that draft: a coin would have contradicted itself at K=0 as
  well. So a single flip carries the same verdict, and the line prints both numerators
  because they do not weigh the same. A `NO CONTROL` is the case where the licence falls:
  a cold approval anywhere in the run makes a flip indistinguishable from a gate that never
  held, and `make qa-gate` is what gets repaired first. A replay with no verdict leaves the
  denominator and is named, by the `qa_coverage` rule: a thin denominator must not read as
  a whole one.
  Two forms, which are not repaired the same way, and the five flips split 4 to 1. The
  first is the **phantom rewrite**: the reviewer writes the corrected draft inside its own
  critique, then approves that ("The corrected draft reads: ...", "The original
  replay-draft was defective and could not be approved as written ... The corrected draft
  above resolves every issue"), while nothing was rewritten and the served draft still
  carries the claimed action. In the most readable example, pass 1 writes "Remove the
  request for the date of birth and answer the registration question from ...", and pass 2
  recites that prescription as if it *were* the body, approving a draft that still asks
  for the date of birth. That is the subject of STU-1140. The second has no phantom rewrite
  and happened once: the reviewer searches precedents live, reads that the organization
  handles withdrawals on notice, and reasons itself into "there is genuinely nothing for
  her to do online", talking itself out of the rule STU-1113 had just installed, one pass
  after applying it. That is judgement erosion proper.
  The 2026-08-30 zero has a second reason not to hold, checkable on its journals: **a
  single tool call in its 15 replays**, and it raised on the placeholder tenant of
  STU-1146. The 2026-09-01 passes make 56 in the first two, all answered, and the third
  says so itself. That is not the whole explanation (the reviewer declines to search in 5
  of the 16 replays of the first two), but the one time the 2026-08-30 reviewer wanted to
  verify, it was refused. That is the STU-1147 doubt, confirmed on an artifact.
  `make qa-erosion` still has no record in the manner of the gate's
  `eval/draft-qa-gate.json`, and does not want one, in both directions now: freezing a
  table under fingerprints would pass off as verified a reviewer nobody replayed, whether
  it says "nothing happened" or "three flips".
  Two repeats and not one, because the STU-1014 noise floor (21 first attempts of 22) makes
  an isolated flip unreadable per case; that is what the harness line says of itself, and
  it is also why a measurement is read whole.
  What the measurement cannot see is named rather than discovered later: production
  rewrites the draft between passes and this does not, so the reviewer receives something
  that visibly ignored its last instruction, which pushes toward rejection. A flip is
  therefore strong evidence of erosion, which is what happened; a hold is weaker evidence
  of the opposite than it looks. Making the draft move is the other form, and that is
  `draft-bakeoff`: there a flip can no longer be attributed to the feedback rather than to
  a better draft.
  **STU-1160 closed the channel, and with it the question this harness asked.** The four
  phantom-rewrite flips were not an artifact: the reviewer, served its own critique in
  `group_feedback`, read it as a description of the draft. `email-draft` therefore no
  longer gives it that, and this pipeline follows (the parity test holds the two groups
  together, and a K axis production does not have would measure something else). The prose
  was the cheaper candidate and it is the one this reviewer had already been seen to get
  around (STU-1113), so it is the channel that goes and not the instruction. Nothing in the
  loop needed it: the feedback to the drafter comes out of `draft-qa`'s `post_validation`
  and not from what the reviewer reads, so the group still restarts and still carries the
  critique. What is removed is only the reviewer's memory of its own previous pass, which
  is exactly what it was taking as evidence.
  The honest consequence is that **it no longer measures erosion**: nothing moves between
  passes any more. What it measures is the noise floor: same reviewer, same body, three
  times. That is the witness STU-1148 already relied on (24/24 `rejected` at K=0), promoted
  to the whole reading. Measured on 2026-09-01 under `claude-code`, 4 cases x 2 repeats:
  **8 replays of 8 held, 0 flips**, 24 agreeing reviewer passes, `unverified: 0`. Against 5
  flips in 24 replays before.
  What that number cannot do is named rather than discovered later: with the channel
  closed, a flip has become structurally impossible except through the model's draw, so
  the verification is close to tautological and it is the fix that carries the claim, not
  the measurement. And it is **one** pass of the two-repeat protocol, where STU-1148 made
  three, its own lesson being that a single pass contradicts itself case by case while
  agreeing in total. A second pass is one command away if doubt returns. Later passes
  (under Studio 0.19.0, and after a skill edit the expiry rule names) returned the same 8/8
  and 0 flips, which is what a noise floor under a closed channel returns and not a further
  confirmation of the fix.
  It is not a gate and exits 0 whatever it finds: a flip is a finding a human reads, and
  the mechanism (a rule the feedback cannot carry away, an iteration ceiling, judging the
  final draft cold) would be decided on the number. No `--model` either, for the
  `email_classifier/qa_gate.py` reason: the question is whether the *production* reviewer erodes.
- `email_classifier/qa_coverage.py` + `scripts/qa_coverage.py`: does the body the group
  delivered answer what the sender asked (STU-1140). It is the gap between the two
  neighbouring measurements, and STU-1113 is the first measured failure to slip through
  it: `make qa-erosion` freezes the draft, so what carried the failure (the rewrite) is
  exactly what it removes; `make draft-bakeoff` lets the draft move but moves the model
  with it, so a flip has two things it can be attributed to. On a production run of
  2026-08-30 the reviewer struck an unsourced sentence while adding that the free-of-charge
  fact was *"the whole of what can be said"*, the drafter then wrote only that fact, and
  the reviewer approved its own instruction coming back. The question actually asked
  (where to cancel online) was nowhere in the delivered body. Each verdict read alone is
  defensible; the sequence is the defect.
  But the sequence is not what makes it a defect, and that is the trap the issue names
  itself: a draft that shrinks toward the feedback is what the loop is *for*, and a group
  that converges by cutting an invented sentence has done its job. So the measurement does
  not fire on the shape of the sequence. It asks the one question that separates the two,
  and asks it of the **source message**, not of the loop: is everything the sender asked
  addressed somewhere in the delivered body?
  The reader that answers has never seen the draft's history, and it is **not**
  `draft-reviewer`: that agent approved this body, so asking it would ask the suspected
  party. No skills and no tools either: the org-facts skill would let it reason about what
  the organization *could* have said, and the site tool would let it go and look, and both
  are sourcing, which is the reviewer's job and not this question. What counts as
  addressed is stated in the prompt, and that is the half that decides: an "the
  information is not published" **addresses** the request. It is the right answer when no
  source carries one, and counting it as unaddressed would turn every honest reply in the
  mailbox red.
  `requests` and `verdict` say the same thing twice, on purpose: they cross-check, and a
  reader that lists an unaddressed request while calling the reply `covered` records
  `incoherent` instead of having one half chosen for it. An output the model did not think
  through is not a judgement. Two ways of not being a measurement, kept apart by the
  STU-912 rule: `error` is a call that returned nothing readable, `incoherent` is a reader
  that answered and contradicted itself, and the repairs differ. Both stay out of
  `flagged` (a read that did not land is not a read that cleared the body), and the
  incoherent output is printed whole on stderr, since what it said is all a human can act
  on.
  A third shape was neither, and it is the one that cost the measurement: `requests` comes
  back sometimes as a real array and sometimes as an array **serialized into a string**,
  roughly one time in two on the same prompt (measured 2026-08-30). Read as a `list` only,
  a perfectly coherent verdict went to `error`, so a sweep measured half of what it ran
  while reporting the rest as dead calls. The contract cannot close this, since
  `required_fields` checks presence and not shape; the string is therefore decoded once,
  where the output is already being checked, and a string that is not a JSON array stays
  an `error`.
  The mechanical reading of the sequence stays, printed *beside* the verdict and never as
  it: `converged_by_removal` (the approved body shorter than the pass before it, and the
  shortest of the run) says it was the loop that dropped the answer, which makes a flagged
  case this issue's case rather than an ordinary drafting miss. The table also prints,
  under the case, the instruction the reviewer gave just before approving, because that is
  what a human must read to decide the mechanism.
  **A group is a child run of the fan-out** (STU-1158), and the sequence of its passes is
  the one `email_classifier/journal_sequence.py` reconstructs: one statement of how a
  journal is read, not two. What this replaced was a refusal on "two fan-out items open",
  and `.studio/pipelines/draft-items.pipeline.yaml` pins drafting concurrency at **2**:
  *every* production drafting journal hit it, and the route the refusal named was
  `make draft-bakeoff`, which replays a run one conversation per `studio run`. That is a
  way to read *a* run, never to measure the one that delivered bodies to a mailbox.
  `Unattributable` keeps its name and its reason, and takes the condition that remains:
  an execution `journal_sequence` cannot place. It is missing from some group's passes and
  the journal does not say which (exactly the fact that makes it unplaceable), so there is
  no row on which to name the gap, and the refusal is the journal's. A group missing a
  pass can report the wrong delivered body, which is a false clearance, the one direction
  this tool must not fail in (STU-912). Measured 2026-09-01: zero unplaceable executions
  across 102 drafting journals on a development checkout, and pass counts agree with
  `scripts/audit_run.py` (`sequence`) on all 102.
  An attempt whose drafting stage **failed** is not a pass: it ends with no output, so it
  wrote no body and nothing the next pass could be shorter than. Four `mailbox-full`
  journals in that tree are only that, and they carry no group rather than a group of
  empty bodies.
  `qa_bakeoff.drafts_from_journal` is left as is: it keys on `write-draft`, whose output
  names its own thread, so it needs no attribution and folding it in here would not come
  out simpler.
  A `draft-qa-replay` or `draft-qa-erosion` journal is not an input either, and raises
  `EchoJournal` by name: its drafting stage re-emits a recorded draft, so the body at the
  end is one no group wrote toward. Let through, it would read as a run that simply drafted
  nothing, which is something else, and the thing a caller would act on. Same direction
  for a drafted thread whose item `drafts-pre` does not carry: `OrphanGroup`, as
  `qa_bakeoff` raises `OrphanDraft`, because borrowing the run's input hands the reader an
  empty `body_text`, and a message that asked nothing comes back `covered`: a false
  clearance, the one direction this tool must not fail in.
  `--repeats 2` by default, for the reason of `email_classifier/qa_erosion.py`: the reader
  is a model, and a single flag cannot be told from a draw, so it prints "a single read,
  not yet a finding". That count is taken over the reads that **landed**, never over those
  requested: a dead call beside three agreeing flags is no evidence against them, and
  making the group unreadable there would hide the finding behind the weather (that was
  this tool's bug, one level above the one it hunts). Only a group where nothing landed is
  `UNREADABLE`, and what was lost is named on every row rather than dropped, so a thin
  denominator never reads as a whole one. That last sentence also holds for the lone flag,
  and that is what the guard missed (STU-1188): it held on `flags == 1 and readable > 1`,
  so it could not fire when only one read had landed, and an `(error, incomplete)`
  published the "UNANSWERED, flagged in 1 of 1 read(s)" finding that two agreeing flags deserve, while
  the same `incomplete` beside a `covered` printed "not yet a finding". Same evidence,
  opposite verdicts, and what decided was whether the other call had died, that is, the
  weather and not the body. Judging on what landed is right with several reads and inverts
  with one: at a denominator of one, taking the survivor at its word *is* reading a thin
  denominator as a whole one. So the rule holds on the flag count alone, and the row says
  which of the two shapes it saw: "one flag in N reads" when a read disagreed, "the only
  read that landed flagged" when there was no other. The price is named: a group whose
  other read always dies never reaches a finding, at any `--repeats`, and a one-read run
  produces none. Nothing is hidden for all that: the flag, what it named and every read
  stay on the row, in the table and in the record; what drops is the corroboration no
  second read gave. It is not a gate and exits 0 whatever it finds.
  `--provider anthropic` by default, like `scripts/draft_bakeoff.py` and for its reasons:
  the CLI builds its system prompt from the cwd, so before Studio 0.17.1 every read carried
  the checkout's `CLAUDE.md` into the prompt and the bill (STU-1013, STU-1115); and every
  read being a one-shot process, it is exactly the shape STU-1133 measured at a cache ratio
  of 0.0, for which the agent's `prompt_cache: "on"` would return nothing. The target's
  `PROVIDER=` overrides it, and a measurement taken under another provider says so rather
  than letting the default be assumed.
- `email_classifier/qa_coverage_gate.py` + `scripts/qa_coverage_gate.py`: the reads the
  coverage reader must *keep* returning (STU-1151). STU-1140's whole evidence is a pair,
  and both halves were Studio journals under `.studio/runs/`: gitignored, pruned at 30
  days, and the one shelf no protection reaches. Around 2026-09-29 the evidence would leave
  the only machine that had it, and "does the read still separate these two runs" would
  stop being askable on any checkout. That is the STU-1078 failure one harness over, so it
  gets STU-1078's answer one harness over: the deployment's `eval/draft-coverage-cases.json`
  freezes, per entry, the sender's message, the passes the group made, the expected verdict
  and the rule the entry tests, and `make qa-coverage-gate` replays them and fails on
  anything else. Committable for the reason `eval/draft-qa-rejections.json` is.
  Not a golden and no re-freeze target, by the STU-1078 rule: an entry asserts what the
  read must say, so a run that says otherwise *is* the finding. `expected_verdict` accepts
  **both** verdicts, where `qa_gate`'s can only say `rejected`: a corpus of cases that flag
  passes trivially under a reader that flags everything, and this reader becomes that one
  rule away. The control entry is the only thing that asks that question.
  Every read must agree, and a non-read is a failure, not a skip: a read that came back on
  the other side has moved whichever side the majority fell on, and a guard that could not
  be asked has not fired (STU-912). That is what separates the two `--repeats 2`:
  `qa_coverage` takes two to tell a flag from a draw, here a lone disagreement *is* the
  finding.
  No `--model`, for the reason of `email_classifier/qa_gate.py`: the question is whether
  the *production* reader still says this.
  The coverage call now lives in `email_classifier/coverage_read.py`, for the reason that
  took `email_classifier/qa_replay.py` out of the bake-off: two callers that read a verdict
  two ways would measure two different things.
  That the guard can fail is measured, and the measurement corrected which edit the entry
  catches. The check the issue asked for (deleting from the prompt the bullet saying an
  "information is not published" addresses the request) leaves the control at `covered`
  2/2: the bullet illustrates a rule stated above it ("addressed when the reply says
  something about it", "unaddressed when the reply never mentions it"), and that body
  mentions the request outright, so the general rule carries it alone. It is the
  **negation** of the bullet that flips the control to `incomplete` 2/2 and exits 1. So the
  entry does guard the rule the issue named, and deleting a sentence is not how that rule
  leaves the prompt: the exact shape STU-1078 measured on the reviewer, where removing a
  skill left the rejection standing and it was the prose that held it.
  **Who runs it, and when** (STU-1156): nobody by hand, and nothing on a schedule, the
  STU-1086 shape taken over unchanged one harness on. The guard is `claude:local`, so no
  push can run it; what a push can run is the question of whether it needs re-running. A
  pass writes the deployment's `eval/draft-coverage-gate.json` with a digest per file the
  reader read, `make qa-coverage-gate-check` compares them to the tree it is in (3 when a
  leg moved, 2 when nothing is recorded, 5 when the script and the package come from two
  trees, STU-1109), and the fast suite asks the question on every push. Editing the
  reader's prose, the `draft-coverage` contract
  (`.studio/contracts/draft-coverage.contract.yaml`), the pipeline,
  `.studio/invariants.md` or the corpus therefore fails the push naming the file, and
  `make qa-coverage-gate` is the only repair.
  The list is short because the reader declares no skill and no tool, deliberately
  (STU-1140: sourcing is `draft-reviewer`'s job), so there is nothing to track on the skill
  or tool-YAML side. It is read off the agent, not copied: a skill or tool the reader
  gained fails in the suite rather than as a silent guard later. And it is not
  `qa_gate.SOURCES`: the coverage replay loads neither the reviewer nor its skills, so
  putting them there would bill a live read to re-answer a question that has not moved.
  The record is written only on a pass, by the STU-1078 rule: it asserts these sources were
  verified, and a run that found a frozen read moved verified the opposite. Leaving the
  stale record in place is what keeps the check red until someone actually replays. A file
  of its own and not a block in the corpus: that matches `qa_gate`, and keeps the corpus a
  thing nothing rewrites. A digest per file, keyed by path, and not one digest over the
  set: an entry added to the corpus and a prompt edit both say "replay it", but only the
  second says the reader's judgement is what changed.
  The record also carries the digest of its own body, and **6** is the code that refuses
  it: the STU-1153 finding reaches this file unchanged, the day two branches each edit a
  different source of the reader and each re-stamp on its own tree. Git then merges the two
  `sources` maps cleanly, every leg of the result matches the merged tree, and the check
  agrees about a reader no run replayed. The shape that conflicts is the lucky one: it
  stops a human. The whole body and not `sources` alone, and a record with no digest falls
  to the same verdict, since a merge can drop the key as easily as it rewrites the map.
  A pass of the gate demonstrated on 2026-09-01: the entries hold 2/2, the pass stamps the
  record, and the check exits 0 afterwards; a line added to the coverage-reader agent YAML
  (`reference-deployment/.studio/agents/coverage-reader.agent.yaml` in the reference
  deployment) makes it exit 3 naming that file and turns the fast suite red on the same
  edit, and a `sources` entry edited by hand makes it exit 6.
  The prompt can also move with no file in the repository changing: Studio 0.19.0 stopped
  pouring a skill's frontmatter into the prompt (STU-916), which the `sources` leg could
  not see since it fingerprints files (the `qa_gate` argument, and the same on this
  harness). Such a kernel change is a reason to re-run the gate, not something its check
  can detect.
  The target has to be told its provider on a machine where the default does not run: under
  an uninitializable `anthropic` provider it printed `0/4 still read the way they were
  frozen` with a `[READS NO LONGER]` per case, the verdict's words for a pass whose eight
  reads were all `error`. It refused to stamp, which is what kept the record honest, and
  that is STU-1254.
- `email_classifier/qa_live_sources.py` + `scripts/qa_live_sources.py`: does a frozen entry
  hold on its prose or on a live source (STU-1114). `qa_gate.SOURCES` fingerprints the
  reviewer's declarations, and `make qa-gate-check` publishes that answer as if it were
  "do the frozen rejections still hold". They are not the same question: during a replay
  the reviewer really calls its tools, so the organization's live pages and the mailbox's
  sent mail are inputs to the text a verdict is given on, and no leg covers them. STU-1104
  had to re-measure after a merge that moved `email_classifier/anonymize.py` for want of a
  record able to say whether the reviewer still read the same thing.
  `make qa-live-sources` therefore replays each entry twice: tools live, then
  `org_web-search_site` and `mail_precedents-search_mail` returning nothing. **Returning
  nothing is not being unreachable**, and the whole rig rests on keeping them apart: the
  silent arm answers *through the real search function* with no source behind it
  (`matches: []`, the answer each tool defines for "the words are not there"), exits 0 and
  is recorded as reachable, so the STU-1139 leg keeps landing on a tool that really raised.
  The silence goes through a shim put in place of `$PYTHON` (`scripts/qa_silent_tool.py`),
  which every command under `.studio/tools/` launches: the agent, contracts, pipeline and
  tool YAMLs in question are the ones production runs, and the arm differs only in what
  came back.
  Two tools, not three. `org_memory-read_memory` has no empty answer (a name absent from
  disk raises, which is the unreachable case STU-1139 already poses), and a corpus whose
  entries carry an empty `memory_index` does not exercise it: the reviewer's own rule
  ("when none does, ignore them") means it is never called.
  What the table showed that was not planned: **the reviewer decides by itself whether to
  search**, and does not decide the same way twice. One entry called no tool in its live arm
  in one run and none in its silent arm in the next; another did the reverse. Neither run
  alone therefore exercised the shim on every entry, and two runs together did, hence the
  second run. That is not a caveat on the reading: a reviewer that declines to search had
  no live source either, so both routes say the same thing. What they do not say alike is
  what the run *tested*, and the table names which of the two each entry took rather than
  leaving the reader to assume it.
  Two runs and not one, also for the reason that made STU-1104 re-measure three times: a
  figure is a claim about a tree, and a rebase can bring commits that touch
  `email_classifier/qa_gate.py`. Whether any of them moves the reviewer, its skills, its
  contracts, the replay pipeline, its tools or `email_classifier/anonymize.py` is checked
  by replaying, not by reading the list.
  What it does not license: adding `email_classifier/anonymize.py`, the site or past mail to
  `SOURCES`. One more pin would bill a live Opus call on every change, most of which cannot
  touch a verdict, and the measurement said none does today. What reopens the question is
  an entry that diverges, and this command is what would name it, plus the source, the live
  arm naming the tools that answered it.
  A measurement, not a gate: it exits 0 whatever it finds, like `make qa-erosion` and
  unlike `make qa-gate`. What makes the gate exit non-zero is the record it would otherwise
  file, which other trees read back; nothing reads `logs/qa-live-sources.json` as a claim.
  No `--model` either: the question is whether *the production reviewer* holds on a live
  source.
  Under Studio 0.17.0 the `claude-code` CLI poured the checkout's `CLAUDE.md` and
  `~/.claude` into the prompt; from 0.17.1 it no longer does (STU-1115). Both arms carried
  that prefix equally, so the *difference* they publish was unaffected, which is why a
  measurement taken before 0.17.1 was recorded with a caveat rather than postponed.
  `studio_version` on a QA measurement is now what says which kernel it ran under
  (STU-1241).
- `email_classifier/draft_bakeoff.py` + `scripts/draft_bakeoff.py`: the same question
  again, asked of the drafter (STU-1073). It was the last leg no measurement had settled,
  and the largest: 64% of the drafting bill on the three post-STU-1051 rounds measured on
  a production deployment's mailbox on 2026-08-29, against 36% for the judge. The
  drafter agent did not even carry a `model:` and inherited `defaults.model`; it now
  carries the literal-free `${DRAFT_MODEL}` the other two agents already carry.
  Neither a golden nor a recorded corpus answers here. "Is this draft good" is not
  mechanical, so this cannot copy `classify-bakeoff`; but the judge is already in the
  pipeline, and STU-1014 measured its own noise floor: 21 first attempts out of 22 when it
  is asked to judge the same drafts twice. That floor is what makes a gap in rejection
  rate readable. Both arms draft live on the same conversations (the recorded fan-out of
  a real drafting journal, the corpus source of `make qa-bakeoff`) and are judged by the
  same Opus reviewer.
  `.studio/pipelines/draft-bakeoff.pipeline.yaml` mirrors `draft-qa-replay` one stage
  earlier: `email-draft` without its mailbox write. Everything that decides a verdict
  stays production's (same agents, same contracts, same group, same `max_iterations`),
  because the figure is the cost per *accepted* draft, so a rejection must cost the
  iteration it costs in production. `seal-draft` (`scripts/seal_draft.py`) takes the
  place of `write-draft` and passes on the `draft_id` a write would have returned, so the
  reviewer's `previous_stage_output` keeps the shape production gives it; it writes
  nothing, and a pass drafts each conversation once per arm and per repeat.
  The deciding figure is cost per accepted draft, never cost per call. STU-1014's finding
  inverts here: a rejection is a whole group iteration, with both a `generate-draft` and a
  `draft-qa` paid again. A drafter 60% cheaper per call and rejected twice as often costs
  more, and a per-call table would have announced it as a bargain.
  Two axes, both scored only where the arm agreed with itself from one repeat to the next.
  `accepted` (the reviewer approved within the group's iterations) is the headline, since
  a conversation no draft satisfies is a sender reading a bad answer; `accepted_first` is
  the cost axis, and the one STU-1014's floor was measured on. `candidate_is_worse` counts
  accepted drafts over the shared denominator instead of comparing two rates, for
  `classify_bakeoff`'s reason: a rate over what each arm stabilized lets a model that
  wavered on everything but two conversations show 100%. A baseline that stabilized
  nothing prints `NO MEASUREMENT` rather than a verdict, and an arm that got nothing
  accepted has no cost per accepted draft: a real bill over a denominator the arm never
  had is an invented figure, the same reason a model without a price raises.
  `--provider anthropic` is the default: `claude-code` builds its system prompt from the
  cwd, so each arm would carry the repository's CLAUDE.md into the
  prompt and into the bill (STU-1013), and a bake-off that decides a production pin would
  be measuring the harness.
  Two harness defects only a real pass could expose were fixed by the 2026-09-01 rerun
  (STU-1162). The loop was arm-major, so an interrupted pass had drafted one whole arm and
  nothing of the other, which is no measurement however far it got; it now interleaves.
  And the trace it writes is read back, so a run already paid for is reused instead of
  redone. Only rows that name the same journal are reused: a drafting item is the whole
  prompt, so a row from another corpus answers another question. `--fresh` redoes
  everything, which is what to ask for when the agents have changed.
  Reading the tool-health annotation correctly: on that rerun the harness reported
  `org_memory-read_memory` unreachable in one arm only, and its stock sentence concludes
  two different pipelines. That was not the case. The deployment's memory index was
  empty, so *any* name requested raises, for both arms. What the line actually measured
  is that one model obeys an empty index and the other calls the tool anyway; the
  difference is in that model's context (tool errors the other never saw), which is model
  behaviour, what the bake-off exists to measure, not a broken setup.
  STU-1237 gives the pin a tie-break with room under it. `accepted` had come back at the
  ceiling for both arms twice running (44/44 and 44/44, then 22/22 and 22/22), so the
  pin was decided by `accepted_first` alone both times, and the second time
  `accepted_first` did not separate the models either (11/17 against 12/20, noise floors
  0.77 and 0.91: not a real difference between two booleans that both mostly land the
  same way). `iterations` is what decides now, when `accepted` has already tied: a
  continuous number where the two booleans are not, and it cost nothing new to add.
  `Authored.attempts` already recorded the group iterations a conversation took, and
  `Spend.attempts` already summed it per arm. `iterations_to_accept` scores it the way
  `accepted` is scored: per conversation, only where the arm's accept/reject verdict
  agreed across every repeat, and only over the conversations it stably accepted. A
  conversation it never settled is already penalized by `accepted`, and folding it into
  the mean too would double that penalty instead of measuring cost.
  Rescored on the same STU-1162 trace (88 live drafts) without paying for a fresh round
  (every one of the 88 rows was already in the run's `logs/` trace, so the demonstration
  is a rescore, not a re-authoring), it separates the arms exactly where both booleans
  tied: 1.39 against 1.41 iterations to acceptance over 22/22 stably accepted
  conversations each. `accepted` still decides `candidate_is_worse` alone and unchanged
  (a drafter cannot buy its way out of a lost conversation with fewer iterations on the
  ones it keeps), and `iterations` is read only once `accepted` has already tied, which
  is the case every bake-off run so far has actually produced.
- `email_classifier/classify_bakeoff.py` + `scripts/classify_bakeoff.py`: STU-1014's
  question, asked of the classifier (STU-1045). Nothing in the repository could answer
  "does this model classify well enough". `eval run` replays stage 1 and never calls the
  LLM, so it prints the same counts under any model, and the deployment's stage-1 golden
  (`reference-deployment/eval/golden.json`) holds `bucket: null, action: null` for every
  fixture that escalates: a golden seeded from a stage-1 replay freezes an
  `escalate_to_llm`, never a stage-2 verdict. The only evidence that had ever settled a
  classification model was two live rounds on three messages (STU-1044), and going back
  to the previous model was the cautious answer, not the measured one.
  `make classify-bakeoff` replays the escalating fixtures under two models and scores
  each against the deployment's stage-2 golden
  (`reference-deployment/eval/stage2-golden.json`). Unlike the drafting bake-off it needs
  no recorded traffic: a fixture is a whole input, so both arms start straight from the
  deployment's fixtures. The lever is `${CLASSIFY_MODEL}`, which the classifier agent
  interpolates with no literal of its own: unset, the engine resolves it to an empty
  string and falls back to `defaults.model`. Production reads the pin from
  `.studio/config.yaml`, and a model a run picks up from the environment is a model no
  report can name.
  Two axes. `verdict` (bucket, suggested_action) is the headline figure; `needs_reply` is
  counted apart, because it is the axis on which a miss costs a sender their reply, and
  the one STU-1044's regression fell on. Since STU-1048 it fails both ways: the corpus
  carries bucket-A messages that owe no reply, so a model that answers `needs_reply` to
  everything now scores below a correct model, where it used to tie. It stays lopsided
  (on the production corpus, 25 of 28 fixtures are someone asking for something, which
  *is* the mailbox), so a gap of one message is a gap of one message, not a rate.
  A model is scored only where it agreed with *itself* across repeats, and the share it
  stabilized is its noise floor: the LLM is not deterministic, so a single round cannot
  tell a wrong verdict from a coin toss. Under a single repeat the floor prints `null`
  rather than 100%, which would only be the artefact of having asked once.
  Three refusals, each because the other branch publishes a figure nobody measured. Golden
  coverage is checked **before the first call** and in both directions: a fixture that
  stage 1 now decides, or that the golden does not cover, is a wrong denominator, and both
  arms would pay one stage-2 pass per conversation to find out afterwards. An arm that
  stabilized nothing prints `null`, not 0%. And a pass whose *baseline* stabilized nothing
  prints `NO MEASUREMENT`, since every arm failing reads through "the candidate is worse"
  exactly like a bad candidate.
  Cleartext on both sides: the corpus is synthetic and committed, whether tokenizing
  changes a verdict is `make eval-ab`'s question, and the alternative puts a 1.76 GB
  detector between two models being compared with each other.
  The harness's acceptance case is the regression STU-1044 found: on a production
  deployment's corpus (STU-1049, 2026-08-28, 26 conversations, 2 repeats per arm) one
  message came back `needs_reply` under `claude-opus-4-8` and `keep_visible` under
  `claude-haiku-4-5`, stable across repeats in both arms. The floor is read before the
  gap: it is because the baseline stabilized all 26 that five verdicts were a gap and not
  a draw. That both axes printed the same figure there was a property of that day's
  corpus, not of the models.
- `email_classifier/draft_delta.py`: how far the reviewer moved a draft before it was sent
  (STU-1100), and since STU-1130 the three figures the voice rules of STU-1124 were meant
  to move. Similarity was not enough: every reply carries the same greeting and the same
  closing, so a length read on the whole body measures the furniture. `answer_part` cuts
  both (the span the org-voice skill measures), `semicolons` counts the mark the archive
  never writes, and the `voice` block of `audit_run.py reviewed` sets a round's
  median, share under 200 characters and semicolons beside the archive's and a reference
  round's. Both baselines are **re-derived by this same extractor**, never copied from
  the skill: comparing a round cut one way to a baseline cut another measures the cut.
  Measured on a production deployment, that re-derivation found the skill publishing a
  277-character archive median where this code reads 267, and 371 on a reference round
  where it reads 406 on the same 17 drafts; the skill's "three semicolons in seventeen"
  were three *occurrences* spread over two drafts. The shortest agrees exactly at 148.
  Both readings are published: the gap is a difference of cut, not a silent correction,
  and the skill stays as is because it is a fingerprinted source of the reviewer, which
  costs a `make qa-gate` to edit.
- `email_classifier/draft_pairing.py` + `email_classifier/fact_read.py`: which sent
  reply a draft is compared with, and what changed in it (STU-1998). The join took the
  newest send on the conversation, which on a production window often answered a later
  question and once predated the draft by months. A draft is now paired with the first
  send after its last `ok` write in `logs/drafts/journal.jsonl`, and with nothing when
  no send follows; a draft the journal never recorded is a `no write time` row, since
  any other instant is a guess. Both readers (`sent_replies` on Graph and IMAP) apply
  the same `first_send_after`, and `reviewed` and `pilot_delta` read the same
  `write_times`. Lexical similarity cannot tell the reviewer's own words for the same
  facts from a correction, so `--facts` (opt-in, live) asks `draft-facts`, a reader
  with no history and no tools, whether the sent reply kept, extended or contradicted
  the draft's facts. Its verdict restates its lists and is cross-checked like
  `draft-coverage`'s: `incoherent` and `error` stay apart. `by_outcome` keeps the
  lexical bands; `by_facts` is what an audit quotes. The reader runs on
  `STUDIO_PROVIDER` (STU-2088): it reads real mail on the serving host, where the
  bake-offs' `anthropic` default has no key and every read came back `error`.
- `email_classifier/pilot_delta.py` + `scripts/pilot_delta.py`: the same reading as
  `audit_run.py reviewed`, but over a whole deployment window and grouped by the
  commit that drafted (STU-1182). `reviewed` figures one run; a deployment's exit
  question covers its whole window, and the first answer to it (2026-09-01) took a
  throwaway aggregator over twenty dumps that nothing committed, so a figure nobody else
  can reproduce. What this adds is the half that kept that first figure from answering
  its own title: every scored draft was written by a commit, and the voice rules landed
  in one particular commit. Reading surviving semicolons as "the mechanics rule did not
  take" was reading a rule that never ran. A round is therefore placed before any figure
  is averaged, by `git merge-base --is-ancestor` and not by a date: the serving host moved
  forward and back within that window, so clock order does not answer for ancestry.
  Three buckets, not two, because "we cannot tell" is an answer of its own: a report that
  names a commit this clone does not have is evidence in neither direction, and counting
  it as `pre-rules` would let a missing object plead that the rules did nothing. That is
  STU-912's direction applied to a bucket. A conversation drafted several times is one
  conversation: it is scored on its most recent round, at the report's mtime and never
  today, otherwise a thread that was re-drafted would weigh more than one answered first
  time, and the old body is not the one the reviewer saw. The ~10 floor is not a gate:
  below it the table still prints and says so, for the reason `_lengths` returns `null`
  rather than 0 (a thin denominator that does not announce itself reads as a whole one).
  `--offline` places rounds without calling Graph, which answers "is there enough
  carrying traffic" for free.
- `email_classifier/correction_rules.py` + `scripts/correction_rules.py`: candidate
  drafting rules read off the reviewer's corrections (STU-1108). A precedent search
  already carries every sent reply forward as phrasing; what it cannot show is what was
  wrong with ours, since the send overwrites the draft. `make correction-rules` takes
  `pilot_delta`'s window (latest drafting round per conversation, `written_at`, the
  reader's `sent_replies`), drops replies sent unchanged, and hands every remaining pair
  to `draft-corrections` in one item: a rule is a pattern across pairs, and a reader that
  saw one pair at a time could not count support. The output is a file under
  `logs/corrections/` for a human to accept into a skill or reject, never a prompt edit
  and never a few-shot example, so one reviewer's habits do not become the drafter's.
  A candidate needs **two** distinct conversations: one pair cannot tell a drafter
  defect from reviewer noise (a fact only the reviewer had, a reply moved to another
  channel), and a higher bar would keep the list empty at a pilot's dozen corrections a
  round. A rule cited by one pair is listed under `single_pair`, not dropped; a cited id
  the input never carried does not count; and every pair must be either cited or
  `unexplained`, never both or neither, or the reading is `incoherent`. Both bodies go
  through `precedents.redact` before the reader sees them, which is also what the file
  keeps: read, never restored, so index-free placeholders. The shelf is age alone, 30
  days. Not `learn/`: that store promotes classification verdicts into rules a machine
  applies, and here the rule is prose a person writes. Its reader follows
  `STUDIO_PROVIDER` for the reason `reviewed --facts` does (STU-2088).
- `email_classifier/pilot_gate.py` + `scripts/pilot_gate.py`: a deployment's three exit
  conditions, read from artifacts rather than from an impression (STU-1242): a week of
  unattended live rounds, no PII leak, and an accepted-draft ratio the audit records. The
  conditions lived as prose in a tracker milestone, and nobody owned the check. A
  milestone description is prose in a milestone, the same thing as a paragraph in a
  skill file, and this repository has seen that shape fail: STU-1125 filed a question
  under "Unanswered questions" and closed, STU-1121 closed by pointing at such a
  paragraph as if it were ownership, and it took STU-1155 to own the request. The price
  here is of another kind: the deployment does not close *late*, it closes on an
  impression or not at all.
  Three conditions and three artifacts, because they do not come from the same place.
  That is also why it is not a subcommand of `scripts/audit_run.py`, which figures *one*
  run where each question here covers a window. The week is read from push-path reports
  (`provenance.input_path == LIVE_INPUT`, STU-859: a `make run` is someone at the
  keyboard, whatever the machine) **and** from the `source: cron` lines of
  `state/health-checks.jsonl`. Neither half implies the other: `reports/` can say nothing
  about the hours between rounds, and an expiring subscription produces neither a report
  nor an error. The `cron` filter is the one from STU-1082, and its rule is taken as is:
  a window whose hours are not covered by hourly runs cannot decide, quiet and
  unmonitored being the same silence. A week counts distinct UTC days carrying a round,
  not elapsed hours: a host that runs on day 1 and day 8 covers a clock week and no
  operating week.
  Three verdicts, not two, STU-912's rule: `not measurable` is an answer distinct from
  `not met`. On the ratio it is the *expected* answer, not a defect: STU-1182 measured
  three conversations drafted by a commit carrying the voice rules, and three drafts give
  a ratio no denominator. The floor is `pilot_delta.enough`'s; below it the condition
  names the count, because "not enough traffic to state a ratio" is what decides whether
  the window extends or the criterion changes, and that decision belongs to a human
  looking at a number.
  Two things it deliberately does not do. It subtracts no known noise from the recall
  inventory: STU-1227 over-reports by matching substrings and STU-1228 leaves keymap
  entries that name nobody, neither is a leak, and a reader who learns to skim the list
  is exactly how a real one goes unnoticed, so both are named in the summary and nothing
  is removed. And it sets no bar on the ratio: `met` there means the audit can *state* a
  number, never that the number is good. The exit criterion sets no threshold, and a
  threshold invented here would be one nobody measured.
  An unsettled run degrades a clean pass instead of being ignored: "no leak over the
  window" is a claim about the window, and a sweep that skipped part of it has made a
  smaller claim. Three populations, and merging two of them is how this condition lies: a
  run without `anonymized_fields` sent mail in clear *by policy* and has no gate to fail,
  a run with a scope but no surviving `sent` record decides in neither direction, and
  only the rest carries a verdict. The Studio-journal fallback `audit_run.py
  recall` accepts is not reused: it attributes payloads by reading journals a neighbour
  may have written and carries a caveat for that reason, and a caveat a sweep prints once
  per run is a caveat nobody reads. A run without its own record is therefore reported
  unsettled, and is decided one at a time where the caveat is in front of a human.
  Exit codes, each its own condition as the family numbers them: **0** everything is met,
  **1** a condition is not, **2** a condition could not be measured and none failed (the
  shape of `recall`: a fault found is actionable today where an unmeasured one is a round
  trip to the host), **5** no report under the given directories, a reader pointed at the
  wrong place rather than an empty window (the refusal of STU-1087 and STU-1109). No
  `tree_guard`, for `email_classifier/pilot_delta.py`'s reason: nothing is published that another tree
  could contradict.
- `email_classifier/eval/`: the eval harness. Stage-1 replay against golden labels
  (STU-816), anonymized versus cleartext A/B (STU-837), and provider parity (STU-848): the
  same fixture encoded as a native Gmail resource and a native Graph resource, read back
  through both projections, must give the same EmailInput and the same verdict. Plus the
  name-detection gate (STU-925): `spans` compares the `person` spans the detector finds on
  the deployment's fixtures with its frozen span set (`make eval-spans`, re-frozen by
  `make eval-spans-seed`), as a set and not a count. A moved span fails, and an extra span
  fails like a missing one (STU-909 and STU-911 were over-redaction bugs). That is what
  holds the upper bounds of the `ner` extra: widening them is one line in `pyproject.toml`,
  and `provenance.detector_stack` only names the drift after a run has already sent mail
  to an LLM (STU-919).
- `logs/`: frontier logs, apply journals under `logs/applied/`, the drafts journal
  `logs/drafts/journal.jsonl` and the stage record `logs/drafts/stages.jsonl`, plus the
  shared keymap, the run's own and what it sent under `logs/anonymization/` (all
  gitignored, written into the deployment root).
