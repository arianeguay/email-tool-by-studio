import os
import shutil
import subprocess
import sys
import types
from functools import lru_cache
from pathlib import Path

# The kernel ships no deployment of its own: unless the caller names one, the suite runs
# against the reference deployment. Set before any email_classifier import, since
# deployment.ROOT is resolved once, at import.
os.environ.setdefault(
    "EMAIL_CLASSIFIER_DEPLOYMENT_ROOT", str(Path(__file__).resolve().parents[1] / "reference-deployment")
)

import pytest
import yaml

from email_classifier import anonymize
from email_classifier.apply.journal import ApplyJournal
from email_classifier import deployment

REPO_ROOT = Path(__file__).parent.parent
FIXTURES_DIR = deployment.ROOT / "fixtures"
RULES_DIR = deployment.ROOT / "rules"
EVAL_DIR = deployment.ROOT / "eval"
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _env_example_keys() -> tuple[str, ...]:
    """Every variable `.env.example` names, read from the file rather than copied.

    Copied, the list becomes a second place to add a variable, and forgetting the
    second is the failure this closes (STU-1076). The file the operator fills in is
    the file the fixture reads, so a key added to one is cleared by the other.
    """
    return tuple(
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )


ENV_EXAMPLE_KEYS = _env_example_keys()


@pytest.fixture(autouse=True)
def no_ambient_env(monkeypatch):
    """`make test` exports `.env`, so the suite would read the operator's own values.

    Several are ones the suite acts on: `WEBHOOK_HOST`/`WEBHOOK_PORT` compose the
    fallback health URL, `WEBHOOK_SSH_HOST` makes a health read dial that host,
    `WEBHOOK_STALE_AFTER_SECONDS` is the threshold alerted on, and `ALERT_WEBHOOK_URL`
    is an endpoint `alerts.notify` would POST to for real.

    Clearing the whole set rather than the names that have burned us so far (STU-1074
    closed two, one at a time, after the second left `main` red for eighteen hours):
    an ambient value fails green on the machine that wrote the `.env` and red in CI,
    so the next variable to be leaned on is found by whoever is least able to fix it.
    A test that needs one sets it.
    """
    for key in ENV_EXAMPLE_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session")
def _stub_claude_dir(tmp_path_factory) -> Path:
    bin_dir = tmp_path_factory.mktemp("stub-claude")
    exe = bin_dir / "claude"
    exe.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    exe.chmod(0o755)
    return bin_dir


@pytest.fixture(autouse=True)
def claude_on_path(monkeypatch, _stub_claude_dir):
    """The `claude-code` preflight refuses without `claude` on PATH (STU-1760), and the
    CI runner has none: the suite must not depend on the host having it. Appended, so a
    real one still wins; a test about the refusal sets PATH itself."""
    monkeypatch.setenv("PATH", f"{os.environ.get('PATH', '')}{os.pathsep}{_stub_claude_dir}")


class FakeDrafts:
    """Stands in for `studio run drafts` (STU-946), which no test may shell out to."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def draft(self, report_path, emails):
        self.calls.append((report_path, [email["message_id"] for email in emails]))
        if self.error:
            raise self.error


class FakeWriter:
    """GmailWriter interface, in memory — no service, no network.

    Keeps per-message label state so idempotence and the apply->undo round-trip
    can be exercised against the same fake mailbox. Location is label state too:
    both real writers report INBOX and TRASH through `label_ids` (STU-983), so
    `trash` moves a message from one to the other here — an in-memory mailbox that
    kept trash on a side list would let the executor read a trashed message as
    still sitting in the Inbox.

    `untrash` restores the labels the message had before the trash, which is what
    Gmail does; Outlook's always moves to the Inbox instead. The weaker of the two
    is modelled on purpose: a plan that only works because untrash happens to land
    in the Inbox has to fail here.
    """

    def __init__(self, fail_on=(), trash_fails_on=()):
        self.fail_on = set(fail_on)
        # Separate from `fail_on` so a test can fail the trash move alone: the
        # marker is written before it, and what a crash between the two leaves
        # behind is the whole point of that order (STU-983).
        self.trash_fails_on = set(trash_fails_on)
        self.label_names = {}      # name -> id
        self.message_labels = {}   # message_id -> set of label ids
        self.ensure_calls = []
        self.before_trash = {}     # message_id -> labels held when it was trashed

    def find_label(self, name):
        return self.label_names.get(name)

    def ensure_label(self, name):
        self.ensure_calls.append(name)
        return self.label_names.setdefault(name, f"L_{name}")

    def add_label(self, message_id, label_id):
        if message_id in self.fail_on:
            raise RuntimeError("boom")
        self.message_labels.setdefault(message_id, set()).add(label_id)

    def remove_label(self, message_id, label_id):
        self.message_labels.setdefault(message_id, set()).discard(label_id)

    def trash(self, message_id):
        if message_id in self.fail_on or message_id in self.trash_fails_on:
            raise RuntimeError("boom")
        labels = self.message_labels.setdefault(message_id, set())
        self.before_trash[message_id] = set(labels)
        labels.discard("INBOX")
        labels.add("TRASH")

    def untrash(self, message_id):
        labels = self.message_labels.setdefault(message_id, set())
        labels.discard("TRASH")
        labels |= self.before_trash.get(message_id, set()) & {"INBOX"}

    @property
    def trashed(self):
        return sorted(
            mid for mid, labels in self.message_labels.items() if "TRASH" in labels
        )

    def label_ids(self, message_id):
        return sorted(self.message_labels.get(message_id, set()))

    def labels_of(self, message_id):
        ids = self.message_labels.get(message_id, set())
        return {name for name, lid in self.label_names.items() if lid in ids} | (
            ids & {"INBOX", "TRASH"}
        )


def inboxed(writer, *message_ids):
    for mid in message_ids:
        writer.message_labels[mid] = {"INBOX"}
    return writer


def trashed(writer, message_id):
    """The state an applied `mark_for_delete` leaves behind: in the trash, carrying
    the marker and `ec/deleted`. That label, not the absence of a bucket, is what
    `providers.trashed_by_us` reads — a message applied as keep_visible is marked
    and unbucketed too."""
    writer.message_labels[message_id] = {
        "TRASH",
        writer.ensure_label("ec/processed"),
        writer.ensure_label("ec/deleted"),
    }
    writer.ensure_calls.clear()
    return writer


def archived(writer, message_id, bucket):
    """The state an applied `label_archive` leaves behind: out of the Inbox,
    carrying its bucket label and the marker. What a later run diffs against."""
    writer.message_labels[message_id] = {
        writer.ensure_label(f"ec/{bucket}"),
        writer.ensure_label("ec/processed"),
    }
    writer.ensure_calls.clear()
    return writer


# ── the `ner` extra ──────────────────────────────────────────────────────────
#
# The extra is heavy and optional, so `pytest` has to pass without it (STU-857). Three
# stubs are what make that true, and they live here rather than once per test module
# because a copy that drifts is a copy that stops standing for the real thing.

# The format half of the tokenizer is tested without the extra and the person half is
# skipped. What must never happen is the third case — person detection quietly finding
# nobody — which `test_person_detection_refuses_to_run_without_the_extra` covers.
requires_gliner = pytest.mark.requires_gliner


@lru_cache(maxsize=1)
def _no_detector() -> str | None:
    """Why the person half cannot run here, or None — the package *and* the weights.

    Probing the import alone guards the package and never the model, so a machine with
    the extra installed and the weights unreachable — an emptied `HF_HOME`, a corrupted
    download, a card the CPU fallback cannot get off — ran these tests and failed them
    instead of skipping them (STU-992). The reason names which of the two it was: an
    absent extra is a machine that was never set up, an unloadable model is one that
    was and has broken since.

    The package half is `missing_detector` and not a second spec probe, so `pin_gliner`
    moves the answer here the way it moves it everywhere else.
    """
    if absent := anonymize.missing_detector():
        return absent
    try:
        anonymize._model()
    except Exception as exc:
        return f"the `ner` extra is installed but the model did not load: {exc!r}"
    return None


def pytest_runtest_setup(item):
    """Skip at setup and not at collection, so only a run that wants the model pays for it.

    A `skipif` condition is evaluated for every marked test the moment the module is
    collected, which would load 1.3 GB of weights during `pytest tests/test_stage1.py`.
    """
    if item.get_closest_marker("requires_gliner") and (why := _no_detector()):
        pytest.skip(why)


class FindsNobody:
    """A detector that finds no one — for a test whose subject is not who is in the text."""

    def predict_entities(self, text, labels, threshold):
        return []


class FindsTheChildInTheHeadOnly:
    """The detector's miss in a history turn, its claim in the head (STU-1993)."""

    def predict_entities(self, text, labels, threshold):
        start = text.find("Rosalie")
        if "invitation" not in text or start < 0:
            return []
        return [{"start": start, "end": start + 7, "text": "Rosalie", "score": 0.9}]


def stub_detector(monkeypatch):
    """Run the tokenizer's person half with no weights and no `ner` extra.

    Every format category still goes through its real regex; only the model load is
    replaced. A test that asserts on a *detected name* must ask for the real detector
    with `real_ner` instead, and skip when the extra is absent — the stub would make it
    pass by finding nothing.
    """
    monkeypatch.setattr(anonymize, "_model", FindsNobody)


def pin_gliner(monkeypatch, present=True):
    """Pin whether `import gliner` succeeds, whatever this machine has installed.

    `missing_detector` imports the package rather than probing a spec, so `None` in
    `sys.modules` is what an absent extra looks like from the inside.
    """
    monkeypatch.setitem(
        sys.modules, "gliner", types.ModuleType("gliner") if present else None
    )


@pytest.fixture
def journal(tmp_path):
    return ApplyJournal(tmp_path / "applied" / "run.jsonl", "run")


def pytest_exception_interact(report, call):
    """A `check=True` subprocess failure otherwise reports only the exit status.

    `CalledProcessError.__repr__` names the command and the return code; what the
    child actually printed lives on the exception and pytest never shows it.
    """
    error = call.excinfo.value
    if not isinstance(error, subprocess.CalledProcessError):
        return
    for name in ("stdout", "stderr"):
        stream = getattr(error, name)
        if stream:
            text = stream.decode(errors="replace") if isinstance(stream, bytes) else stream
            report.sections.append((f"Captured {name} of the failing subprocess", text))


# ── the debounce window, in tests ────────────────────────────────────────────
#
# `_DEBOUNCE_SECONDS` is 15s in production, sized against real Graph bursts (STU-939).
# A test that folds a burst into one run only needs a window that outlasts the posts
# it makes on the same thread -- microseconds -- so the value it picks is arbitrary
# and is paid in full, in real time, on every run. Four tests picking 5s were 25s of
# the suite's wall clock: half the hermetic run, spent asleep.
#
# Shared rather than per-module so the next test to need a window cannot reintroduce
# the cost by picking its own number.
BURST_WINDOW = 0.5


# ── editing a prompt source under test ───────────────────────────────────────
#
# `.studio/` is repository state the whole suite reads: `prompt_fingerprint` hashes the
# agents, skills and contracts a draft is built from, and `anonymize.vouched()` harvests
# the club skill. A test that edits one of those files in place is writing state its
# neighbours are reading, and under `pytest -n auto` the neighbours are separate
# processes over one checkout, so that write is a window they can land in (STU-1085).
# Two of them did, each failing once in a handful of runs and passing alone every time:
#
#   - `write_text` truncates before it writes. `vouched()` reads the skill and then
#     `_skill_people()` reads it a second time; catching the whole file on the first
#     read and the truncated one on the second leaves the eight words of the skill's
#     declared `people:` un-subtracted, which is exactly the eight forms
#     `test_the_published_rate_was_measured_against_this_trees_evidence` reported the
#     curated keep-set grown by — a false alarm on the one rate the repo publishes.
#   - the appended file and the restored one fingerprint differently, so two
#     `drafts-pre` runs straddling the window produced items differing by more than
#     their `run_id`.
#
# So the rule, and the fixture that makes it the easy path: a test that needs a prompt
# source to change edits a copy, never the checkout.
@pytest.fixture
def studio_copy(tmp_path) -> Path:
    """A private `.studio/`, editable without writing state another worker is reading."""
    destination = tmp_path / "studio"
    shutil.copytree(
        deployment.ROOT / ".studio",
        destination,
        # Followed, not copied: reference-deployment's `contracts`, `pipelines` and
        # `tools` are relative links to the kernel's, which dangle once moved, and an
        # edit through one would land in the shared file.
        symlinks=False,
        # Studio's own run logs are no part of a prompt and are not small: 12 MB in the
        # main checkout, and a symlink into it from every worktree (STU-1011).
        ignore=shutil.ignore_patterns("runs"),
    )
    return destination


# ── where a webhook app under test is allowed to write ───────────────────────
#
# Every one of these defaults to a path under the repo itself, and forgetting one is
# silent: the app runs, the test passes, and the write lands in the checkout. Under
# `pytest -n auto` it stops being silent -- two workers racing on the same
# `state/runs.json` collide in `os.replace`, the run raises, and the queue worker reads
# that as a failed notification and retries it, which is the extra fetch and the extra
# stage-2 call three tests were failing on (STU-1055).
#
# One list, shared by every factory, so the next path added to `create_app` is added
# here once rather than in four places minus one.
def served_policy(tmp_path: Path) -> Path:
    """The repo's policy with its date floor widened, for a test whose mail is dated.

    Everything else is the served file, so a test still reads the real `auto_apply`
    and the real anonymization scope. Only `start_date` moves, and it has to: the
    floor defaults to today (STU-1123), while a push fixture carries a written-down
    date so its ordering against a watermark or a sibling can be asserted at all.
    Widening it is what keeps those tests about the mechanism they name.
    """
    body = yaml.safe_load((RULES_DIR / "policy.yaml").read_text(encoding="utf-8"))
    body["start_date"] = "2000-01-01"
    path = tmp_path / "rules" / "policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, allow_unicode=True), encoding="utf-8")
    return path


def webhook_paths(tmp_path: Path) -> dict:
    return {
        "learned_path": tmp_path / "rules" / "learned.yaml",
        "log_path": tmp_path / "logs" / "frontier.jsonl",
        "verdict_store_path": tmp_path / "logs" / "verdicts.jsonl",
        "keymap_path": tmp_path / "logs" / "anonymization" / "keymap.json",
        "report_dir": tmp_path / "reports",
        "journal_dir": tmp_path / "logs" / "applied",
        "audit_log_path": tmp_path / "audits" / "audit_log.json",
        "skipped_path": tmp_path / "audits" / "skipped.json",
        "drafts_stage_log": tmp_path / "logs" / "drafts" / "stages.jsonl",
        "state_file": tmp_path / "state" / "gmail-history.json",
        "liveness_file": tmp_path / "state" / "last-notification.json",
        "run_count_file": tmp_path / "state" / "runs.json",
        "subscription_file": tmp_path / "state" / "outlook-subscription.json",
        "queue_file": tmp_path / "state" / "notifications.jsonl",
    }
