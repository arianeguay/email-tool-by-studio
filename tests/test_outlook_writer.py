"""Tests for the Outlook write primitives (STU-847).

A fake Graph session throughout — no msal/requests, no mailbox. The fake serves
no DELETE verb, so a code path reaching for permanent deletion would crash.
"""

from pathlib import Path

import pytest

from email_classifier.apply.executor import apply_verdicts
from email_classifier.apply.journal import ApplyJournal, read_entries
from email_classifier.apply.outlook import OutlookWriter
from email_classifier.apply.undo import undo_entries
from email_classifier.core.contract import load_taxonomy
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.stage1.policy import Policy
from email_classifier import deployment

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")


class _Session:
    """Fake GraphSession backed by an in-memory mailbox."""

    def __init__(self, categories=None, folder="inbox"):
        self.messages = {"m1": {"categories": list(categories or []), "folder": folder}}
        self.calls = []

    def get(self, url, params=None):
        self.calls.append(("GET", url, params))
        if "/mailFolders(" in url:
            return {"id": url.split("'")[1]}
        # The mailbox stores the folder by name; Graph reports it as `parentFolderId`.
        message = self.messages[url.rsplit("/", 1)[-1]]
        return {**message, "parentFolderId": message["folder"]}

    def request(self, method, url, params=None, payload=None):
        self.calls.append((method, url, payload))
        if url.endswith("/move"):
            self.messages[url.split("/")[3]]["folder"] = payload["destinationId"]
            return {}
        self.messages[url.rsplit("/", 1)[-1]]["categories"] = payload["categories"]
        return {}


def _writer(**kwargs):
    session = _Session(**kwargs)
    return OutlookWriter(session), session


def test_resolving_a_label_calls_graph_at_all_never():
    """A category needs no registration, so neither call may touch the mailbox."""
    writer, session = _writer()

    assert writer.find_label("email-tool/processed") == "email-tool/processed"
    assert writer.ensure_label("email-tool/newsletter") == "email-tool/newsletter"
    assert session.calls == []


def test_add_label_appends_to_the_existing_categories():
    writer, session = _writer(categories=["keepme"])

    writer.add_label("m1", "email-tool/newsletter")

    assert session.messages["m1"]["categories"] == ["keepme", "email-tool/newsletter"]


def test_add_label_is_idempotent_and_issues_no_write():
    writer, session = _writer(categories=["email-tool/newsletter"])

    writer.add_label("m1", "email-tool/newsletter")

    assert [c for c in session.calls if c[0] != "GET"] == []


def test_remove_label_drops_only_that_category():
    writer, session = _writer(categories=["keepme", "email-tool/newsletter"])

    writer.remove_label("m1", "email-tool/newsletter")

    assert session.messages["m1"]["categories"] == ["keepme"]


def test_remove_inbox_is_a_move_to_the_archive_folder():
    writer, session = _writer()

    writer.remove_label("m1", "INBOX")

    assert session.messages["m1"]["folder"] == "archive"
    assert session.messages["m1"]["categories"] == []


def test_add_inbox_moves_the_message_back():
    writer, session = _writer()
    writer.remove_label("m1", "INBOX")

    writer.add_label("m1", "INBOX")

    assert session.messages["m1"]["folder"] == "inbox"


def test_trash_moves_to_deleted_items_and_untrash_restores_the_inbox():
    writer, session = _writer()

    writer.trash("m1")
    assert session.messages["m1"]["folder"] == "deleteditems"

    writer.untrash("m1")
    assert session.messages["m1"]["folder"] == "inbox"


def test_no_call_ever_uses_the_delete_verb():
    writer, session = _writer()

    writer.trash("m1")

    assert [c for c in session.calls if c[0] == "DELETE"] == []


def test_label_ids_reports_the_inbox_alongside_the_categories():
    """The executor diffs label and location on one axis (STU-963), and Gmail's
    `label_ids` has always answered INBOX the same way."""
    writer, session = _writer(categories=["email-tool/processed"])

    assert writer.label_ids("m1") == ["email-tool/processed", "INBOX"]

    session.messages["m1"]["folder"] = "archive"
    assert writer.label_ids("m1") == ["email-tool/processed"]


def test_a_category_rewrite_never_folds_the_inbox_into_the_categories():
    writer, session = _writer(categories=["keepme"])

    writer.add_label("m1", "email-tool/C")

    assert session.messages["m1"]["categories"] == ["keepme", "email-tool/C"]


def _policy():
    return Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=["mark_for_delete"],
        frontier_log_file=False,
        frontier_log_stdout=False,
        email_provider="outlook",
    )


def _verdict(action="label_archive", bucket="C"):
    return EmailVerdict(
        message_id="m1",
        thread_id="t1",
        path="deterministic",
        status="ok",
        bucket=bucket,
        suggested_action=action,
        confidence="high",
        rule_id="r1",
        subtype=None,
        reasoning=None,
        error=None,
        date="2026-08-18T12:00:00+00:00",
    )


def test_archive_then_undo_round_trips_the_mailbox(tmp_path):
    writer, session = _writer()
    before = {"categories": [], "folder": "inbox"}
    journal = ApplyJournal(tmp_path / "run.jsonl", "run")

    apply_verdicts([_verdict()], writer, _policy(), journal, TAXONOMY, dry_run=False)
    assert session.messages["m1"] == {
        "categories": ["email-tool/C", "email-tool/processed"],
        "folder": "archive",
    }

    undo_entries(read_entries(journal.path), writer, ApplyJournal(tmp_path / "u.jsonl", "run"))
    assert session.messages["m1"] == before


def test_trash_then_undo_round_trips_the_mailbox(tmp_path):
    writer, session = _writer()
    journal = ApplyJournal(tmp_path / "run.jsonl", "run")

    apply_verdicts([_verdict("mark_for_delete", "D")], writer, _policy(), journal, TAXONOMY, dry_run=False)
    assert session.messages["m1"]["folder"] == "deleteditems"

    undo_entries(read_entries(journal.path), writer, ApplyJournal(tmp_path / "u.jsonl", "run"))
    assert session.messages["m1"] == {"categories": [], "folder": "inbox"}


def test_dry_run_mutates_nothing(tmp_path):
    writer, session = _writer()

    apply_verdicts(
        [_verdict()], writer, _policy(), ApplyJournal(tmp_path / "plan.jsonl", "run"), TAXONOMY
    )

    assert [c for c in session.calls if c[0] != "GET"] == []


@pytest.mark.parametrize(
    "action,bucket,categories,folder",
    [
        # The state each action leaves behind, folder included: an archived message
        # carries its bucket, a trashed one carries `email-tool/deleted` (STU-983).
        ("label_archive", "C", ["email-tool/C", "email-tool/processed"], "archive"),
        ("mark_for_delete", "D", ["email-tool/processed", "email-tool/deleted"], "deleteditems"),
    ],
)
def test_a_reapplied_run_skips_already_processed_messages(
    tmp_path, action, bucket, categories, folder
):
    writer, session = _writer(categories=categories, folder=folder)
    journal = ApplyJournal(tmp_path / "run.jsonl", "run")

    outcomes = apply_verdicts(
        [_verdict(action, bucket)], writer, _policy(), journal, TAXONOMY, dry_run=False
    )

    assert outcomes[0].status == "skipped_processed"
    assert session.messages["m1"]["folder"] == folder


# The Deletions dumpster a hard delete leaves a message in: it still resolves on
# Graph, and its `parentFolderId` matches none of the folders this app files to
# (STU-1075). The fake resolves a well-known folder to its own name, so any name
# the writer does not ask for is a folder outside the table.
_DUMPSTER = "recoverableitemsdeletions"


def test_label_ids_reports_elsewhere_for_a_folder_we_never_file_to():
    """The folder axis has three answers. Categories alone used to mean both
    "archived" and "gone", and the executor read the second as the first."""
    writer, session = _writer(categories=["email-tool/C", "email-tool/processed"], folder=_DUMPSTER)

    assert writer.label_ids("m1") == ["email-tool/C", "email-tool/processed", "ELSEWHERE"]

    # The Archive is in the table precisely so it keeps answering the other way.
    session.messages["m1"]["folder"] = "archive"
    assert writer.label_ids("m1") == ["email-tool/C", "email-tool/processed"]


def test_a_message_the_mailbox_no_longer_holds_is_not_applied_to(tmp_path):
    """`make apply RUN_ID=<an older run>` is an ordinary rollback, and every report
    older than a teardown names messages in this state. Nothing about them is ours
    to move — and the marker must not be litter in the dumpster either."""
    writer, session = _writer(folder=_DUMPSTER)
    journal = ApplyJournal(tmp_path / "run.jsonl", "run")

    outcomes = apply_verdicts([_verdict()], writer, _policy(), journal, TAXONOMY, dry_run=False)

    assert [(o.status, o.operation) for o in outcomes] == [("skipped_elsewhere", None)]
    assert [c for c in session.calls if c[0] != "GET"] == []
    assert session.messages["m1"] == {"categories": [], "folder": _DUMPSTER}
    assert not journal.path.exists()  # nothing happened, so there is nothing to undo
