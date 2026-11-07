"""Run audit — deterministic checks over a classification run's artifacts.

Pure module: no I/O, callers pass parsed data. The CLI wrapper lives in
scripts/audit_run.py; the /validate-classify-run skill runs it and never
re-implements these checks inline.
"""

import re
from collections.abc import Iterable
from pathlib import Path

from email_classifier.core.contract import Taxonomy, enum_and_consistency_errors
from email_classifier.orchestrate.set_aside import SET_ASIDE_ERROR

CATCH_ALL = "catch_all"

STAGE2_ERROR_TAGS = frozenset({
    "studio_cli_missing",
    "studio_run_timeout",
    "studio_run_failed",
    "studio_output_json_parse_error",
    "studio_run_output_missing",
    "studio_run_output_invalid",
    # Not a studio fault: the provider answered a limit notice instead of a verdict
    # (STU-1016). A known tag, so an audit reads it as a named failure rather than as
    # a report carrying an error nothing recognises.
    "provider_unavailable",
    # Kept out of the fan-out after repeated failed polls (STU-2109): no call was made.
    SET_ASIDE_ERROR,
})


def stage2_calls(verdicts: list[dict]) -> int:
    """A run's stage-2 calls: one per escalated conversation (STU-944, STU-989).

    Derived from the verdicts, not read from `totals`, so a report written before
    `llm_calls` existed still reports its real LLM cost rather than a missing key the
    auditor fills back in as a zero.
    """
    return len({v.get("thread_id") for v in verdicts
                if v.get("path") == "llm" and v.get("error") != SET_ASIDE_ERROR})


def check_report(report: dict, taxonomy: Taxonomy) -> list[str]:
    """Internal-consistency + INV-08 checks on one report. Returns all violations.

    `taxonomy` is this deployment's own bucket shape (STU-1461), required and never
    defaulted: INV-08 is a check against a specific deployment's tables, and there is
    no taxonomy this function could assume on a caller's behalf.
    """
    errors: list[str] = []
    verdicts = report.get("verdicts", [])
    totals = report.get("totals", {})

    if totals.get("emails") != len(verdicts):
        errors.append(f"totals.emails={totals.get('emails')} but {len(verdicts)} verdicts")
    det = sum(1 for v in verdicts if v.get("path") == "deterministic")
    llm = sum(1 for v in verdicts if v.get("path") == "llm")
    err = sum(1 for v in verdicts if v.get("status") == "error")
    for name, computed in (("deterministic", det), ("llm", llm), ("errors", err)):
        if totals.get(name) != computed:
            errors.append(f"totals.{name}={totals.get(name)} but verdicts say {computed}")
    # One stage-2 call per escalated conversation (STU-989). Checked only when the key is
    # there: a report written before the field is missing it, not lying about it.
    if "llm_calls" in totals:
        calls = stage2_calls(verdicts)
        if totals["llm_calls"] != calls:
            errors.append(
                f"totals.llm_calls={totals['llm_calls']} but verdicts span {calls} "
                "escalated conversation(s)"
            )
    if verdicts:
        ratio = det / len(verdicts)
        if abs(report.get("ratio_deterministic", -1) - ratio) > 1e-9:
            errors.append(
                f"ratio_deterministic={report.get('ratio_deterministic')} but computed {ratio}"
            )

    seen: set[str] = set()
    for v in verdicts:
        mid = v.get("message_id")
        if mid in seen:
            errors.append(f"duplicate message_id in verdicts: {mid} (STU-395 dedup broken)")
        seen.add(mid)

        path, status = v.get("path"), v.get("status")
        if path == "deterministic" and v.get("rule_id") == CATCH_ALL:
            errors.append(f"{mid}: deterministic verdict from catch_all (chokepoint bypassed)")
        if path == "llm" and v.get("rule_id") != CATCH_ALL:
            errors.append(f"{mid}: llm verdict from rule '{v.get('rule_id')}' (not catch_all)")

        if status == "ok":
            for e in enum_and_consistency_errors(
                v.get("bucket"),
                v.get("confidence"),
                v.get("suggested_action"),
                taxonomy,
                v.get("subtype") if path == "llm" else None,
            ):
                errors.append(f"{mid}: {e}")
            if path == "llm" and not (v.get("subtype") and v.get("reasoning")):
                errors.append(f"{mid}: llm ok verdict missing subtype/reasoning")
        elif status == "error":
            if v.get("error") not in STAGE2_ERROR_TAGS:
                errors.append(f"{mid}: unknown error tag '{v.get('error')}'")
        else:
            errors.append(f"{mid}: unknown status '{status}'")
    return errors


# A leg's first call can only write its prefix; a hit ratio is a claim about a second
# one, and below this the 0.0 is arithmetic rather than a finding (STU-1057).
MIN_RATIO_CALLS = 2

# Providers that spawn one process per call, so no call can read back a prefix another
# wrote. Measured under `claude-code` in STU-1133 — hit ratio 0.0 over 12 calls and again
# over 27, against 0.30 for the same corpus on the API — and nothing reachable from this
# repo changes it: not a contract's tool floor, not an agent's pin, not `prompt_cache`.
_NO_PREFIX_REUSE = frozenset({"claude-code"})


def check_cache_hits(
    ratios: dict[str, tuple[float | None, int, int, str | None]],
) -> tuple[list[str], list[str]]:
    """Legs that stopped reading their cached prefix back (STU-1057).

    A leg that loses its caching costs several times what it should and says
    nothing: STU-1030 took the anti-theatre `minimum` out of the drafting
    contract, which was the only thing making `prompt_cache: auto` resolve to
    caching, and the drafts leg ran at 0.0 for weeks at four times the price
    while every artifact looked normal.

    Zero and not a tuned floor. Reading nothing back is a mechanism failure — a
    contract that dropped a tool floor, an agent that lost its pin, a provider
    whose `auto` resolves elsewhere — where a low ratio is a degradation. The two
    ratios ever measured here are 0.30 and 0.778, and a floor between them would
    be a number nothing supports.

    Zero hits has two shapes and the message names the one it measured (STU-1080).
    A prefix written and never read back is the leg paying cache-write for nothing;
    a prefix never written at all costs nothing extra and has a different cause —
    a model whose minimum cacheable prefix is above what this leg sends, which is
    what run `d2592ee5` was, on the haiku default and writing no block at all. An
    audit that trusts the wrong sentence looks for the wrong defect.

    A third cause takes the first shape and is not a violation (STU-1145): under a
    provider in `_NO_PREFIX_REUSE` the write is unreadable by construction, so the
    finding is returned as a note. Returning nothing was the other option and is
    worse — a host silently on such a provider would look like one whose leg caches.
    Returning a violation is what STU-1133 left behind: `make run`, `classify-batch`
    and every bake-off would file the same line forever, `audit_filing` would file it
    unattended on the first multi-conversation push burst (STU-939), and a check that
    fails identically every time is one nobody reads the day it means something.

    The exemption is narrow on purpose, and each edge is what keeps it from becoming
    the pre-built escape hatch that silences a real regression. It covers only the
    written shape: a prefix never written is still the model's minimum cacheable
    prefix, still actionable, still a violation everywhere. It covers only providers
    measured unable to reuse a prefix, never a leg that merely "should not cache".
    And a leg with no recorded provider keeps the old behaviour — `None` is a leg
    written before STU-1145, and unknown must not be the way through.
    """
    violations, notes = [], []
    for name, (ratio, calls, written, provider) in sorted(ratios.items()):
        if ratio != 0.0 or calls < MIN_RATIO_CALLS:
            continue
        if written and provider in _NO_PREFIX_REUSE:
            notes.append(
                f"{name} made {calls} call(s) at cache_hit_ratio 0.0 under provider "
                f"`{provider}`, which spawns a process per call — the prefix each one "
                f"writes cannot be read back by another, so this is structural and not "
                f"a leg that lost its caching")
        elif written:
            violations.append(
                f"{name} made {calls} call(s) and read none of its prefix back "
                f"(cache_hit_ratio 0.0) — the leg is paying cache-write on a prefix "
                f"nothing reads")
        else:
            violations.append(
                f"{name} made {calls} call(s) and cached nothing at all "
                f"(cache_hit_ratio 0.0, cache_creation 0) — no prefix was ever "
                f"written, which a model whose minimum cacheable prefix is above "
                f"this leg's does")
    return violations, notes


def check_log_consistency(report: dict, log_entries: list[dict]) -> list[str]:
    """Cross-check verdicts against this run's frontier-log lines (same run_id)."""
    errors: list[str] = []
    run_id = report.get("run_id")
    entries = [e for e in log_entries if e.get("run_id") == run_id]
    if not entries:
        if report.get("verdicts", []):
            return [f"no frontier-log entries for run {run_id}"]
        return []

    log_ids = [e.get("message_id") for e in entries]
    verdict_ids = [v.get("message_id") for v in report.get("verdicts", [])]
    if sorted(log_ids) != sorted(verdict_ids):
        missing = set(verdict_ids) - set(log_ids)
        extra = set(log_ids) - set(verdict_ids)
        if missing:
            errors.append(f"verdicts absent from frontier log: {sorted(missing)}")
        if extra:
            errors.append(f"frontier-log entries without a verdict: {sorted(extra)}")

    escalated_log = {e["message_id"] for e in entries if e.get("action") == "escalate_to_llm"}
    escalated_report = {
        v["message_id"] for v in report.get("verdicts", []) if v.get("path") == "llm"
    }
    if escalated_log != escalated_report:
        errors.append(
            f"escalation mismatch: log says {sorted(escalated_log - escalated_report)} "
            f"escalated-only, report says {sorted(escalated_report - escalated_log)} llm-only"
        )
    return errors


def check_dump_consistency(report: dict, dump: list[dict]) -> list[str]:
    """Every unique dump message must have exactly one verdict."""
    dump_ids = {e.get("message_id") for e in dump}
    verdict_ids = {v.get("message_id") for v in report.get("verdicts", [])}
    errors: list[str] = []
    if dump_ids - verdict_ids:
        errors.append(f"dump messages without a verdict: {sorted(dump_ids - verdict_ids)}")
    if verdict_ids - dump_ids:
        errors.append(f"verdicts not in the dump: {sorted(verdict_ids - dump_ids)}")
    return errors


def no_frontier_log_note(log_source: str | None = None) -> str:
    """Why `policy_downgrades` is null — the note that travels with the value (STU-912)."""
    where = f" at {log_source}" if log_source else ""
    return (f"policy_downgrades is null: no frontier log{where} was read — the count comes "
            "from the log's action_original, which the report does not carry")


def unrecorded_drafting_note(n: int, run_id: str, log) -> str:
    """Why every drafting figure over `run_id` is a floor — the note that travels
    with the count (STU-1111), read by `audit_run.py cost` and by a filed entry."""
    return (f"{n} execution(s) of the drafting leg under {run_id} opened in {log} and "
            "never closed — killed before `drafts-post`, which records nothing "
            "(STU-917). `drafting_usd` and `llm_calls` are the executions that did "
            "close, so both are floors, and `usd_per_drafted_conversation` is null "
            "rather than the closing executions' own figure wearing the run's name: "
            "the run's denominator is not in this file (STU-1111).")


def metrics(report: dict, log_entries: list[dict] | None = None,
            log_source: str | None = None) -> dict:
    """Observability summary — never fails, feeds the audit log and dashboard.

    A metric this run could not compute is emitted null with a `schema_note` saying so,
    never dropped (STU-912). The auditor hand-copies this output into
    `audits/audit_log.json`, and a key that simply vanished is filled back in as a zero
    no shape check can tell from a real count — the dashboard then compares a real
    downgrade count against a fabricated one.
    """
    verdicts = report.get("verdicts", [])
    error_tags: dict[str, int] = {}
    by_subtype: dict[str, int] = {}
    for v in verdicts:
        if v.get("status") == "error":
            tag = v.get("error") or "unknown"
            error_tags[tag] = error_tags.get(tag, 0) + 1
        if v.get("subtype"):
            by_subtype[v["subtype"]] = by_subtype.get(v["subtype"], 0) + 1
    out = {
        "run_id": report.get("run_id"),
        "provenance": report.get("provenance"),
        "totals": {**(report.get("totals") or {}), "llm_calls": stage2_calls(verdicts)},
        # Per message, unlike llm_calls — the two units only agreed before STU-944.
        "ratio_deterministic": report.get("ratio_deterministic"),
        "by_bucket": report.get("by_bucket"),
        "by_action": report.get("by_action"),
        "by_subtype": dict(sorted(by_subtype.items())),
        "error_tags": error_tags,
    }
    if log_entries is None:
        out["policy_downgrades"] = None
        out["schema_note"] = no_frontier_log_note(log_source)
    else:
        entries = [e for e in log_entries if e.get("run_id") == report.get("run_id")]
        out["policy_downgrades"] = sum(1 for e in entries if e.get("action_original"))
    return out


DRAFTED = "ok"
SENT = "sent"
VANISHED = "vanished"

# What a problem row is named by. `updated` is left out: it is counted, not named,
# and a row that is not `ok` never carries one.
_DRAFT_ROW = ("timestamp", "message_id", "thread_id", "draft_id", "status", "error")


def draft_activity(entries: list[dict], run_id: str) -> dict:
    """What one run's drafting did, read off the shared per-draft journal (STU-1029).

    Scoped by `run_id`, for the reason `drafts.drafted_message_ids` scopes the same
    way (STU-1003): two runs over one mailbox share most of their `needs_reply` set,
    and the stage writes its rows after its own report is on disk, inside the window
    of whatever run started meanwhile — so neither the message id nor the clock
    separates them.

    A row naming no run is unattributable, never this run's: `redraft` answers a
    human, and rows older than STU-917 name nobody. Saying so is the point — grouping
    draft ids by the mail they answer is how the audit of run `8704964d` read a
    human's deletion as the one-draft-per-conversation guard failing (STU-998).

    Those rows are counted and their draft ids listed, not reproduced: a journal
    written before STU-917 is nearly all of them (90 of 115 on this mailbox), and
    spelling each one out buries the run's own answer under rows belonging to nobody.
    The draft id is the handle that misreading turned on, and it is enough to find
    the row it came from. Listed once each: a thread drafted again repeats an id, and
    the volume is already the row count.

    `sent` and `vanished` are counted apart (STU-1549): both are a draft that left
    Drafts, but one is a reviewer sending it and the other a draft gone. A `sent` row
    is not a problem, so it is counted and not named.
    """
    created = updated = sent = vanished = 0
    problems: list[dict] = []
    orphans: list[dict] = []
    for entry in entries:
        if entry.get("run_id") is None:
            orphans.append(entry)
        elif entry.get("run_id") != run_id:
            continue
        elif entry.get("status") == SENT:
            sent += 1
        elif entry.get("status") != DRAFTED:
            vanished += entry.get("status") == VANISHED
            problems.append({key: entry.get(key) for key in _DRAFT_ROW})
        elif entry.get("updated"):
            updated += 1
        else:
            created += 1
    return {
        "run_id": run_id, "created": created, "updated": updated,
        "sent": sent, "vanished": vanished, "problems": problems,
        "unattributable": {
            "rows": len(orphans),
            "draft_ids": list(dict.fromkeys(e["draft_id"] for e in orphans
                                            if e.get("draft_id"))),
        },
    }


UNKNOWN_SELECTION = "unrecorded"


def draft_selection(executions: list[dict]) -> list[dict]:
    """Why each closed drafting execution drafted fewer than it was handed (STU-1186).

    Read off `logs/drafts/stages.jsonl` and not off the per-draft journal, which
    cannot answer it: a conversation `drafts-pre` held never reaches the journal at
    all, so it and one the leg lost leave the same absence. Run `af590fa8` wrote
    `needs_reply: 1, drafted: 0` and only the Studio journal — gitignored and pruned
    at 30 days (STU-1012) — said the conversation already carried a draft.

    Per execution and never folded, for the reason `cost.drafts_executions` hands them
    back one by one (STU-1111): a run killed mid-fan-out resumes under its own id, and
    the resume selected again out of what was left. Summing `needs_reply` across the
    two would count the same conversations twice, and the arithmetic that makes the
    line worth reading — `selected` plus `held` is `needs_reply` — holds per line and
    nowhere else.

    A line without the field reports `selection: "unrecorded"`, never zero held: a run
    predating it decided nothing it could say, and reporting nothing held would say the
    stage held nothing back — the direction that reads as "a reply was lost" when it
    was not, or as "nothing was owed" when something was. This is the direction
    STU-912 sets and the one `audit_filing` already takes for a check family that did
    not run.
    """
    return [{
        "status": entry.get("status"),
        "needs_reply": entry.get("needs_reply"),
        "drafted": entry.get("drafted"),
        "selection": (entry["selection"] if isinstance(entry.get("selection"), dict)
                      else UNKNOWN_SELECTION),
    } for entry in executions]


def unrecorded_selection_note(log) -> str:
    """Why an execution reporting no breakdown is not an execution that held nothing —
    the sentence that travels with `UNKNOWN_SELECTION` (STU-1186)."""
    return (f"at least one execution in {log} predates the selection breakdown "
            "(STU-1186): whether the conversations it did not draft were held by "
            "`drafts-pre` or lost by the leg is not answerable from this file. The "
            "run's Studio journal answers it while it survives — gitignored, pruned "
            "at 30 days.")


def unaudited_runs(report_run_ids: list[str], audit_entries: list[dict],
                   skipped_entries: Iterable[dict] = ()) -> list[str]:
    """Run ids that produced a report but were never written to the audit log (STU-829).

    Nothing else links `reports/` back to `audits/audit_log.json`, so a skipped audit
    leaves the cross-run dashboard reading as complete while it is not.

    A run marked in `audits/skipped.json` counts as covered (STU-900). A smoke run of
    one email does not earn an audit, and a gate that demanded one would be worked
    around rather than obeyed. The marker excuses the run only when it carries a
    reason: an entry naming none is not a decision, it is the same silence in a
    second file.
    """
    audited = {e.get("run_id") for e in audit_entries}
    audited |= {e.get("run_id") for e in skipped_entries if (e.get("reason") or "").strip()}
    return [rid for rid in report_run_ids if rid not in audited]


AUDIT_ENTRY_SCHEMA: dict[str, tuple[type, ...]] = {
    "run_id": (str,),
    "date": (str,),
    "dump": (str,),
    "provider": (str,),
    "git_commit": (str, type(None)),
    "emails": (int,),
    "ratio_deterministic": (int, float),
    "catch_all_share": (int, float),
    "errors": (int,),
    "error_tags": (dict,),
    "policy_downgrades": (int,),
    "violations": (int,),
    "never_fires": (list,),
    "unexercised_by_this_run": (list,),
    "by_bucket": (dict,),
    "verdict": (str,),
    "new_issues": (list,),
}

NULLABLE_WITH_SCHEMA_NOTE = frozenset({
    "never_fires", "unexercised_by_this_run", "policy_downgrades",
})


# A filed entry says so in the field a human owes, and keeps saying it until one
# overwrites the field. Prefix and not equality: a reviewer who appends their reading
# below the sentinel instead of replacing it still reads as owing one, which is the
# direction an audit has to fail in.
UNREVIEWED = "UNREVIEWED"

UNREVIEWED_VERDICT = (
    f"{UNREVIEWED} — filed by the run itself (STU-1084), not by a person. Every field "
    "above is machine-computed off the report and the frontier log; nobody has read "
    "this run. Replace this text with a verdict once someone has."
)


def filed_entry(report: dict, run_metrics: dict, rules: dict, violations: int,
                schema_note: str, date: str) -> dict:
    """The audit entry a push run files for itself (STU-1084).

    Every field here is one `metrics` and `rule_stats` already compute — filing it is
    transcription, which is what six backfill issues (STU-841 through STU-1071) each
    did by hand at a rate inbound mail sets. What a person owes afterwards is a
    reading of entries that exist, and `verdict` is where that reading goes.

    The two staleness lists are null on purpose, with the `schema_note` the log
    already uses for them. `never_fires` drives rule removal: it is a claim about the
    *rule set*, identical on every run that pinned it, and copying the same two lists
    onto a hundred push entries would be the same transcription done by machine. The
    reviewer computes it once over the window with `audit_run.py rules`.
    """
    provenance = report.get("provenance") or {}
    return {
        "run_id": report.get("run_id"),
        # Passed in, never `today()`: `make audit-file` over a host that served runs
        # under an older build files a week of them at once, and a dashboard reading
        # dates would see one busy day and six empty ones.
        "date": date,
        # The marker the run recorded, never a guess: for a push run it is
        # `LIVE_INPUT`, which says the mail never touched disk (STU-859).
        "dump": provenance.get("input_path") or "unknown (report records no input)",
        "provider": provenance.get("provider") or "unknown",
        "git_commit": provenance.get("git_commit"),
        "emails": (report.get("totals") or {}).get("emails", 0),
        "ratio_deterministic": report.get("ratio_deterministic", 0.0),
        "catch_all_share": rules["catch_all_share"],
        "errors": (report.get("totals") or {}).get("errors", 0),
        "error_tags": run_metrics["error_tags"],
        "policy_downgrades": run_metrics["policy_downgrades"],
        "violations": violations,
        "never_fires": None,
        "unexercised_by_this_run": None,
        "by_bucket": report.get("by_bucket") or {},
        "schema_note": schema_note,
        "verdict": UNREVIEWED_VERDICT,
        "new_issues": [],
    }


def is_unreviewed(entry: dict) -> bool:
    """Whether this entry is still the one a run filed, with no reading behind it."""
    return (entry.get("verdict") or "").startswith(UNREVIEWED)


def unreviewed_runs(entries: Iterable[dict]) -> list[str]:
    """Run ids whose entry is filed but whose verdict nobody has written (STU-1084).

    `coverage` prints these and still exits 0. They are the debt filing creates: the
    gate stops holding the next run, so the count is the only thing left saying a
    person still owes a reading, and a filed entry that reported nothing would be the
    silence `audit-skip` refuses (STU-900).
    """
    return [e.get("run_id") for e in entries if is_unreviewed(e)]


# A verdict a person wrote about a whole window instead of one run. It is a reading, so
# `is_unreviewed` is false for it, and it must name a file that holds that reading: a
# verdict pointing at nothing would be the sentinel with its text swapped.
WINDOW_REVIEW = "Reviewed in window audit "


def check_window_verdicts(entries: list[dict], root: Path) -> list[str]:
    """Window-review verdicts whose audit file is missing or does not list the run."""
    errors: list[str] = []
    for entry in entries:
        verdict = entry.get("verdict") or ""
        if not verdict.startswith(WINDOW_REVIEW):
            continue
        rid = entry.get("run_id", "no run_id")
        named = verdict[len(WINDOW_REVIEW):].split(" ", 1)[0].rstrip(".;,")
        doc = root / named
        if not doc.is_file():
            errors.append(f"entry {rid}: window audit {named} does not exist")
        elif rid not in doc.read_text():
            errors.append(f"entry {rid}: window audit {named} does not list this run")
    return errors


def check_audit_entries(entries: list[dict]) -> list[str]:
    """Shape violations in `audits/audit_log.json` (STU-831).

    The log is hand-written and read by no code, so a renamed metric (STU-825's
    dead_rules split) drifts silently and the cross-run dashboard compares two
    different metrics as one. An unrecognised key is a violation, not a passthrough:
    that is what a rename looks like from here. A metric a run never computed may be
    null only alongside a `schema_note` saying so.
    """
    errors: list[str] = []
    for i, entry in enumerate(entries):
        where = f"entry {i} ({entry.get('run_id', 'no run_id')})"
        for key in AUDIT_ENTRY_SCHEMA.keys() - entry.keys():
            errors.append(f"{where}: missing key {key}")
        for key in entry.keys() - AUDIT_ENTRY_SCHEMA.keys() - {"schema_note"}:
            errors.append(f"{where}: unknown key {key}")
        for key, types in AUDIT_ENTRY_SCHEMA.items():
            if key not in entry:
                continue
            value = entry[key]
            if value is None and key in NULLABLE_WITH_SCHEMA_NOTE:
                if not entry.get("schema_note"):
                    errors.append(f"{where}: {key} is null without a schema_note")
                continue
            if isinstance(value, bool) or not isinstance(value, types):
                names = "|".join(t.__name__ for t in types)
                errors.append(f"{where}: {key} is {type(value).__name__}, expected {names}")
    return errors


# A schema_note claim, in the shape STU-899 settled on: a repo-relative file anchored
# to a commit sha, sitting next to the hash it asserts that file has there. The path
# must carry a slash and an extension, which is what separates the claim from the prose
# around it — "the rules/ tree at e4b70d48" and "a dirty tree ... at 04097b2" anchor a
# whole tree, assert no hash, and are left alone.
_PINNED_FILE = re.compile(
    r"(?P<path>[\w.-]+(?:/[\w.-]+)+\.[A-Za-z0-9]+) at (?P<sha>[0-9a-f]{7,40})\b"
)
_QUOTED_HASH = re.compile(r"\b[0-9a-f]{8,64}\b")


def _asserted_hash(note: str, pin: re.Match) -> str | None:
    """The hash a pin claims, read as the nearest hex token outside the pin itself.

    Both shapes the log uses put it adjacent: "pins rules_sha256 4b4c159f, byte-identical
    to rules/rules.yaml at a78d79d8" quotes it before, "rules/rules.yaml at cf1ca852
    (43d5eac1)" after. Nearest, not first: a note that also names the hash the run pinned
    in order to say the replay ran against a *different* rule set quotes two, and the one
    it asserts is the one it wrote next to the anchor.
    """
    start, end = pin.span()
    outside = [m for m in _QUOTED_HASH.finditer(note) if m.end() <= start or m.start() >= end]
    if not outside:
        return None
    return min(outside, key=lambda m: min(abs(m.start() - end), abs(start - m.end()))).group()


def check_schema_note_pins(entries: list[dict], resolve) -> list[str]:
    """Verify the sha a `schema_note` pins its claim to (STU-914).

    STU-899 pinned every note's claim to a commit instead of `HEAD` and wrote the
    convention into the skill, where it is enforced by whoever reads it. Two notes had
    already gone false under `HEAD` with nothing to catch it, so the convention is worth
    no more than the last reader's attention. Here the claim is re-derived: `resolve(sha,
    path)` returns the sha256 of that file as of that commit — `git show <sha>:<path>` —
    or None when the commit is not in this repo.

    An unreachable pin is a violation, not a skip: it is as unverifiable as no pin, and
    a note nobody can re-check is the state STU-899 was closing. Prose without a pin
    passes untouched — only the one claim shape is being schema'd.
    """
    errors: list[str] = []
    for i, entry in enumerate(entries):
        note = entry.get("schema_note")
        if not isinstance(note, str):
            continue
        where = f"entry {i} ({entry.get('run_id', 'no run_id')})"
        for pin in _PINNED_FILE.finditer(note):
            path, sha = pin["path"], pin["sha"]
            actual = resolve(sha, path)
            if actual is None:
                errors.append(f"{where}: schema_note pins {path} at {sha}, "
                              f"a commit not reachable in this repo")
                continue
            asserted = _asserted_hash(note, pin)
            if asserted is None or actual.startswith(asserted):
                continue
            errors.append(f"{where}: schema_note pins {path} at {sha}, which hashes to "
                          f"{actual[:12]}, but the note quotes {asserted}")
    return errors


def rule_stats(
    report: dict,
    rule_ids: list[str] | None,
    corpus_fired: set[str] | None,
) -> dict:
    """Per-rule fire counts, staleness split, catch-all share.

    A rule quiet on one run is not dead: a ten-email sample cannot exercise every sender.
    `corpus_fired` is the set of rules the fixture corpus fires, which separates a genuine
    removal candidate from a sample-size artifact (STU-825).

    Both are null when the rule set the run pinned could not be recovered (STU-913): the
    staleness split is a statement about *that* rule set, and computing it against a later
    one calls a rule added since the run dead. `fired` and `catch_all_share` are read off
    the verdicts, so they survive — the two lists go null, the same shape the audit log
    already uses for a run whose staleness nobody could compute.
    """
    fired: dict[str, int] = {}
    for v in report.get("verdicts", []):
        rid = v.get("rule_id") or "unknown"
        fired[rid] = fired.get(rid, 0) + 1
    total = len(report.get("verdicts", []))
    if rule_ids is None or corpus_fired is None:
        quiet_never = quiet_unexercised = None
    else:
        quiet = [r for r in rule_ids if r not in fired and r != CATCH_ALL]
        quiet_never = [r for r in quiet if r not in corpus_fired]
        quiet_unexercised = [r for r in quiet if r in corpus_fired]
    return {
        "fired": dict(sorted(fired.items(), key=lambda kv: -kv[1])),
        "never_fires": quiet_never,
        "unexercised_by_this_run": quiet_unexercised,
        "catch_all_share": (fired.get(CATCH_ALL, 0) / total if total else 0.0),
    }


# --- Anonymization recall (STU-1036) -----------------------------------------------

# The keys of a recorded payload that are identifiers rather than text: scanning them
# reports a leak on values no scope ever claimed to tokenize. `thread_id` is a Graph
# handle, `prompt_fingerprint` and `stage1_fingerprint` are hashes, `run_id` is ours,
# and `anonymize_fields` is the scope's own list of field names.
_NON_TEXT_KEYS = frozenset({
    "thread_id", "run_id", "prompt_fingerprint", "stage1_fingerprint",
    "anonymize_fields", "date", "provider_category",
})


def payload_text(payload: dict) -> str:
    """Every string a payload would put in front of the model, joined.

    Joined rather than walked field by field: a recall verdict is about the prompt as
    a whole, and a leak that only a nested value carries is still a leak.
    """
    def walk(value) -> Iterable[str]:
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, sub in value.items():
                if key not in _NON_TEXT_KEYS:
                    yield from walk(sub)
        elif isinstance(value, list):
            for sub in value:
                yield from walk(sub)

    return "\n".join(walk(payload))


def recall(payloads: list[dict], keymap: dict[str, str]) -> dict:
    """What survived tokenization in the text a run actually sent stage 2.

    Two independent readings of the same text, because each misses what the other
    catches. `_FORMAT_PATTERNS` finds a value no keymap ever knew — the miss that
    matters most, since nothing else in the run would record it. Searching the
    keymap's cleartext values finds the opposite failure: a value the run *did*
    tokenize somewhere and left in clear here, which no format pattern covers because
    a person's name has no format. Measured on a 26-message club corpus, the second
    reading is what caught `Ariane` in `Hello Ariane,` and `Marc` in `J'ai mis Marc
    dans la boucle`; the first caught neither.

    Neither reading is a count of minted entries, and that is the point: a run can
    legitimately mint zero — every value already carried in from an earlier round —
    and still owe a recall verdict (STU-1036).

    Two exclusions, both mechanical, both for values that are not leaks whatever the
    text says. A role address is let through by design (STU-902), so reporting one
    would fail every run at a gate meant to pass. A keymap value that is itself a
    token is a keymap defect (STU-981) and not text the run sent in clear; it is
    counted separately rather than dropped, since nothing else names it.

    What is left still needs reading, and is reported with its token and one excerpt
    so it can be: a keymap holds entries that name nobody — `architect`, a company
    name, `New Sign-In` — and those appear in clear precisely because they were never
    private. Telling them from a real name is the precision half of the same audit
    step, and no rule here can do it.

    The keymap reading matches the way the code it audits matches: case-sensitively and
    on whole words (STU-1227). It was a substring search, defended as over-reporting in
    the direction a human reads — but the thing it over-reported cannot be a leak.
    `_known_person_spans` has claimed a keymap value on whole words since STU-1046,
    precisely so `marc` the ordinary noun is not `Marc`, so a substring hit is text the
    tokenizer never had a chance to redact. Measured on run `1452f339` (the serving host,
    2026-09-01), one of its two hits was `sam` inside `samedi`.

    That is not caution but noise, and the cost is the one this audit cannot afford: a
    real leak sitting in a list the auditor has learned to wave through.

    The matcher is `_known_person_spans` itself rather than a second copy of its regex.
    Its name says person because production has one caller; what it *is* is the rule for
    where a known value is matched, which is the rule this reading exists to audit — and
    a second statement of it would answer differently the day one of them changed.

    `_FORMAT_PATTERNS` is untouched: an email or a phone number is a leak wherever it
    appears, and it has its own shape. The one exception is a digit run inside a
    `|`-separated code list, which is what `x-microsoft-antispam` carries: run
    `7491c521` reported `18002099003` from it as a phone (STU-1994).
    """
    from email_classifier.anonymize import (
        _FORMAT_PATTERNS,
        _TOKEN,
        _known_person_spans,
        is_machine_address,
    )

    blob = "\n".join(payload_text(p) for p in payloads)

    formats: dict[str, list[str]] = {}
    for category, pattern in _FORMAT_PATTERNS.items():
        hits = sorted({
            m.group(0) for m in pattern.finditer(blob)
            if not (category == "phone" and "|" in blob[m.start() - 1:m.start()]
                    + blob[m.end():m.end() + 1])
        })
        if category == "email":
            hits = [h for h in hits if not is_machine_address(h)]
        if hits:
            formats[category] = hits

    cleartext = []
    tokens_as_values = []
    for token, value in sorted(keymap.items()):
        if not value:
            continue
        hits = _known_person_spans(blob, [value])
        if not hits:
            continue
        if _TOKEN.match(value):
            tokens_as_values.append({"token": token, "value": value})
            continue
        if "@" in value and is_machine_address(value):
            continue
        cleartext.append({
            "token": token,
            "value": value,
            "occurrences": len(hits),
            "excerpt": _excerpt(blob, hits[0][0], len(value)),
        })

    return {
        "payloads": len(payloads),
        "characters_scanned": len(blob),
        "keymap_values": len(keymap),
        "format_hits": formats,
        "keymap_values_in_clear": cleartext,
        "keymap_values_that_are_tokens": tokens_as_values,
        "leaks": sum(len(v) for v in formats.values()) + len(cleartext),
    }


def _excerpt(blob: str, at: int, length: int, width: int = 40) -> str:
    """The occurrence in its surroundings — what makes a hit triageable.

    Given the offset rather than the value: the caller matched on whole words, and
    `blob.index` would walk back to the first *substring* occurrence — an excerpt around
    a position the hit was not at (STU-1227).
    """
    return blob[max(0, at - width):at + length + width].replace("\n", " ")



# The events of a Studio child log that carry text this side of the process assembled
# and handed the child. `pipeline_start.input` is the input file itself;
# `tool_call_start.params` is what a tool was called with. Deliberately not
# `tool_call_complete.result`: a tool's answer is redacted by the module that serves it
# (`precedents.py`, `club_memory.py`), which is a different gate from this one.
_SENT_EVENTS = {"pipeline_start": "input", "tool_call_start": "params"}


def studio_payloads(events: Iterable[dict]) -> list[dict]:
    """The outbound payloads a Studio child log recorded (STU-1036).

    The fallback for a run written before it kept its own record. Weaker evidence than
    that record, and for one reason worth stating: a classify child log carries no run
    id of ours, so attributing it to a run is done on what its input names, which
    cannot separate two runs that classified the same conversation.
    """
    return [payload for event in events
            if (key := _SENT_EVENTS.get(event.get("event"))) is not None
            and isinstance(payload := event.get(key), dict)]


def studio_thread_id(events: Iterable[dict]) -> str | None:
    """The conversation a classify child log was started on, or None."""
    for event in events:
        if event.get("event") == "pipeline_start":
            payload = event.get("input")
            if isinstance(payload, dict):
                thread_id = payload.get("thread_id")
                return thread_id if isinstance(thread_id, str) else None
    return None

