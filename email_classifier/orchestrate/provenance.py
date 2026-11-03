"""Run provenance (STU-819): what produced a report.

Reports are the audit corpus, so an audit must read how a run was launched instead of
reconstructing it from failure signatures. Collection never raises: a checkout without
git or an unreadable rules file degrades to null rather than aborting a classification.
"""

import hashlib
import importlib.metadata
import os
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

import yaml


# The input of a run whose mail never touched disk (webhook push). Naming it beats a
# null, which an audit reads as "unrecorded" and then guesses at a dump (STU-859).
LIVE_INPUT = "webhook:push-notification"

# A run a scheduler launched with nobody at the keyboard (STU-2069). An IMAP mailbox has
# no push path, so a deployment polling it on a timer floods the gate the way push does.
TRIGGER_ENV = "EMAIL_CLASSIFIER_TRIGGER"
SCHEDULE_TRIGGER = "schedule"

# The name detector's own dependencies. Tokenization is upstream of every span GLiNER
# returns, so these decide recall the way rules.yaml decides a rule outcome (STU-919).
_DETECTOR_PACKAGES = ("gliner", "transformers", "torch", "sentencepiece", "protobuf")
# The checkout this package was imported from. A deployment pins the kernel as a
# submodule, so the deployment's own commit does not name the code that ran (STU-1996).
KERNEL_ROOT = Path(__file__).resolve().parents[2]

# Tracked files the serving host rewrites by design (STU-1084, STU-1081), relative to
# the deployment root. A change to them is not code served that no commit holds.
HOST_WRITTEN = ("audits/audit_log.json", "audits/skipped.json")

# The provider this host runs, whichever command was typed (STU-1128). Both
# orchestrations pass it to Studio as `--provider`: the push path reads it here, and the
# Makefile defaults `PROVIDER` to it for the Studio-orchestrated ones. Before that the
# two disagreed — the push path ran on the host's provider and `classify-batch` on the
# agent's own pin — so the same mail classified under two providers depending on which
# command was typed, and one of them died.
HOST_PROVIDER_ENV = "STUDIO_PROVIDER"

# How `config.yaml` says a provider takes a credential from the environment. The bare
# form only: the engine also reads `${VAR:-default}` (`env-vars.ts`), and a block that
# carries its own default is a block with nothing missing.
_INTERPOLATION = re.compile(r"\$\{(\w+)\}")


_ENV_REF = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def _interpolate(value):
    """`${VAR}` / `${VAR:-default}` resolved the way Studio resolves config, where an
    empty variable counts as unset. Studio sees the value after this; provenance must too."""
    if not isinstance(value, str):
        return value
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value) or None


def resolved_provider(studio_dir: Path, override: str | None = None,
                      agent: str = "classifier") -> str | None:
    """Which provider an agent's calls actually ran on.

    Studio resolves it CLI flag > the agent's own `provider` > `defaults.provider`
    (`stage-executor.ts` overwrites the agent's field with the flag). Provenance
    records what ran, so it has to resolve it the same way; a caller that hardcoded
    the config default would write the wrong one into every report the moment an
    agent names its own, which STU-1013 had the classifier do until STU-1133.

    Per agent rather than per run, and it stays that way with no agent naming one
    today: the middle term is the whole reason this function exists, and collapsing
    it while it happens to be empty is how the next pin goes unrecorded.
    """
    if override:
        return override
    try:
        config = yaml.safe_load(
            (studio_dir / f"agents/{agent}.agent.yaml").read_text(encoding="utf-8"))
        if pinned := _interpolate(config.get("provider")):
            return pinned
        defaults = yaml.safe_load((studio_dir / "config.yaml").read_text(encoding="utf-8"))
        return _interpolate((defaults.get("defaults") or {}).get("provider"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError, AttributeError):
        return None


def unusable_provider(studio_dir: Path, provider: str | None) -> str | None:
    """None when the run may proceed, else why `provider` cannot answer a call here.

    Studio builds a provider on first use, so a missing credential arrives as one dead
    conversation inside an otherwise green run: every stage success, `errors: 1`, and a
    report whose `provenance.provider` names a provider it never reached (STU-1128).
    Refusing up front is what turns that into a run that failed and said why.

    What a provider needs is already declared — its `config.yaml` block interpolates
    the environment — so an unset variable there is the whole answer, and no list of
    which provider wants which credential is kept here to go stale.

    Cannot-tell degrades to None rather than to a refusal: a provider the config does
    not describe is one the CLI registers itself (`mock`), and a config that will not
    parse is Studio's own error to raise on the first call. This guard only speaks
    where the config gave it something to read.

    `claude-code` needs one thing no config block declares: Studio spawns a bare
    `claude` off the PATH, so a missing executable failed every escalation for 21
    hours on the serving host with nothing alerting (STU-1760).
    """
    if not provider:
        return None
    if provider == "claude-code" and shutil.which("claude") is None:
        return (
            f"provider `claude-code` cannot spawn: no `claude` executable on PATH "
            f"{os.environ.get('PATH', '')!r}. Install Claude Code for this account, or "
            f"point {HOST_PROVIDER_ENV} at a provider this host runs."
        )
    try:
        config = yaml.safe_load((studio_dir / "config.yaml").read_text(encoding="utf-8"))
        block = (config.get("providers") or {}).get(provider)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, AttributeError):
        return None
    if not block:
        return None
    missing = sorted({name for name in _INTERPOLATION.findall(yaml.safe_dump(block))
                      if not os.environ.get(name)})
    if not missing:
        return None
    return (
        f"provider `{provider}` cannot initialize: {', '.join(missing)} unset in the "
        f"environment. Set it, or point {HOST_PROVIDER_ENV} at a provider this host runs."
    )


def _git(repo_root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def _sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _relative(path: Path | str, repo_root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path)


def collect(
    provider: str,
    rules_path: Path,
    repo_root: Path,
    learned_path: Path | None = None,
    anonymized_fields: list[str] | None = None,
    input_path: Path | str | None = None,
    start_date: date | None = None,
    mailbox: str | None = None,
) -> dict:
    commit = _git(repo_root, "rev-parse", "HEAD")
    # Untracked files are agent scratch, not the code that ran: counting them pinned
    # git_dirty to true on every run (STU-824). The audit files are the serving host's
    # own writes after every run, so counting them pinned it again (STU-2062).
    dirty = _git(repo_root, "status", "--porcelain", "-uno", "--", ":/",
                 *(f":(exclude){path}" for path in HOST_WRITTEN))
    return {
        "provider": provider,
        "git_commit": commit,
        "git_dirty": bool(dirty) if dirty is not None else None,
        "kernel_commit": _git(KERNEL_ROOT, "rev-parse", "HEAD"),
        "rules_sha256": _sha256(rules_path),
        # The learned layer is gitignored, so the commit hash does not pin it (STU-822).
        "learned_sha256": _sha256(learned_path),
        # Which fields Stage 2 tokenized before the LLM saw them; empty = cleartext.
        # An audit cannot tell the two apart from the verdicts alone (STU-393).
        "anonymized_fields": list(anonymized_fields or []),
        # Which versions detected the names in those fields; null = the extra was absent,
        # so the run either found no name or refused to start (STU-919).
        "detector_stack": {p: _version(p) for p in _DETECTOR_PACKAGES},
        # Which mail the run consumed (STU-859). A non-file marker (the webhook's live
        # notification) hashes to null: there is no artifact to cross-check against.
        "input_path": _relative(input_path, repo_root) if input_path is not None else None,
        "input_sha256": _sha256(Path(input_path)) if input_path is not None else None,
        # How far back this run was allowed to reach (STU-1123). What the report does
        # not hold and is older than this was out of scope, not missed — and no
        # artifact told those apart before, which is the whole of what the floor buys
        # an audit. Null on a path that predates the floor, never a stand-in date.
        "start_date": start_date.isoformat() if start_date is not None else None,
        # Which mailbox the run read (STU-1183). Two have existed — the staging box
        # and the production mailbox since 2026-08-30 — and a report from either read
        # identically, so an audit that took one's ids to the other's store got zero
        # rows back and read that as deleted mail rather than as the wrong mailbox.
        # Handed in already resolved, by `providers.configured_mailbox` and nothing
        # else: the point of the field is that it agrees with the client that did the
        # reading, and a second way to answer it is how the two come apart. Null
        # wherever nothing names one — Gmail always, Outlook under a delegated grant,
        # and any run over a frozen dump, which read no mailbox at all today.
        "mailbox": mailbox,
        # Who launched the run (STU-2069): `schedule` files itself like a push run.
        "trigger": os.environ.get(TRIGGER_ENV) or None,
    }
