"""Tests for the IMAP provider (STU-2054).

`FakeImap` is an in-process IMAP server behind `imaplib`'s client API: it answers
LIST, SELECT, UID SEARCH/FETCH/STORE/MOVE/EXPUNGE and APPEND in the shapes a real
server sends, so `ImapSession`'s parsing runs for real and no network is opened.
Its folder list and capabilities are the ones Migadu reported on 2026-11-03.
"""

import re
from datetime import datetime
from email.message import EmailMessage
import pytest

from email_classifier import deployment, providers
from email_classifier.apply.executor import apply_verdicts
from email_classifier.apply.imap.drafts import ImapDrafter
from email_classifier.apply.imap.writer import ImapWriter
from email_classifier.apply.journal import ApplyJournal, read_entries
from email_classifier.apply.undo import undo_entries
from email_classifier.core.contract import load_taxonomy
from email_classifier.fetch.imap import ImapFetcher, ImapSession, projection
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.stage1.policy import Policy

TAXONOMY = load_taxonomy(deployment.ROOT / "rules" / "taxonomy.yaml")

_FOLDERS = {
    "INBOX": "", "Archive": "\\Archive", "Drafts": "\\Drafts", "Sent": "\\Sent",
    "Trash": "\\Trash", "Junk": "\\Junk", "Factures": "",
}


def _mail(mid, sender="parent@example.com", subject="Question", body="Bonjour",
          refs=None, date="Mon, 02 Nov 2026 10:00:00 +0000", extra=None) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "moi@example.ca"
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = f"<{mid}>"
    if refs:
        message["In-Reply-To"] = f"<{refs[-1]}>"
        message["References"] = " ".join(f"<{r}>" for r in refs)
    for name, value in (extra or {}).items():
        message[name] = value
    message.set_content(body)
    return message.as_bytes()


class FakeImap:
    def __init__(self):
        self.folders: dict[str, dict[int, dict]] = {name: {} for name in _FOLDERS}
        self.next_uid = 1
        self.selected = None

    def add(self, folder, raw, flags=(), received="02-Nov-2026 10:00:00 +0000"):
        uid = self.next_uid
        self.next_uid += 1
        self.folders[folder][uid] = {"raw": raw, "flags": set(flags), "received": received}
        return uid

    def where(self, mid):
        for folder, messages in self.folders.items():
            for message in messages.values():
                if projection.message_id(projection.parse(message["raw"])) == mid:
                    return folder, message
        return None

    # --- imaplib's client surface ---

    def list(self):
        return "OK", [
            f'(\\HasNoChildren{" " + use if use else ""}) "/" "{name}"'.encode()
            for name, use in _FOLDERS.items()
        ]

    def select(self, mailbox, readonly=False):
        self.selected = mailbox.strip('"')
        return "OK", [str(len(self.folders[self.selected])).encode()]

    def append(self, mailbox, flags, date_time, message):
        uid = self.add(mailbox.strip('"'), message, re.findall(r"[^\s()]+", flags))
        return "OK", [f"[APPENDUID 1 {uid}] Append completed".encode()]

    def uid(self, command, *args):
        box = self.folders[self.selected]
        if command == "SEARCH":
            return "OK", [" ".join(str(u) for u in sorted(box) if self._match(box[u], list(args[1:]))).encode()]
        uids = [int(u) for u in args[0].split(",")]
        if command == "FETCH":
            data = []
            for u in uids:
                m = box[u]
                meta = (f'{u} (UID {u} FLAGS ({" ".join(sorted(m["flags"]))}) '
                        f'INTERNALDATE "{m["received"]}" BODY[] {{{len(m["raw"])}}}').encode()
                data += [(meta, m["raw"]), b")"]
            return "OK", data
        if command == "STORE":
            flags = set(args[2].strip("()").split())
            for u in uids:
                if args[1].startswith("+"):
                    box[u]["flags"] |= flags
                else:
                    box[u]["flags"] -= flags
            return "OK", [None]
        if command == "MOVE":
            for u in uids:
                self.folders[args[1].strip('"')][self.next_uid] = box.pop(u)
                self.next_uid += 1
            return "OK", [None]
        if command == "EXPUNGE":
            for u in uids:
                if "\\Deleted" in box[u]["flags"]:
                    del box[u]
            return "OK", [None]
        raise AssertionError(f"unexpected UID {command}")

    def _match(self, message, criteria):
        def take():
            key = criteria.pop(0)
            if key == "ALL":
                return True
            if key == "OR":
                left, right = take(), take()
                return left or right
            if key == "HEADER":
                name, value = criteria.pop(0), criteria.pop(0).strip('"')
                if name == "References" and value.startswith("<"):
                    return False  # what Migadu answers (2026-11-03)
                parsed = projection.parse(message["raw"])
                return value in str(parsed.get(name, ""))
            if key == "TEXT":
                return criteria.pop(0).strip('"').encode() in message["raw"]
            if key == "SINCE":
                criteria.pop(0)
                return True
            raise AssertionError(f"unexpected criterion {key}")

        result = True
        while criteria:
            result = take() and result
        return result


@pytest.fixture
def imap():
    return FakeImap()


def _session(imap):
    return ImapSession(imap)


def test_a_reply_threads_on_its_root_and_reads_without_the_quoted_parent():
    raw = _mail("m2", refs=["root", "m1"], body="Merci!\n\nLe lun. 2 nov., Ariane a écrit :\n> Bonjour")
    message = projection.parse(raw)

    item = projection.message_to_input(message, datetime(2026, 11, 2, 10))

    assert item["message_id"] == "m2"
    assert item["thread_id"] == "root"
    assert item["body_snippet"] == "Merci!"


@pytest.mark.parametrize("quoted", [
    "Le lun. 2 nov. 2026, à 10 h 00, Marc Tremblay <marc@example.com> a\nécrit :\n> Bonjour",
    "On Mon, Nov 2, 2026 at 10:00 AM Marc <marc@example.com> wrote:\n> Hi",
    "De : Marc\nEnvoyé : 2 novembre 2026\nObjet : Question\nBonjour",
])
def test_the_quoted_parent_is_cut_whatever_client_wrote_it(quoted):
    message = projection.parse(_mail("m2", body=f"Parfait, merci.\n\n{quoted}"))

    assert projection.message_body(message) == "Parfait, merci."


def test_only_allowlisted_headers_reach_the_input():
    message = projection.parse(_mail("m1", extra={"List-Unsubscribe": "<mailto:x@y>", "X-Secret": "no"}))

    item = projection.message_to_input(message, datetime(2026, 11, 2))

    assert item["headers"] == {"List-Unsubscribe": "<mailto:x@y>"}


def test_special_use_folders_are_found_by_attribute(imap):
    session = _session(imap)

    assert session.special("Archive") == "Archive"
    assert session.special("Drafts") == "Drafts"
    with pytest.raises(LookupError):
        session.special("All")


def test_fetch_reads_inbox_and_archive_newest_first_and_never_sent_or_drafts(imap):
    imap.add("INBOX", _mail("old"), received="01-Nov-2026 09:00:00 +0000")
    imap.add("Archive", _mail("new"), received="02-Nov-2026 09:00:00 +0000")
    imap.add("INBOX", _mail("draft"), flags=["\\Draft"])
    imap.add("Sent", _mail("mine"))
    imap.add("Factures", _mail("sieve"))

    inputs = ImapFetcher(_session(imap)).fetch_inputs("", 10)

    assert [i["message_id"] for i in inputs] == ["new", "old"]


def test_fetch_caps_at_max_results(imap):
    for n in range(5):
        imap.add("INBOX", _mail(f"m{n}"), received=f"0{n + 1}-Nov-2026 09:00:00 +0000")

    inputs = ImapFetcher(_session(imap)).fetch_inputs("", 2)

    assert [i["message_id"] for i in inputs] == ["m4", "m3"]


def test_fetching_never_marks_a_message_seen(imap):
    imap.add("INBOX", _mail("m1"))

    ImapFetcher(_session(imap)).fetch_inputs("", 10)

    assert imap.where("m1")[1]["flags"] == set()


def test_a_conversation_keeps_our_own_trash_and_drops_a_humans(imap):
    imap.add("INBOX", _mail("root"))
    imap.add("Archive", _mail("m2", refs=["root"]))
    imap.add("Trash", _mail("m3", refs=["root"]), flags=["ec/deleted"])
    imap.add("Trash", _mail("m4", refs=["root"]))
    imap.add("Sent", _mail("m5", refs=["root"]))

    ids = ImapFetcher(_session(imap)).conversation_message_ids("root")

    assert sorted(ids) == ["m2", "m3", "root"]


def test_a_conversation_never_takes_a_message_whose_id_merely_contains_the_root(imap):
    imap.add("INBOX", _mail("root@x"))
    imap.add("INBOX", _mail("other-root@x"))

    assert ImapFetcher(_session(imap)).conversation_message_ids("root@x") == ["root@x"]


def test_thread_history_marks_our_sent_turns_and_skips_the_answered_mail(imap):
    imap.add("Archive", _mail("root", body="Question 1"), received="01-Nov-2026 09:00:00 +0000")
    imap.add("Sent", _mail("r1", sender="moi@example.ca", refs=["root"], body="Réponse"),
             received="01-Nov-2026 10:00:00 +0000")
    imap.add("INBOX", _mail("m3", refs=["root", "r1"], body="Question 2"),
             received="02-Nov-2026 09:00:00 +0000")
    fetcher = ImapFetcher(_session(imap))

    turns = fetcher.fetch_thread_history("root", exclude_message_id="m3")

    assert [(t["direction"], t["body"]) for t in turns] == [
        ("received", "Question 1"), ("sent", "Réponse"),
    ]
    assert fetcher.last_sent_at("root") == "2026-11-01T10:00:00+00:00"
    assert fetcher.sent_replies(["root"])["root"]["body"] == "Réponse"


def test_precedent_search_covers_sent_but_never_trash(imap):
    imap.add("Sent", _mail("s1", body="Le cours est annulé"))
    imap.add("Trash", _mail("t1", body="Le cours est annulé"))

    hits = ImapFetcher(_session(imap)).search_messages("cours", 10)

    assert [h["body"] for h in hits] == ["Le cours est annulé"]


def test_label_ids_read_the_folder_on_the_label_axis(imap):
    imap.add("INBOX", _mail("a"), flags=["\\Seen", "ec/C"])
    imap.add("Archive", _mail("b"))
    imap.add("Trash", _mail("c"))
    imap.add("Factures", _mail("d"))
    writer = ImapWriter(_session(imap))

    assert writer.label_ids("a") == ["ec/C", "INBOX"]
    assert writer.label_ids("b") == []
    assert writer.label_ids("c") == ["TRASH"]
    assert writer.label_ids("d") == ["ELSEWHERE"]


def _verdict(action="label_archive", bucket="C"):
    return EmailVerdict(
        message_id="m1", thread_id="m1", path="deterministic", status="ok",
        bucket=bucket, suggested_action=action, confidence="high", rule_id="r1",
        subtype=None, reasoning=None, error=None, date="2026-11-02T10:00:00+00:00",
    )


def _policy():
    return Policy(
        deterministic_can_finalize=True, destructive_actions_allowed=["mark_for_delete"],
        frontier_log_file=False, frontier_log_stdout=False, email_provider="imap",
    )


def test_archive_then_undo_round_trips_the_mailbox(imap, tmp_path):
    imap.add("INBOX", _mail("m1"))
    writer = ImapWriter(_session(imap))
    journal = ApplyJournal(tmp_path / "run.jsonl", "run")

    apply_verdicts([_verdict()], writer, _policy(), journal, TAXONOMY, dry_run=False)
    folder, message = imap.where("m1")
    assert (folder, message["flags"]) == ("Archive", {"ec/C", "ec/processed"})

    undo_entries(read_entries(journal.path), writer, ApplyJournal(tmp_path / "u.jsonl", "run"))
    folder, message = imap.where("m1")
    assert (folder, message["flags"]) == ("INBOX", set())


def test_trash_then_undo_round_trips_the_mailbox(imap, tmp_path):
    imap.add("INBOX", _mail("m1"))
    writer = ImapWriter(_session(imap))
    journal = ApplyJournal(tmp_path / "run.jsonl", "run")

    apply_verdicts([_verdict("mark_for_delete", "D")], writer, _policy(), journal, TAXONOMY, dry_run=False)
    assert imap.where("m1")[0] == "Trash"
    assert "ec/deleted" in imap.where("m1")[1]["flags"]

    undo_entries(read_entries(journal.path), writer, ApplyJournal(tmp_path / "u.jsonl", "run"))
    folder, message = imap.where("m1")
    assert (folder, message["flags"]) == ("INBOX", set())


def test_a_reply_draft_threads_under_its_parent_and_never_sends(imap, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "moi@example.ca")
    imap.add("INBOX", _mail("m2", refs=["root"], extra={"Reply-To": "papa@example.com"}))
    drafter = ImapDrafter(_session(imap))

    result = drafter.upsert_reply_draft("m2", "root", "Bonjour, oui.")

    folder, stored = imap.where(result["draft_id"])
    draft = projection.parse(stored["raw"])
    assert folder == "Drafts" and "\\Draft" in stored["flags"]
    assert draft["To"] == "papa@example.com"
    assert draft["Subject"] == "Re: Question"
    assert draft["In-Reply-To"] == "<m2>"
    assert draft["References"] == "<root> <m2>"
    assert result["updated"] is False
    assert drafter.find_draft("root") == result["draft_id"]


def test_a_second_upsert_replaces_the_draft_instead_of_adding_one(imap, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "moi@example.ca")
    imap.add("INBOX", _mail("m1"))
    drafter = ImapDrafter(_session(imap))

    first = drafter.upsert_reply_draft("m1", "m1", "v1")
    second = drafter.upsert_reply_draft("m1", "m1", "v2", reply_to="x@example.com", reply_name="X")

    assert second["updated"] is True
    assert len(imap.folders["Drafts"]) == 1
    assert drafter.get_draft(first["draft_id"]) is None
    assert drafter.get_draft(second["draft_id"])["isDraft"] is True
    assert projection.parse(imap.where(second["draft_id"])[1]["raw"])["To"] == "X <x@example.com>"


def test_deleting_a_draft_expunges_only_that_draft(imap, monkeypatch):
    monkeypatch.setenv("IMAP_USER", "moi@example.ca")
    imap.add("INBOX", _mail("m1"))
    imap.add("Drafts", _mail("human-draft"), flags=["\\Draft", "\\Deleted"])
    drafter = ImapDrafter(_session(imap))
    draft_id = drafter.upsert_reply_draft("m1", "m1", "v1")["draft_id"]

    drafter.delete_draft(draft_id)

    assert imap.where(draft_id) is None
    assert imap.where("human-draft") is not None


def test_imap_is_a_drafting_provider_and_needs_its_credentials(monkeypatch):
    for key in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    assert providers.can_draft("imap")
    with pytest.raises(RuntimeError, match="IMAP_HOST, IMAP_USER, IMAP_PASSWORD"):
        providers.get_fetcher("imap")


def test_the_configured_mailbox_is_the_imap_login(monkeypatch):
    monkeypatch.setenv("IMAP_USER", "moi@example.ca")

    assert providers.configured_mailbox("imap") == "moi@example.ca"


def test_sent_since_reads_the_sent_folder_only_and_skips_drafts(imap):
    imap.add("Sent", _mail("r1", sender="moi@example.ca", body="Voici les dates."),
             received="18-Sep-2026 15:00:00 +0000")
    imap.add("Sent", _mail("r2", sender="moi@example.ca", body="Brouillon"), flags=["\\Draft"])
    imap.add("INBOX", _mail("m1", body="Question"))

    replies = ImapFetcher(_session(imap)).sent_since("2026-09-18T00:00:00+00:00")

    assert replies == [{"date": "2026-09-18T15:00:00+00:00", "body": "Voici les dates."}]
