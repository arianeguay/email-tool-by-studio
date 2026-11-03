"""A thin IMAP session over `imaplib` (STU-2054).

The only module that speaks the wire protocol. Folders are named by their
SPECIAL-USE attribute (RFC 6154) rather than by a configured name, so a server
that spells its archive `Archives` or `INBOX.Archive` needs no setting. Every
read uses `BODY.PEEK[]`, so fetching never marks a message `\\Seen`.

UIDs are per folder and change when a message moves, which is why nothing above
this module holds one across calls: the fetcher addresses a message by its
`Message-ID` and asks this session where it sits now.
"""

import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

INBOX = "INBOX"

_LIST_LINE = re.compile(rb'\((?P<attrs>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>"(?:\\.|[^"])*"|\S+)')
_FETCH_META = re.compile(rb"UID (?P<uid>\d+)|FLAGS \((?P<flags>[^)]*)\)|INTERNALDATE \"(?P<date>[^\"]+)\"")
_APPENDUID = re.compile(rb"APPENDUID \d+ (\d+)")


@dataclass(frozen=True)
class RawMessage:
    uid: str
    flags: frozenset[str]
    received: datetime
    raw: bytes


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(name: bytes) -> str:
    text = name.decode()
    if text.startswith('"'):
        text = re.sub(r'\\(.)', r"\1", text[1:-1])
    return text


def _check(typ: str, data) -> list:
    if typ != "OK":
        raise imaplib.IMAP4.error(f"IMAP command failed: {typ} {data!r}")
    return data


class ImapSession:
    def __init__(self, connection):
        self._conn = connection
        self._selected: str | None = None
        self._special: dict[str, str] | None = None

    def special(self, use: str) -> str:
        """The folder carrying `\\<use>` (`Archive`, `Sent`, `Drafts`, `Trash`, `Junk`)."""
        if self._special is None:
            self._special = {}
            for line in _check(*self._conn.list()):
                match = _LIST_LINE.match(line or b"")
                if not match:
                    continue
                for attr in match["attrs"].split():
                    self._special.setdefault(attr.decode().lstrip("\\"), _unquote(match["name"]))
        try:
            return self._special[use]
        except KeyError:
            raise LookupError(f"the IMAP server marks no folder \\{use}") from None

    def _select(self, folder: str) -> None:
        if self._selected != folder:
            _check(*self._conn.select(_quote(folder)))
            self._selected = folder

    def search(self, folder: str, *criteria: str) -> list[str]:
        self._select(folder)
        data = _check(*self._conn.uid("SEARCH", None, *criteria))
        return [uid.decode() for uid in (data[0] or b"").split()]

    def fetch(self, folder: str, uids: list[str]) -> list[RawMessage]:
        if not uids:
            return []
        self._select(folder)
        data = _check(*self._conn.uid(
            "FETCH", ",".join(uids), "(UID FLAGS INTERNALDATE BODY.PEEK[])"
        ))
        messages = []
        for part in data:
            if not isinstance(part, tuple):
                continue
            meta, raw = part
            fields: dict[str, bytes] = {}
            for match in _FETCH_META.finditer(meta):
                fields.update({k: v for k, v in match.groupdict().items() if v is not None})
            received = datetime(*imaplib.Internaldate2tuple(
                b'INTERNALDATE "' + fields["date"] + b'"'
            )[:6]).astimezone(timezone.utc)
            messages.append(RawMessage(
                uid=fields["uid"].decode(),
                flags=frozenset(fields.get("flags", b"").decode().split()),
                received=received,
                raw=raw,
            ))
        return messages

    def move(self, folder: str, uid: str, destination: str) -> None:
        self._select(folder)
        _check(*self._conn.uid("MOVE", uid, _quote(destination)))

    def add_flags(self, folder: str, uid: str, *flags: str) -> None:
        self._select(folder)
        _check(*self._conn.uid("STORE", uid, "+FLAGS.SILENT", f"({' '.join(flags)})"))

    def remove_flags(self, folder: str, uid: str, *flags: str) -> None:
        self._select(folder)
        _check(*self._conn.uid("STORE", uid, "-FLAGS.SILENT", f"({' '.join(flags)})"))

    def append(self, folder: str, flags: str, raw: bytes) -> str | None:
        """Store `raw` in `folder`; the new UID when the server reports one (UIDPLUS)."""
        typ, data = self._conn.append(_quote(folder), flags, None, raw)
        _check(typ, data)
        match = _APPENDUID.search(data[0] or b"")
        return match.group(1).decode() if match else None

    def delete(self, folder: str, uid: str) -> None:
        """Expunge one message by UID, never the folder's other `\\Deleted` mail."""
        self.add_flags(folder, uid, "\\Deleted")
        _check(*self._conn.uid("EXPUNGE", uid))


def configured_mailbox() -> str | None:
    return os.environ.get("IMAP_USER") or None


def build_session() -> ImapSession:
    """Log in with `IMAP_HOST`, `IMAP_USER` and `IMAP_PASSWORD` over implicit TLS."""
    missing = [k for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise RuntimeError(f"IMAP provider needs {', '.join(missing)} in the environment")
    connection = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], int(os.environ.get("IMAP_PORT", "993")))
    connection.login(os.environ["IMAP_USER"], os.environ["IMAP_PASSWORD"])
    return ImapSession(connection)
