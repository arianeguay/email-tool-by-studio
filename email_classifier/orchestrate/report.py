"""Classification report (STU-392): aggregate both paths into a report.

Pure module — no I/O, no LLM. `build_report` does the aggregation; `report_to_dict`
renders the JSON-serializable shape written to disk; `render_human` renders the stdout
summary. The deterministic/LLM ratio is the economy argument — surface it prominently.

Two units live here, and confusing them overstates what a run cost (STU-989). Every
verdict is one message: `totals`, `by_bucket` and `composition` are per message, and
apply, undo and the journal need them that way. A stage-2 *call* is one conversation
(STU-944), so `totals["llm_calls"]` is the count a reader means by "what did this run
spend" — on `fixtures-threads/` 11 escalated messages are 6 calls. `ratio_deterministic`
stays per message.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class EmailVerdict:
    message_id: str         # stable per-message identity — unique within a report
    thread_id: str          # conversation-grouping context
    path: str               # "deterministic" | "llm"
    status: str             # "ok" | "error"
    bucket: str | None
    suggested_action: str | None
    confidence: str | None
    rule_id: str | None     # deterministic path, and catch_all for escalated ones
    subtype: str | None     # llm path only
    reasoning: str | None   # llm path only
    error: str | None       # error-taxonomy tag when status == "error"
    error_detail: str | None = None  # captured stage-2 output behind the tag (STU-820)
    # The message's own date, so a conversation can be reconciled by recency without
    # re-reading the corpus (STU-940). Empty on a report written before that field.
    date: str = ""


@dataclass
class Report:
    run_id: str
    totals: dict[str, int]
    ratio_deterministic: float
    by_bucket: dict[str, int]
    by_action: dict[str, int]
    verdicts: list[EmailVerdict]
    provenance: dict = field(default_factory=dict)
    composition: dict = field(default_factory=dict)
    # What the run's stage-2 calls spent, in the report because the Studio log that
    # used to be the only record went with the worktree that produced it (STU-1012).
    # Empty on a report written before that, which `audit_run.py cost` reads as
    # "ask the Studio log" — never as a free run.
    cost: dict = field(default_factory=dict)


def stage2_calls(verdicts: list[EmailVerdict]) -> int:
    """How many stage-2 calls the run made: one per escalated conversation (STU-944).

    Derived rather than plumbed, because it is exact either way: `collapse` groups the
    escalated messages by `thread_id`, so the distinct threads on the LLM path *are* the
    calls — including for a report a fan-out assembled, and for one written before this
    field existed. An errored verdict counts: the call was still made.
    """
    return len({v.thread_id for v in verdicts if v.path == "llm"})


def build_report(
    run_id: str,
    verdicts: list[EmailVerdict],
    provenance: dict | None = None,
    composition: dict | None = None,
    cost: dict | None = None,
) -> Report:
    total = len(verdicts)
    deterministic = sum(1 for v in verdicts if v.path == "deterministic")
    llm = sum(1 for v in verdicts if v.path == "llm")
    errors = sum(1 for v in verdicts if v.status == "error")

    by_bucket: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for v in verdicts:
        if v.status != "ok":
            continue
        if v.bucket is not None:
            by_bucket[v.bucket] = by_bucket.get(v.bucket, 0) + 1
        if v.suggested_action is not None:
            by_action[v.suggested_action] = by_action.get(v.suggested_action, 0) + 1

    return Report(
        run_id=run_id,
        totals={
            "emails": total,
            "deterministic": deterministic,
            "llm": llm,
            "llm_calls": stage2_calls(verdicts),
            "errors": errors,
        },
        ratio_deterministic=(deterministic / total if total else 0.0),
        by_bucket=by_bucket,
        by_action=by_action,
        verdicts=verdicts,
        provenance=provenance or {},
        composition=composition or {},
        cost=cost or {},
    )


def report_to_dict(report: Report) -> dict:
    return {
        "run_id": report.run_id,
        "totals": report.totals,
        "ratio_deterministic": report.ratio_deterministic,
        "by_bucket": report.by_bucket,
        "by_action": report.by_action,
        "verdicts": [asdict(v) for v in report.verdicts],
        "provenance": report.provenance,
        "composition": report.composition,
        "cost": report.cost,
    }


def _provenance_line(provenance: dict) -> str:
    if not provenance:
        return "Provenance: (unrecorded)"
    commit = provenance.get("git_commit") or "unknown"
    dirty = " +dirty" if provenance.get("git_dirty") else ""
    rules = provenance.get("rules_sha256") or "unknown"
    return (
        f"Provider: {provenance.get('provider') or 'unknown'} | "
        f"commit: {commit[:8]}{dirty} | rules sha256: {rules[:12]} | "
        f"input: {provenance.get('input_path') or 'unrecorded'} | "
        # Which store the ids below resolve in (STU-1183). Two mailboxes have been
        # served, and a report from either read identically until this line.
        f"mailbox: {provenance.get('mailbox') or 'unnamed'}"
    )


def _composition_line(composition: dict, emails: int) -> str:
    """The ratio's denominator: what rules can reach on this run (see STU-404)."""
    return (
        f"Addressable: {composition['addressable']}/{emails} "
        f"(unsubscribable {composition['unsubscribable']}, "
        f"recurring sender {composition['recurring']}) | "
        f"coverage {round(composition['ratio_addressable'] * 100)}%"
    )


def render_human(report: Report) -> str:
    t = report.totals
    det_pct = round(report.ratio_deterministic * 100)
    llm_pct = round(t["llm"] / t["emails"] * 100) if t["emails"] else 0
    calls = stage2_calls(report.verdicts)
    lines = [
        f"=== Classification Report (run {report.run_id}) ===",
        f"{t['emails']} messages | {t['deterministic']} deterministic ({det_pct}% "
        f"of messages) | {t['llm']} -> LLM ({llm_pct}%) | {t['errors']} errors",
        f"Stage 2 calls: {calls} (one per conversation) | "
        f"escalated messages: {t['llm']}",
        _provenance_line(report.provenance),
    ]
    if report.composition:
        lines.append(_composition_line(report.composition, t["emails"]))
    lines += [
        "By bucket:  " + "  ".join(f"{b}:{n}" for b, n in sorted(report.by_bucket.items())),
        "By action:  " + "  ".join(f"{a}:{n}" for a, n in sorted(report.by_action.items())),
        "Escalated (frontier):",
    ]
    escalated = [v for v in report.verdicts if v.path == "llm"]
    if not escalated:
        lines.append("  (none)")
    for v in escalated:
        if v.status == "ok":
            lines.append(f"  {v.message_id}   catch_all -> LLM -> {v.bucket} / {v.suggested_action}")
        else:
            lines.append(f"  {v.message_id}   catch_all -> LLM -> ERROR ({v.error})")
    return "\n".join(lines)
