"""Gmail client: OAuth2 + read (list/get) loop (STU-391, scope widened by STU-405).

The fetch surface stays read-only: the only API calls here are messages().list(),
messages().get(), threads().get() and labels().list(). Write operations live in
`email_classifier.apply.gmail`, which reuses these credentials.

Google libraries are imported lazily (inside the OAuth/service functions) so the
pagination logic and its tests run without the optional `[fetch]` deps installed.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from email_classifier.fetch import headers
from email_classifier.fetch.gmail import projection
from email_classifier.fetch.grant import GrantLost
from email_classifier.providers import trashed_by_us

# gmail.modify is the minimal scope for labels + trash (STU-405). It does NOT
# allow permanent deletion (messages.delete) or sending — never widen further.
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

# Headers requested from the API. format='metadata' returns only these, so the
# privacy allowlist is enforced at the request boundary. From/Subject are
# consumed into their own fields by the projection, not kept in headers.
_METADATA_HEADERS = ["From", "Subject", *headers.HEADER_ALLOWLIST]

# Gmail caps a single messages.list page at 500.
_PAGE_SIZE = 500

# Labels that keep a message out of what a run classifies (STU-963). SENT is in the
# list for a different reason than the others: our own reply is not mail to classify.
_NOT_INCOMING = frozenset({"TRASH", "SPAM", "DRAFT", "SENT"})
# The half of `_NOT_INCOMING` the listing would otherwise spend `max_results` on
# (STU-2097): measured on a test mailbox, 25 slots went to drafts and sent mail and the
# older inbox was never listed. Spam and trash are already out by Gmail's default.
_LISTING_EXCLUDES = "-in:sent -in:drafts"

CONSENT_COMMAND = "make gmail-consent"

_DEFAULT_CREDENTIALS = Path("secrets/credentials.json")
_DEFAULT_TOKEN = Path("secrets/token.json")


def _resolve(explicit: Path | None, env_var: str, default: Path) -> Path:
    if explicit is not None:
        return explicit
    from_env = os.environ.get(env_var)
    return Path(from_env) if from_env else default


def _has_required_scopes(token_info: dict) -> bool:
    return set(SCOPES) <= set(token_info.get("scopes", []))


def get_credentials(
    credentials_file: Path | None = None,
    token_file: Path | None = None,
    interactive: bool = True,
):
    """Load a cached token, refresh it, or run the InstalledApp consent flow.

    The refresh token is cached to `token_file` so later runs are non-interactive.
    A cached token granted narrower scopes than SCOPES (e.g. a gmail.readonly
    token from before STU-405) is discarded so the consent flow runs again —
    refreshing it would silently yield write calls that 403 at runtime.

    `interactive=False` is for a caller with no human at a browser — a service,
    a cron job. `run_local_server` there binds a port and blocks forever on a
    redirect nobody will produce, which reads as a healthy idle process, so what
    would open a browser raises `GrantLost` instead (STU-948).
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = _resolve(credentials_file, "GMAIL_CREDENTIALS_FILE", _DEFAULT_CREDENTIALS)
    token_path = _resolve(token_file, "GMAIL_TOKEN_FILE", _DEFAULT_TOKEN)

    creds = None
    why = "no cached token"
    if token_path.exists():
        token_info = json.loads(token_path.read_text(encoding="utf-8"))
        if _has_required_scopes(token_info):
            creds = Credentials.from_authorized_user_info(token_info, SCOPES)
        else:
            why = f"the cached token is short of {' '.join(SCOPES)}"

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Refresh token expired or revoked (invalid_grant).
            creds = None
            why = f"the refresh token no longer redeems ({exc})"

    if creds is None or not creds.valid:
        if not interactive:
            raise GrantLost(
                f"the cached Gmail grant is unusable ({why}) and this process has "
                f"no browser to consent in. Run `{CONSENT_COMMAND}` on the host."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_service(creds):
    """Build the Gmail API client from credentials, guarded against a dead grant."""
    from googleapiclient.discovery import build

    return GrantAware(build("gmail", "v1", credentials=creds))


def _is_grant_refusal(exc) -> bool:
    """Two shapes, because google-auth redeems the refresh token inside the call.

    A 401 that reached Gmail is the grant, not this call — the status is read off
    the exception rather than importing googleapiclient, which this module
    deliberately does not depend on. A revoked refresh token never gets that far:
    it raises `RefreshError` out of `execute()` itself. A 403 is a scope.
    """
    if getattr(getattr(exc, "resp", None), "status", None) == 401:
        return True
    from google.auth.exceptions import RefreshError

    return isinstance(exc, RefreshError)


# googleapiclient backs off and retries a 429, a 5xx and a 403 `rateLimitExceeded`
# on its own when asked; a thread expansion of 200 conversations trips the per-minute
# quota and otherwise dies having written nothing (STU-2098). Never a 401.
_RETRIES = 5


def _guard(execute):
    """Wrap one `execute()` so a refused grant surfaces as `GrantLost` (STU-953).

    Neither shape is retryable — only a browser restores the grant — so both
    have to leave the drain's generic handler, which would drop the notification
    and re-run the refusal on the next one.
    """

    def guarded(*args, **kwargs):
        kwargs.setdefault("num_retries", _RETRIES)
        try:
            return execute(*args, **kwargs)
        except Exception as exc:
            if not _is_grant_refusal(exc):
                raise
            raise GrantLost(
                f"{exc} — the Gmail grant no longer authenticates. "
                f"Run `{CONSENT_COMMAND}` on the host."
            ) from exc

    return guarded


class GrantAware:
    """Proxy over the fluent Gmail service that guards every `.execute()`.

    Graph funnels its calls through one `GraphSession.request`, which is where
    `_raise_for_status` reads a 401. Gmail has no such object — `users()`,
    `messages()`, `drafts()` and `labels()` each build their own request — so
    the choke point every call does share is `execute()`.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def __getattr__(self, name):
        attr = getattr(self._wrapped, name)
        if not callable(attr):
            return attr
        if name == "execute":
            return _guard(attr)
        return lambda *args, **kwargs: GrantAware(attr(*args, **kwargs))


def _bounded(query: str, since: str | None) -> str:
    """`query` AND-ed with Gmail's own floor, one day wider than the real one.

    Gmail's `after:` takes a date, not an instant, and reads it in the account's
    timezone — so an exact translation is not on offer. Widening is the direction
    that costs nothing: `window.in_scope` is the boundary, and this only keeps
    `max_results` from being spent listing mail the run will drop. Narrowing by a
    timezone would instead lose mail the floor says is in scope, invisibly.
    """
    if since is None:
        return query
    floor = datetime.fromisoformat(since).date() - timedelta(days=1)
    clause = f"after:{floor:%Y/%m/%d}"
    return f"{query} {clause}" if query else clause


def _list_message_ids(service, query: str, max_results: int,
                      since: str | None = None) -> list[str]:
    query = _bounded(query, since)
    ids: list[str] = []
    page_token = None
    while len(ids) < max_results:
        response = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=min(_PAGE_SIZE, max_results - len(ids)),
                pageToken=page_token,
            )
            .execute()
        )
        ids.extend(m["id"] for m in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return ids[:max_results]


class GmailFetcher:
    """Fetcher implementation (STU-324) bound to an already-built service.

    No Google imports: the service object is injected, so tests run against a
    fake without the optional `[fetch]` deps installed.
    """

    def __init__(self, service):
        self._service = service
        self._label_names: dict[str, str] | None = None

    def _name_of(self, label_ids: list[str]) -> set[str]:
        """Label names for ids. Gmail reports ids; only the system ones are their
        own name, and `ec/processed` is not a system label. Listed once per
        fetcher — the names do not change under a run."""
        if self._label_names is None:
            listed = self._service.users().labels().list(userId="me").execute()
            self._label_names = {
                label["id"]: label["name"] for label in listed.get("labels", [])
            }
        return {self._label_names.get(label_id, label_id) for label_id in label_ids}

    def _incoming(self, label_ids: list[str]) -> bool:
        """Whether the mailbox still shows this message as live incoming mail.

        TRASH is the one `_NOT_INCOMING` label that can be ours (STU-983): a
        message carrying `ec/deleted` is one this app threw out, and it comes back
        when a reply reconciles the conversation to a live bucket.
        """
        blocking = _NOT_INCOMING & set(label_ids)
        if not blocking:
            return True
        return blocking == {"TRASH"} and trashed_by_us(self._name_of(label_ids))

    def fetch_inputs_by_ids(self, message_ids: list[str]) -> list[dict]:
        """Fetch each message as metadata and project to EmailInput dicts.

        `_NOT_INCOMING` is filtered here and not only by the listing (STU-993):
        the push path names ids straight off a `history.list` record and never
        lists, and the queue is drained off-band (STU-927), so the gap between
        the notification and this GET is however long the queue is. A message
        trashed in that window would be classified, un-trashed by apply and
        answered with a draft. `labelIds` rides on the `format="metadata"` read
        each id costs either way — a filter, not a second call.

        TRASH takes the same exemption it takes at the listing (STU-983): this is
        the call `threads.py` reads a conversation's missing half back with, so a
        blanket drop here would take back exactly what `conversation_message_ids`
        just offered. The window this guard exists for is a *human* throwing mail
        out, and `trashed_by_us` is false for that.
        """
        inputs: list[dict] = []
        for message_id in message_ids:
            raw = (
                self._service.users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=_METADATA_HEADERS,
                )
                .execute()
            )
            if self._incoming(raw.get("labelIds", [])):
                inputs.append(projection.message_to_input(raw))
        return inputs

    def fetch_inputs(self, query: str, max_results: int,
                     since: str | None = None) -> list[dict]:
        """List matching messages, fetch each as metadata, project to EmailInput dicts.

        `since` is the run's date floor (STU-1123), carried into the query so the
        window is spent on mail the run can actually use. It is a hint and never
        the boundary — see `_bounded`.
        """
        listing = f"{query} {_LISTING_EXCLUDES}" if query else _LISTING_EXCLUDES
        return self.fetch_inputs_by_ids(
            _list_message_ids(self._service, listing, max_results, since)
        )

    def conversation_message_ids(self, thread_id: str) -> list[str]:
        """Every incoming message of that thread the mailbox still holds (STU-963).

        Spam and draft messages are dropped: a draft is what we are about to write,
        and a message somebody threw out is not part of what the thread currently
        says. So is SENT — the club's own reply is not mail to classify, and left in
        it would be scored, could become the thread's most recent message, and would
        be stamped and archived out of Sent Mail.

        TRASH is dropped on the same reasoning but not the same test (STU-983):
        "somebody threw it out" is only true of a message *we* did not trash, and
        our own says so on itself — `ec/deleted`, which `trashed_by_us` reads. Those
        come back: a reply can reconcile the conversation to a live bucket, and
        untrashing the half an earlier run threw out is what closes the split. A
        message a human deleted carries no such label and never re-enters — it would
        be re-classified every run, would vote in the thread's verdict, and could be
        untrashed against them.

        `format="minimal"` because only ids and labels are read here; the caller
        projects through `fetch_inputs_by_ids` like any other id.
        """
        thread = (
            self._service.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal")
            .execute()
        )

        return [
            message["id"]
            for message in thread.get("messages", [])
            if self._incoming(message.get("labelIds", []))
        ]


_SENT_HEADERS = ["Subject", "In-Reply-To", "References"]


class GmailSentSource:
    """The Sent folder of a Gmail mailbox read as a second, read-only record of
    replies (STU-2082): a deployment moving to another mailbox still answers from
    this one for a while, and its own Sent folder cannot see those replies."""

    def __init__(self, service):
        self._service = service

    def sent_to(self, correspondent: str, after: str) -> list[dict]:
        """Every message sent to `correspondent` after the instant `after`.

        Gmail's `after:` takes epoch seconds as well as a date, so the bound is exact
        here, unlike the fetch's (`_bounded`). The date is Gmail's own `internalDate`,
        not the `Date` header a client wrote.
        """
        epoch = int(datetime.fromisoformat(after.replace("Z", "+00:00")).timestamp())
        query = f"in:sent to:{correspondent} after:{epoch}"
        sent = []
        for message_id in _list_message_ids(self._service, query, _PAGE_SIZE):
            raw = (
                self._service.users()
                .messages()
                .get(userId="me", id=message_id, format="metadata",
                     metadataHeaders=_SENT_HEADERS)
                .execute()
            )
            by_name = {h["name"].lower(): h["value"]
                       for h in raw.get("payload", {}).get("headers", [])}
            sent.append({
                "date": datetime.fromtimestamp(int(raw["internalDate"]) / 1000,
                                               tz=timezone.utc).isoformat(),
                "subject": by_name.get("subject", ""),
                "in_reply_to": by_name.get("in-reply-to", ""),
                "references": by_name.get("references", ""),
            })
        return sent
