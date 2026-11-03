"""Outlook client: OAuth2 (MSAL) + Microsoft Graph read loop (STU-325).

Mirror of `fetch/gmail/client.py`. Reading the mailbox — `GET /me/messages` and
`GET /me/messages/{id}` — runs under `Mail.Read`. `push.py` also writes to
`/subscriptions`, which registers notification delivery rather than touching
mail, and is likewise permitted under `Mail.Read`.

Mutating the mailbox needs `Mail.ReadWrite`, which a fetch never asks for: the
write path requests it explicitly and `preflight_write_scope` refuses to start
an apply run when the cached grant does not carry it (STU-844).

msal and requests are imported lazily (inside the auth/HTTP functions) so the
pagination logic and its tests run without the optional `[fetch-outlook]` deps
installed.
"""

import json
import logging
import os
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from email_classifier import draft_pairing
from email_classifier.fetch.grant import GrantLost
from email_classifier.fetch.outlook import projection
from email_classifier.providers import trashed_by_us

_log = logging.getLogger("email_classifier.fetch.outlook")

# Everything the read and subscription paths need.
READ_SCOPES = ["Mail.Read"]

# Minimal scope for categories, folder moves and delete-to-Deleted-Items. Never
# widen further (Mail.Send, Mail.ReadWrite.All): those capabilities have no
# caller here.
WRITE_SCOPES = ["Mail.ReadWrite"]

CONSENT_COMMAND = "make outlook-consent"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Fields requested from Graph. `$select` is the privacy allowlist at the request
# boundary: no attachments, no recipients, and never `body` — a reply's `body`
# carries the whole quoted chain, so classifying on it reads the sender through
# our own earlier answer (STU-935). `internetMessageHeaders` is only returned by
# a single-message GET, which is why listing is ids-only.
_SELECT = (
    "id,conversationId,from,subject,receivedDateTime,uniqueBody,"
    "inferenceClassification,internetMessageHeaders,parentFolderId,categories,isDraft"
)

# The drafting subset only (STU-852). Graph has no preview of `uniqueBody`, so
# `_SELECT` already returns the whole thing and this call repeats it — kept
# because the drafts path is provider-agnostic and Gmail's snippet really is a
# snippet.
_BODY_SELECT = "id,uniqueBody"

# A precedent hit (STU-872): when it was written and what it says, nothing else.
_PRECEDENT_SELECT = "id,receivedDateTime,parentFolderId,uniqueBody"

# An earlier turn of the conversation being answered (STU-941): the precedent fields
# plus `from`, for the per-turn domain veto (STU-1467). Read for the opposite reason
# from a precedent: this is the sender's own thread, so the turn keeps the batch's
# indexed tokens instead of being flattened.
_THREAD_SELECT = "id,receivedDateTime,from,uniqueBody"

# How many earlier turns reach the prompt. A club thread is three or four
# messages; on a longer one the recent turns are what a follow-up refers to, and
# the whole of it would crowd out the mail being answered — one Graph GET each,
# inside the drafting stage's timeout.
_HISTORY_TURNS = 6

# Folders a message sits in only because it is no longer part of the mailbox.
# A turn the run itself trashed, one a human deleted, or one Outlook filed as
# junk is not conversation context: deleting a message is the clearest statement
# that it should not be read again (STU-962).
_DELETED_FOLDER = "deleteditems"
_GONE_FOLDERS = (_DELETED_FOLDER, "junkemail")

# Folders a message to classify never sits in: the gone ones, plus our own replies.
# A draft is the same exclusion with no folder to name it — it is read off
# `isDraft`, the way `_conversation_messages` filters it (STU-996).
_NOT_INCOMING = (*_GONE_FOLDERS, "sentitems")

# Graph returns `uniqueBody` as HTML unless asked otherwise; plain text spares the
# app an HTML stripper.
_TEXT_BODY = ['outlook.body-content-type="text"']

# Graph caps a `$search` page at 250; 100 keeps plain and search listings on one
# code path.
_PAGE_SIZE = 100

_DEFAULT_TOKEN = Path("secrets/outlook-token.json")

# Client credentials ask for every application role the registration was consented
# to; there is no per-call scope to narrow, so `WRITE_SCOPES` governs the delegated
# flow only and the app-only grant is checked against its `roles` instead.
_APP_ONLY_SCOPE = ["https://graph.microsoft.com/.default"]


# Personal (outlook.com) and work accounts both sign in through `common`.
_DEFAULT_TENANT = "common"


def configured_mailbox() -> str | None:
    """The mailbox this process addresses by id, or None when it runs as `/me`."""
    return (os.environ.get("OUTLOOK_MAILBOX") or "").strip() or None


def _client_secret() -> str | None:
    return (os.environ.get("OUTLOOK_CLIENT_SECRET") or "").strip() or None


def app_only() -> bool:
    """Whether this process authenticates as itself rather than as a signed-in user.

    The secret decides, not a flag: a flag is a third setting to get wrong, and the
    two credentials are already mutually exclusive — a public client has no secret
    and a confidential one cannot run the interactive flow we use.

    App-only has no `/me`, so a secret without a mailbox is refused rather than
    defaulted. Guessing which mailbox to read is the one mistake here that is
    silent and destructive at once (STU-1087 refused to guess a port for less).
    """
    if not _client_secret():
        return False
    if not configured_mailbox():
        raise ValueError(
            "OUTLOOK_CLIENT_SECRET is set but OUTLOOK_MAILBOX is not — an app-only "
            "grant authenticates as the application, so there is no signed-in user "
            "for `/me` to mean. Name the mailbox to address."
        )
    return True


def _token_path(token_file: Path | None = None) -> Path:
    return token_file or Path(os.environ.get("OUTLOOK_TOKEN_FILE") or _DEFAULT_TOKEN)


def _short(scope: str) -> str:
    """Graph reports scopes bare and fully qualified; compare on the name alone."""
    return scope.rsplit("/", 1)[-1].casefold()


# Graph answers with the scopes it granted, not the ones that were asked for: an
# app consented to Mail.ReadWrite is handed `scope: "Mail.ReadWrite"` even when
# the request said Mail.Read. Comparing the names literally reads that as a
# refusal, so a read call re-consents forever against a grant that already covers
# it. Only the supersets Graph actually collapses belong here.
_SATISFIED_BY = {"mail.read": frozenset({"mail.read", "mail.readwrite"})}


def _satisfies(scope: str) -> frozenset[str]:
    name = _short(scope)
    return _SATISFIED_BY.get(name, frozenset({name}))


def _honours(result: dict | None, scopes: list[str]) -> bool:
    if not result or "access_token" not in result:
        return False
    granted = result.get("scope")
    if granted is None:
        # A cache hit carries no `scope` at all — MSAL answers with just
        # access_token/expires_in/token_type/token_source. It only serves a token
        # whose cached target already covers the requested scopes, so the match is
        # its word and there is nothing left to verify. Reading the absent key as
        # an empty grant refuses every cache hit, which is a consent per call.
        return True
    granted = {_short(scope) for scope in granted.split()}
    return all(_satisfies(scope) & granted for scope in scopes)


def _why_silent_failed(result: dict | None) -> str:
    """What MSAL said, for the `GrantLost` message a human reads days later."""
    if not result:
        return "no cached account"
    if "access_token" not in result:
        return result.get("error_description") or result.get("error") or "no token returned"
    return f"granted {result.get('scope', '') or 'nothing'}"


def granted_scopes(token_file: Path | None = None) -> set[str]:
    """Scopes carried by the cached Graph tokens, unqualified and casefolded.

    Empty when the cache is absent or unparseable — the caller decides what that
    means.
    """
    try:
        cache = json.loads(_token_path(token_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {
        _short(scope)
        for entry in cache.get("AccessToken", {}).values()
        for scope in entry.get("target", "").split()
    }


def _app_only_roles() -> set[str] | None:
    """The application roles the grant carries, or None when they cannot be read.

    Read off the access token's own claims: app-only has no token cache to inspect
    and asking Graph for the application object would need `Application.Read.All`,
    a permission this app should not hold to read one field.

    None is "could not tell", which is not "insufficient" — a token Microsoft
    stops shaping as a readable JWT must not turn every apply run into a refusal.
    The 403 that preflight exists to pre-empt stays the backstop for that case.
    """
    import base64

    try:
        token = _app_only_token_provider()()
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except GrantLost:
        raise
    except Exception:
        return None
    return {_short(role) for role in claims.get("roles", [])}


def preflight_write_scope(token_file: Path | None = None) -> str | None:
    """None when the grant may mutate the mailbox, else why it may not.

    An absent or unreadable cache refuses too: an apply run is not the place to
    discover that consent is missing, halfway through mutating the mailbox.
    """
    if app_only():
        roles = _app_only_roles()
        if roles is None:
            _log.warning(
                "could not read the app-only grant's roles from its token — the "
                "write preflight did not run, and a missing role will surface as a "
                "403 mid-run instead"
            )
            return None
        missing = [s for s in WRITE_SCOPES if _short(s) not in roles]
        if not missing:
            return None
        return (
            f"the app-only grant is missing the {', '.join(missing)} application "
            f"role (granted: {', '.join(sorted(roles)) or 'nothing'}) — mutating the "
            "mailbox would 403 mid-run. Grant it in Entra and re-consent as admin."
        )
    granted = granted_scopes(token_file)
    missing = [s for s in WRITE_SCOPES if _short(s) not in granted]
    if not missing:
        return None
    needed = ", ".join(missing)
    if not granted:
        return (
            f"no Graph scopes cached in {_token_path(token_file)}: {needed} must be "
            f"consented before mutating an Outlook mailbox. Run `{CONSENT_COMMAND}`."
        )
    return (
        f"the cached Graph token is missing {needed} (granted: "
        f"{', '.join(sorted(granted))}) — mutating the mailbox would 403 mid-run. "
        f"Re-consent with `{CONSENT_COMMAND}`."
    )


def _build_app(client_id: str | None, tenant: str | None, cache_path: Path):
    """Build the MSAL app over the persisted token cache. Returns (cache, app)."""
    import msal

    app_id = client_id or os.environ.get("OUTLOOK_CLIENT_ID")
    if not app_id:
        raise ValueError(
            "OUTLOOK_CLIENT_ID is not set — register an Azure AD app (see README)."
        )
    authority = (
        "https://login.microsoftonline.com/"
        f"{tenant or os.environ.get('OUTLOOK_TENANT_ID') or _DEFAULT_TENANT}"
    )
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8"))
    return cache, msal.PublicClientApplication(app_id, authority=authority, token_cache=cache)


def signed_in_address(
    client_id: str | None = None,
    tenant: str | None = None,
    token_file: Path | None = None,
) -> str | None:
    """The mailbox the cached grant belongs to, or None when nothing is cached.

    Read from the MSAL account, not from `GET /me`: the profile endpoint needs
    `User.Read`, which no path here consents to, so asking Graph 403s under a
    mail-only grant. The account carries the username the token was issued for,
    which is the identity every write will actually run as.

    App-only has no account to read, and needs none: the mailbox is named rather
    than inferred, so the configured name *is* the answer.
    """
    if app_only():
        return configured_mailbox()
    _, app = _build_app(client_id, tenant, _token_path(token_file))
    accounts = app.get_accounts()
    return (accounts[0].get("username") or "").lower() or None if accounts else None


def _app_only_token_provider(
    client_id: str | None = None, tenant: str | None = None
) -> Callable[[], str]:
    """Tokens for the application itself, over a client secret.

    No cache file and no refresh token: the secret *is* the credential, and a new
    token is minted per hour from it. That is the whole reason for this path — a
    delegated grant dies quietly (STU-930) and this one cannot.

    A refusal here is permanent until a human edits the secret, so it raises
    `GrantLost` like a dead delegated grant: the callers that stop a service on
    one must stop on the other.
    """
    app_id = client_id or os.environ.get("OUTLOOK_CLIENT_ID")
    if not app_id:
        raise ValueError(
            "OUTLOOK_CLIENT_ID is not set — register an Azure AD app (see README)."
        )
    tenant = tenant or os.environ.get("OUTLOOK_TENANT_ID") or _DEFAULT_TENANT
    if tenant == _DEFAULT_TENANT:
        raise ValueError(
            "OUTLOOK_TENANT_ID must name the tenant for an app-only grant: "
            f"`{_DEFAULT_TENANT}` is a sign-in router for users, and client "
            "credentials have no user to route."
        )

    # After the configuration checks, so a misconfiguration is named on a machine
    # that has not installed the optional Graph dependency — the reason the whole
    # module imports msal lazily.
    import msal

    app = msal.ConfidentialClientApplication(
        app_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=_client_secret(),
    )

    def access_token() -> str:
        result = app.acquire_token_for_client(scopes=_APP_ONLY_SCOPE) or {}
        if "access_token" not in result:
            raise GrantLost(
                "the Outlook app-only grant did not authenticate "
                f"({result.get('error_description') or result.get('error') or result}). "
                "The client secret may have expired or been rotated."
            )
        return result["access_token"]

    return access_token


def get_token_provider(
    client_id: str | None = None,
    tenant: str | None = None,
    token_file: Path | None = None,
    scopes: list[str] | None = None,
    interactive: bool = True,
) -> Callable[[], str]:
    """Return a callable that yields a valid Graph access token on every call.

    Graph tokens live one hour, so the token is acquired per request rather than
    once per run: MSAL serves it from its cache and silently redeems the refresh
    token when it has expired. The cache is persisted to `token_file` so later
    runs are non-interactive; the first run opens a browser for consent.

    `scopes` defaults to READ_SCOPES; the write path passes WRITE_SCOPES. A
    refresh token predating the wider grant redeems silently but comes back
    short of the requested scopes, so the result is checked and re-consented
    rather than trusted.

    `interactive=False` is for a caller with no human at a browser — a service,
    a cron job. Consent there cannot succeed, so what would open a browser
    raises `GrantLost` instead (STU-930).
    """
    if app_only():
        return _app_only_token_provider(client_id, tenant)
    scopes = scopes or READ_SCOPES
    cache_path = _token_path(token_file)
    cache, app = _build_app(client_id, tenant, cache_path)

    # A token is acquired per Graph call, so an unconditional interactive fallback
    # opens one browser window per message. Consent is offered once; if the grant
    # it returns still falls short, the next call fails loud instead of prompting
    # fifty times.
    consented = False

    def access_token() -> str:
        nonlocal consented
        accounts = app.get_accounts()
        result = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
        if not _honours(result, scopes):
            if not interactive:
                raise GrantLost(
                    "the cached Graph grant no longer redeems silently "
                    f"({_why_silent_failed(result)}) and this process has no "
                    f"browser to consent in. Run `{CONSENT_COMMAND}` on the host."
                )
            if consented:
                raise RuntimeError(
                    f"Outlook sign-in still short of {' '.join(scopes)} after consenting "
                    f"(granted: {(result or {}).get('scope', '') or 'nothing'}). "
                    f"Re-run `{CONSENT_COMMAND}`."
                )
            result = app.acquire_token_interactive(scopes, prompt="consent")
            consented = True
        if "access_token" not in result:
            raise RuntimeError(
                f"Outlook sign-in failed: {result.get('error_description', result)}"
            )
        if not _honours(result, scopes):
            raise RuntimeError(
                f"Outlook sign-in granted {result.get('scope', '')!r}, "
                f"short of the requested {' '.join(scopes)}."
            )
        if cache.has_state_changed:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(cache.serialize(), encoding="utf-8")
        return result["access_token"]

    return access_token


# Graph puts the reason in the response body: `ErrorInvalidReferenceItem` and its
# message are what tell a refused operation apart from a bad id or a missing
# scope. `raise_for_status` builds its message from the status line and URL
# alone, and every journal here records `str(exc)` (STU-862).
_ERROR_BODY_LIMIT = 500


def _raise_for_status(response) -> None:
    import requests

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = (response.text or "").strip()
        if len(body) > _ERROR_BODY_LIMIT:
            body = f"{body[:_ERROR_BODY_LIMIT]}... (truncated)"
        detail = f"{exc}: {body}" if body else str(exc)
        # 401 is the grant itself, not this call: MSAL refreshes the access token
        # per request, so a token that reaches Graph and is refused means the
        # refresh behind it no longer stands. Retrying re-runs the same refusal
        # (STU-930). A 403 is a scope, which `preflight_write_scope` owns.
        if response.status_code == 401:
            raise GrantLost(
                f"{detail} — the Outlook grant no longer authenticates. "
                f"Run `{CONSENT_COMMAND}` on the host."
            ) from exc
        if not body:
            raise
        raise requests.HTTPError(detail, response=response) from exc


class GraphSession:
    """Authenticated Graph calls. The unit `OutlookFetcher` is built on."""

    def __init__(
        self,
        token_provider: Callable[[], str],
        base_url: str = GRAPH_BASE,
        mailbox: str | None = None,
    ):
        self._token_provider = token_provider
        self._base_url = base_url
        self._mailbox = mailbox

    @property
    def mailbox_path(self) -> str:
        """What `/me` means for this session — itself, or one addressed mailbox.

        Every Graph path in this package is written `/me/...`, and every one of them
        funnels through `request`, so the substitution belongs here rather than at
        forty call sites. Rewriting one prefix in one place is also what keeps the
        two credentials from becoming two dialects of the same client.
        """
        if self._mailbox is None:
            return "/me"
        return f"/users/{quote(self._mailbox)}"

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        payload: dict | None = None,
        prefer: list[str] | None = None,
    ) -> dict:
        """Call a Graph path (or an absolute `@odata.nextLink`) and return the JSON."""
        import requests

        if not url.startswith("https://"):
            if url == "/me" or url.startswith("/me/"):
                url = f"{self.mailbox_path}{url[len('/me'):]}"
            url = f"{self._base_url}{url}"
        response = requests.request(
            method,
            url,
            params=params,
            json=payload,
            headers={
                "Authorization": f"Bearer {self._token_provider()}",
                # A default Graph id changes when the message moves between
                # folders, which is exactly what archiving and trashing do
                # (STU-847) — the report and the undo journal would both point at
                # ids that no longer resolve. Immutable ids survive the move, and
                # they have to be asked for on every call: an immutable id does
                # not resolve on a request that omits the header.
                "Prefer": ", ".join(['IdType="ImmutableId"', *(prefer or [])]),
            },
            timeout=30,
        )
        _raise_for_status(response)
        # DELETE answers 204 with no body, and .json() would choke on it.
        return response.json() if response.content else {}

    def get(
        self, url: str, params: dict | None = None, prefer: list[str] | None = None
    ) -> dict:
        return self.request("GET", url, params, prefer=prefer)


def build_session(
    token_provider: Callable[[], str] | None = None,
    scopes: list[str] | None = None,
    interactive: bool = True,
    mailbox: str | None = None,
) -> GraphSession:
    """Build a Graph session, running the OAuth flow unless a provider is given.

    `mailbox` defaults to whatever the environment configures, so every existing
    caller reaches the right mailbox without being told about the flow it runs
    under — which is the point: only this module knows there are two.
    """
    return GraphSession(
        token_provider or get_token_provider(scopes=scopes, interactive=interactive),
        mailbox=mailbox if mailbox is not None else configured_mailbox(),
    )


def _list_message_ids(
    session: GraphSession,
    query: str,
    max_results: int,
    exclude_folders: frozenset[str] | set[str] = frozenset(),
    since: str | None = None,
) -> list[str]:
    """Ids of matching messages, dropping drafts and the ones in `exclude_folders`.

    Folders are filtered here rather than through `$filter`, which Graph rejects
    alongside `$search`, and before the cap so an excluded message does not spend
    one of the `max_results` slots.

    `since` is the run's date floor (STU-1123), and it rides on `$filter` — so it
    is only sent on the unsearched listing, for the same reason the folders are
    not. Under a `$search` the floor is enforced by `window.in_scope` alone and the
    window can be spent on mail the run then drops; that is a searched fetch's
    price, not a hole, since the boundary itself still holds.

    Drafts go unconditionally, no caller opting in (STU-996): `/me/messages` is
    every folder, and a draft is mail nobody has written yet — not a message to
    classify, and not how the club writes when a precedent quotes it back.
    """
    ids: list[str] = []
    url = "/me/messages"
    # Graph rejects $search combined with $orderby or $filter, so only one of the
    # two shapes is sent.
    params: dict | None = {
        "$select": "id,parentFolderId,isDraft",
        "$top": min(_PAGE_SIZE, max_results),
        **({"$search": f'"{query}"'} if query else {
            "$orderby": "receivedDateTime desc",
            **({"$filter": _received("ge", since)} if since else {}),
        }),
    }
    while len(ids) < max_results:
        page = session.get(url, params)
        ids.extend(
            m["id"]
            for m in page.get("value", [])
            if not m.get("isDraft") and m.get("parentFolderId") not in exclude_folders
        )
        url = page.get("@odata.nextLink")
        if not url:
            break
        # nextLink already carries every query option.
        params = None
    return ids[:max_results]


def _received(op: str, when: str) -> str:
    """A `receivedDateTime` clause. Graph wants a bare datetime literal, `Z`-suffixed;
    our dates carry `+00:00`. The operator is the caller's: `ge` for the run's floor,
    which is midnight of a day that counts, `gt` for a watermark, which is a message
    already classified."""
    return f"receivedDateTime {op} {when.replace('+00:00', 'Z')}"


def _list_inbox_ids_since(session: GraphSession, since: str) -> list[str]:
    """Ids of inbox messages received after `since` (ISO 8601 UTC).

    Same folder the subscription watches, so a `missed` catch-up covers exactly
    what delivery would have carried. Unbounded on purpose: a cap here would
    silently truncate the gap, which is the failure this path exists to close.
    """
    ids: list[str] = []
    url = "/me/mailFolders('inbox')/messages"
    params: dict | None = {
        "$select": "id",
        "$filter": _received("gt", since),
        "$top": _PAGE_SIZE,
    }
    while True:
        page = session.get(url, params)
        ids.extend(m["id"] for m in page.get("value", []))
        url = page.get("@odata.nextLink")
        if not url:
            return ids
        params = None


# What Graph answers on `/me/messages/{id}` when the id is not one: a
# conversation id is malformed there (400), a well-formed id naming nothing is
# missing (404). Anything else — auth, throttling, outage — is not a routing
# signal and stays raised.
_NOT_A_MESSAGE_ID = frozenset({400, 404})


class OutlookFetcher:
    """Fetcher implementation (STU-324) bound to an already-built Graph session.

    No msal/requests imports: the session is injected, so tests run against a
    fake without the optional `[fetch-outlook]` deps installed.
    """

    def __init__(self, session: GraphSession):
        self._session = session
        self._folders: dict[str, str] = {}

    def fetch_inputs_by_ids(self, message_ids: list[str]) -> list[dict]:
        """Fetch each message with the selected fields and project to EmailInput dicts.

        Deleted Items and Junk are dropped here and not only by the listing
        (STU-973): the push path names ids straight off a notification, and the
        queue is drained off-band (STU-927), so the gap between Graph announcing a
        message and this GET is however long the queue is. A message trashed in
        that window would be classified, moved back out of the trash by apply and
        answered with a draft. `parentFolderId` rides on the GET each id costs
        either way — a wider `$select`, not a second call.

        Deleted Items takes the same exemption it takes at the listing (STU-983):
        this is the call `threads.py` fetches a conversation's missing half with,
        so a blanket drop here would take back exactly what
        `conversation_message_ids` just offered. `ec/deleted` is what separates the
        two — the window this guard exists for is a *human* throwing mail out, and
        `trashed_by_us` is false for that.

        Sent Items and drafts go here as well, on the same reasoning and against
        the same window (STU-996): `_NOT_INCOMING`, not `_GONE_FOLDERS`, so this
        funnel and `conversation_message_ids` answer "incoming mail" once.
        """
        not_incoming = self._folder_ids(_NOT_INCOMING)
        deleted = self._folder_ids((_DELETED_FOLDER,))
        inputs: list[dict] = []
        for message_id in message_ids:
            message = self._session.get(
                f"/me/messages/{message_id}", {"$select": _SELECT}, _TEXT_BODY
            )
            folder = message.get("parentFolderId")
            if message.get("isDraft"):
                continue
            if folder not in not_incoming or (
                folder in deleted and trashed_by_us(message.get("categories") or [])
            ):
                inputs.append(projection.message_to_input(message))
        return inputs

    def fetch_inputs_since(self, since: str) -> list[dict]:
        """Every inbox message received after `since` — the `missed` catch-up (STU-937)."""
        return self.fetch_inputs_by_ids(_list_inbox_ids_since(self._session, since))

    def inbox_received_since(self, since: str) -> list[tuple[str, str]]:
        """`(id, receivedDateTime)` of every Inbox message received at or after `since`.

        Ids and dates only (STU-1502): the caller diffs them against `reports/`, and a
        subject or a sender would put a parent's mail in a cron line for nothing.
        Unbounded, like `_list_inbox_ids_since`: a cap would hide the oldest miss,
        which is the one the check exists to name.
        """
        found: list[tuple[str, str]] = []
        url = "/me/mailFolders('inbox')/messages"
        params: dict | None = {
            "$select": "id,receivedDateTime,isDraft",
            "$filter": _received("ge", since),
            "$top": _PAGE_SIZE,
        }
        while url:
            page = self._session.get(url, params)
            found.extend(
                (m["id"], m["receivedDateTime"])
                for m in page.get("value", [])
                if not m.get("isDraft")
            )
            url = page.get("@odata.nextLink")
            params = None
        return found

    def fetch_input_by_reference(self, ref: str) -> dict:
        """One EmailInput from what an Outlook link named — a message or a thread.

        Both are the same URL shape (`mail_link.message_ref`), so which one was
        pasted is settled by asking. A conversation id is not a message id, and
        Graph rejects it on the message route as a *bad* id, not a missing one:
        `400 ErrorInvalidIdMalformed`. 404 comes back only for a well-formed id
        naming nothing — a mail deleted between copy and run. Both mean "ask the
        conversation". The conversation is then read for the mail a reply would
        answer, its most recent message that is not a draft — the draft being
        redrafted is exactly what has to be stepped over.
        """
        try:
            return projection.message_to_input(
                self._session.get(
                    f"/me/messages/{ref}", {"$select": _SELECT}, _TEXT_BODY
                )
            )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in _NOT_A_MESSAGE_ID:
                raise
        return projection.message_to_input(self._conversation_reply_target(ref))

    def _conversation_messages(self, conversation_id: str, folder: str = "") -> list[dict]:
        """The conversation's sent-or-received messages, with the folder each sits in.

        Drafts are filtered out: a draft is what we are about to write, never part
        of the exchange. `folder` scopes the same query to one mail folder —
        `sentitems` is how a turn is known to be the club's own without asking the
        grant who the club is (STU-941). The list is ids and dates only for the
        same reason `fetch_inputs` is: `uniqueBody` and `internetMessageHeaders`
        come back on a single-message GET alone.

        Paged to the end rather than capped at one page: a truncated `sentitems`
        slice does not lose a turn, it relabels one — the club's own reply comes
        back as `received` and the drafter reads its own words as the parent's.
        """
        messages: list[dict] = []
        url = f"/me{folder}/messages"
        params: dict | None = {
            "$filter": f"conversationId eq '{conversation_id}' and isDraft eq false",
            "$select": "id,receivedDateTime,parentFolderId,categories",
            "$top": _PAGE_SIZE,
        }
        while True:
            page = self._session.get(url, params)
            messages.extend(page.get("value") or [])
            url = page.get("@odata.nextLink")
            if not url:
                return messages
            # nextLink already carries every query option.
            params = None

    def fetch_thread_history(self, thread_id: str, exclude_message_id: str) -> list[dict]:
        """The conversation's other turns, oldest first (STU-941).

        The mail being answered is excluded — the item already carries it in full
        as `body_text`, and sending it twice only spends tokens. What is left is
        what a follow-up refers to: the parent's earlier questions, and the club's
        own replies to them.

        The whole mailbox is listed, not the inbox: a turn the classifier already
        applied has been archived out of it, which is precisely the older half of
        a two-turn thread. Deleted Items and Junk are dropped back out (STU-962),
        before the cut so that a deleted turn does not spend one of the
        `_HISTORY_TURNS` slots.

        Sorted here rather than with `$orderby`, which Graph rejects alongside a
        `$filter` on another property, then cut to the most recent
        `_HISTORY_TURNS`.
        """
        sent = {
            message["id"]
            for message in self._conversation_messages(
                thread_id, "/mailFolders('sentitems')"
            )
        }
        gone = self._gone_folder_ids()
        earlier = sorted(
            (
                message
                for message in self._conversation_messages(thread_id)
                if message["id"] != exclude_message_id
                and message.get("parentFolderId") not in gone
            ),
            key=lambda message: message["receivedDateTime"],
        )[-_HISTORY_TURNS:]
        return [
            projection.message_to_turn(
                self._session.get(
                    f"/me/messages/{message['id']}", {"$select": _THREAD_SELECT}, _TEXT_BODY
                ),
                sent=message["id"] in sent,
            )
            for message in earlier
        ]

    def _folder_ids(self, names: tuple[str, ...]) -> set[str]:
        """Ids of well-known folders, each resolved once per fetcher.

        The well-known names are the same in every mailbox, the ids they resolve
        to are not. Read through the same session as the listing, so both sides
        of the comparison come back in the immutable form the `Prefer` header
        asks for.
        """
        for name in names:
            if name not in self._folders:
                self._folders[name] = self._session.get(
                    f"/me/mailFolders('{name}')", {"$select": "id"}
                )["id"]
        return {self._folders[name] for name in names}

    def _gone_folder_ids(self) -> set[str]:
        return self._folder_ids(_GONE_FOLDERS)

    def last_sent_at(self, thread_id: str) -> str | None:
        """When the club last replied on that conversation, None when it never did.

        Ids and dates only, deliberately not `fetch_thread_history`: the drafts
        selection asks this of every pending mail, including the ones it is about
        to skip, and a turn's body is what it would pay for them (STU-945).
        """
        sent = self._conversation_messages(thread_id, "/mailFolders('sentitems')")
        return max(
            (projection.iso_date(message["receivedDateTime"]) for message in sent),
            default=None,
        )

    def sent_replies(self, written_at: dict[str, str]) -> dict[str, dict]:
        """The club's first sent reply after each draft was written, with its text.

        Keyed by conversation, valued by the draft's write time; a conversation with
        no send after it is absent (STU-1998, `draft_pairing`).

        `last_sent_at` answers *when* for the drafting selection and stops there,
        because it is asked of every pending mail including the ones about to be
        skipped. This is asked of a handful of conversations already known to hold a
        draft, so it pays the body GET the comparison needs (STU-1100).

        `uniqueBody`, not `body`: a reply carries the quoted parent, and diffing our
        draft against their reply plus our own quoted mail reads every send as a
        rewrite.
        """
        replies = {}
        for thread_id, at in written_at.items():
            sent = self._conversation_messages(thread_id, "/mailFolders('sentitems')")
            first = draft_pairing.first_send_after(
                sent, at, lambda message: message["receivedDateTime"])
            if first is None:
                continue
            body = self.fetch_bodies_by_ids([first["id"]]).get(first["id"], "")
            replies[thread_id] = {
                "sent_at": projection.iso_date(first["receivedDateTime"]),
                "body": body,
            }
        return replies

    def _conversation_reply_target(self, conversation_id: str) -> dict:
        """The most recent live message a reply to that conversation answers.

        Deleted Items and Junk are dropped first (STU-967): the listing is
        mailbox-wide, so the newest turn of a conversation somebody has since
        thrown out is still in it, and replying to it answers the wrong text and
        quotes a deleted mail back at the sender. A conversation whose every turn
        is gone raises the `LookupError` an empty one does — nothing live is left
        to answer.
        """
        gone = self._gone_folder_ids()
        messages = [
            message
            for message in self._conversation_messages(conversation_id)
            if message.get("parentFolderId") not in gone
        ]
        if not messages:
            raise LookupError(
                f"no message to reply to in conversation {conversation_id}"
            )
        latest = max(messages, key=lambda message: message["receivedDateTime"])
        return self._session.get(
            f"/me/messages/{latest['id']}", {"$select": _SELECT}, _TEXT_BODY
        )

    def fetch_bodies_by_ids(self, message_ids: list[str]) -> dict[str, str]:
        """Full plain-text bodies, keyed by message id — drafting only (STU-852).

        A separate call rather than a wider `_SELECT`: every fetched mail would
        otherwise carry a body no classification reads.
        """
        return {
            message_id: projection.message_body(
                self._session.get(
                    f"/me/messages/{message_id}", {"$select": _BODY_SELECT}, _TEXT_BODY
                )
            )
            for message_id in message_ids
        }

    def search_messages(self, query: str, max_results: int) -> list[dict]:
        """Prior mail matching `query`, as `{date, direction, body}` hits (STU-872).

        `/me/messages` is the whole mailbox, not a folder, so the club's own sent
        replies are searched alongside what parents wrote — which is the half a
        precedent is wanted for. Deleted Items and Junk are dropped back out
        (STU-965): a phishing mail Outlook filed as junk, or an exchange somebody
        deleted, is the last thing to show the drafter as how the club writes.
        Each hit says whether the club sent it, read from Sent Items the way a
        thread turn is: a deployment may let a recent sent reply ground the club's
        current state, and never a parent's message (STU-2000).
        """
        sent_ids = self._folder_ids(("sentitems",))
        hits = []
        for message_id in _list_message_ids(
            self._session, query, max_results, self._gone_folder_ids()
        ):
            raw = self._session.get(
                f"/me/messages/{message_id}", {"$select": _PRECEDENT_SELECT}, _TEXT_BODY
            )
            hits.append(projection.message_to_precedent(raw, raw.get("parentFolderId") in sent_ids))
        return hits

    def fetch_inputs(self, query: str, max_results: int,
                     since: str | None = None) -> list[dict]:
        """List matching messages, fetch each one, project to EmailInput dicts.

        `/me/messages` is every folder, Deleted Items and Junk included, and this
        corpus is what apply writes to (STU-973): a trashed message classified
        into a bucket is *moved* out of the trash, and one classified
        `needs_reply` collects a draft. Deleting a mail is the clearest statement
        that it should not be acted on again. Every other folder stays in —
        Archive and user folders hold the older half of live threads.

        Sent Items and drafts are excluded too (STU-996) — `_NOT_INCOMING`, the
        set `conversation_message_ids` states the reason on. Measured before the
        fix: 7 of run 8704964d's 26 messages were the mailbox owner's own, three
        of them reply drafts an earlier run had written minutes earlier, and all
        seven escalated to stage 2 as `human_real`.
        """
        return self.fetch_inputs_by_ids(
            _list_message_ids(
                self._session, query, max_results, self._folder_ids(_NOT_INCOMING),
                since,
            )
        )

    def conversation_message_ids(self, thread_id: str) -> list[str]:
        """Every incoming message of that conversation the mailbox still holds (STU-963).

        Mailbox-wide, not inbox-scoped: the older half of a thread an earlier run
        archived is exactly what the caller is missing. Junk is dropped for the
        reason it is everywhere else (STU-962, STU-967) — a message somebody threw
        out is not part of what the thread currently says.

        Deleted Items is dropped on the same reasoning but not the same test
        (STU-983): "somebody threw it out" is only true of a message *we* did not
        trash, and our own says so on itself — `ec/deleted`, which `trashed_by_us`
        reads. Those come back: a reply can reconcile the conversation to a live
        bucket, and untrashing the half an earlier run threw out is what closes the
        split. A message a human deleted carries no such category and never
        re-enters — it would be re-classified every run, would vote in the thread's
        verdict, and could be untrashed against them.

        Sent Items is dropped too, and for a bigger reason: the club's own replies
        are not mail to classify. Left in, one would be scored like an incoming
        message, could be the conversation's most recent — deciding the thread's
        action and drawing a draft addressed to ourselves — and apply would stamp
        it and move it out of Sent Items. What a sent reply *does* mean for the
        thread is read where it belongs, off `last_sent_at` (STU-945).
        """
        gone = self._folder_ids(_NOT_INCOMING)
        deleted = self._folder_ids((_DELETED_FOLDER,))
        return [
            message["id"]
            for message in self._conversation_messages(thread_id)
            if message.get("parentFolderId") not in gone
            or (
                message.get("parentFolderId") in deleted
                and trashed_by_us(message.get("categories") or [])
            )
        ]
