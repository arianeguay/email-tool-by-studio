"""One-click unsubscribe from bulk senders the owner never reads (STU-2071).

Off unless the policy sets `auto_unsubscribe: true`. A sender qualifies when one of the
run's messages from it is in bucket C, carries an RFC 8058 one-click header with an
`https` URL, and the mailbox holds at least two messages from it, none ever read.

Only the RFC 8058 POST is ever sent, with redirects refused: a `mailto:` would send mail
as the owner, and a GET on the link is a page visit a sender can count as engagement.
Each sender is posted to once, whatever the answer, and journaled before the next one.
"""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from email_classifier import deployment
from email_classifier.orchestrate.report import EmailVerdict
from email_classifier.stage1.policy import Policy

JOURNAL = deployment.ROOT / "logs" / "unsubscribed.jsonl"
_ONE_CLICK = "List-Unsubscribe=One-Click"
_URI = re.compile(r"<([^<>]+)>")


def one_click_url(headers: dict[str, str]) -> str | None:
    if headers.get("List-Unsubscribe-Post", "").strip().lower() != _ONE_CLICK.lower():
        return None
    return next(
        (uri for uri in _URI.findall(headers.get("List-Unsubscribe", ""))
         if uri.lower().startswith("https://")),
        None,
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def post_one_click(url: str) -> int | None:
    """The HTTP status of the RFC 8058 POST, None when no answer came back."""
    request = urllib.request.Request(
        url, data=_ONE_CLICK.encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return None


def _journaled(journal: Path) -> set[str]:
    if not journal.exists():
        return set()
    lines = journal.read_text(encoding="utf-8").splitlines()
    return {json.loads(line)["sender"] for line in lines if line.strip()}


def unsubscribe(
    verdicts: list[EmailVerdict],
    writer,
    policy: Policy,
    run_id: str,
    dry_run: bool = True,
    journal: Path = JOURNAL,
    post=post_one_click,
) -> list[str]:
    """The senders unsubscribed from, or that would be under `dry_run`, which posts nothing."""
    if not policy.auto_unsubscribe:
        return []
    checked = _journaled(journal)
    senders = []
    for v in verdicts:
        if v.status != "ok" or v.bucket != "C":
            continue
        found = writer.sender_and_headers(v.message_id)
        if found is None:
            continue
        sender, headers = found
        url = one_click_url(headers)
        if url is None or sender in checked:
            continue
        checked.add(sender)
        seen = writer.seen_from(sender)
        if len(seen) < 2 or any(seen):
            continue
        senders.append(sender)
        if dry_run:
            continue
        status = post(url)
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "sender": sender, "host": urlsplit(url).hostname,
                "status": status, "run_id": run_id,
            }) + "\n")
    return senders


def render_summary(senders: list[str], dry_run: bool) -> str:
    verb = "would unsubscribe from" if dry_run else "unsubscribed from"
    return f"  {verb} {len(senders)} senders" + "".join(f"\n    {s}" for s in senders)
