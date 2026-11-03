"""Which sent reply a draft is compared with (STU-1998).

The newest send on a conversation is not the reply to our draft: a conversation the
club answered several times holds a send for each later question, and one answered
before the draft was written holds a send that predates it. The reply sent in the
draft's place is the first send after the draft reached the mailbox, and the drafts
journal is where that instant is recorded, by the code that wrote it. No send after it
means nothing was sent in its place, so the draft stays unjoined.

Pure: the readers list the sends, this picks one.
"""

from datetime import datetime


def _instant(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def write_times(entries: list[dict], run_id: str) -> dict[str, str]:
    """When `run_id` last wrote each conversation's draft, from its `ok` journal rows.

    The last write and not the first: a group revision rewrites the draft in place, and
    the body being compared is the last attempt's.
    """
    written: dict[str, str] = {}
    for entry in entries:
        if entry.get("run_id") != run_id or entry.get("status") != "ok":
            continue
        thread_id, at = entry.get("thread_id"), entry.get("timestamp")
        if thread_id and at and (thread_id not in written
                                 or _instant(at) > _instant(written[thread_id])):
            written[thread_id] = at
    return written


def first_send_after(sends, written_at: str, sent_at):
    """The earliest of `sends` strictly after `written_at`, or None.

    `sent_at` maps a send to its ISO 8601 instant, so each reader keeps its own shape.
    """
    after = [s for s in sends if _instant(sent_at(s)) > _instant(written_at)]
    return min(after, key=lambda s: _instant(sent_at(s)), default=None)
