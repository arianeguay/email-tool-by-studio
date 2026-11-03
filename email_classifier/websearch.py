"""`org_web-search_site` (STU-871): the club site, read live instead of remembered.

The deployment's facts skill is the club's facts as they stood the day
someone read them off the site. The site moves — the selections calendar lands on it,
coaches are named after the selections — so a draft grounded only in the skill answers
"the club will confirm" to questions the site already answers.

The agent supplies search words, never a URL: the pages are fixed by
`rules/web_sources.yaml`, and anything outside it is refused. A passage the tool
returns carries the URL it came from, which is what the draft cites.

A passage is a block of the page's visible text, or — for a page whose calendar
renders client-side — one event of the payload behind it (STU-895).
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import yaml

from email_classifier import deployment

REPO_ROOT = deployment.ROOT
_log = logging.getLogger("email_classifier.websearch")
TIMEOUT_SECONDS = 10
PASSAGE_LIMIT = 400

# A fan-out drafts a reply per email, each one searching the same handful of pages.
# Without this the club's site takes a few hundred requests per run.
CACHE_TTL_SECONDS = 3600
CACHE_DIR = REPO_ROOT / "dumps" / "web-cache"

_SKIPPED_TAGS = {"script", "style", "head", "noscript"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}

# The federation's calendar renders client-side: its visible text is a navigation
# shell, and every date sits in the event object handed to the calendar widget
# inside a <script> the block extractor skips (STU-895).
_EVENTS_PAYLOAD = re.compile(r'\{"\d+":\{"IdEvent"')
_MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
              "août", "septembre", "octobre", "novembre", "décembre"]


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[str] = []
        self._current: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping += 1
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._current.append(data)

    def _flush(self) -> None:
        block = re.sub(r"\s+", " ", "".join(self._current)).strip()
        if block:
            self.blocks.append(block)
        self._current = []

    def close(self) -> None:
        super().close()
        self._flush()


def to_blocks(html: str) -> list[str]:
    """The page's visible text, one entry per block-level element."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.blocks


def _french_date(timestamp: str) -> str:
    """`2025-11-01 07:00` as `1 novembre 2025`, so a month-name query reaches it."""
    year, month, day = timestamp[:10].split("-")
    return f"{int(day)} {_MONTHS_FR[int(month) - 1]} {year}"


def event_payload(html: str) -> list[dict]:
    """The events behind a client-side calendar, or nothing for a page without one.

    Exposed rather than kept inside `to_events` because the staleness check digests the
    same payload and asks a coarser question of it (STU-1097) — one parse, so the two
    readers cannot come to disagree about what an event is.
    """
    found = _EVENTS_PAYLOAD.search(html)
    if not found:
        return []
    payload, _ = json.JSONDecoder().raw_decode(html[found.start():])
    return list(payload.values())


def to_events(html: str) -> list[str]:
    """One dated passage per event, for a page whose calendar renders client-side."""
    passages = []
    for event in event_payload(html):
        start, end = _french_date(event["StartTime"]), _french_date(event["EndTime"])
        when = start if start == end else f"{start} au {end}"
        parts = [when, event["EventName"], event["Location"], event["CalendarName"]]
        passages.append(" — ".join(part for part in parts if part))
    return passages


# The extractors a page in `rules/web_sources.yaml` may declare itself under
# (STU-1471). A page with no `extractor:` key gets plain block extraction only —
# unconditional detection here would apply this to any page whose HTML happens to
# match, declared or not.
_EXTRACTORS = {"federation_calendar": to_events}


def fold(text: str) -> str:
    """Lowercased and stripped of accents, so `selections` finds `sélections`."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _entries(path: Path) -> list[dict]:
    """Each `pages:` item, normalized: a bare URL or a `{url, extractor}` mapping."""
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = config.get("pages") or []
    if not raw:
        raise ValueError(f"{path} lists no page — the tool has nothing it may read")
    return [
        {"url": item, "extractor": None} if isinstance(item, str)
        else {"url": item["url"], "extractor": item.get("extractor")}
        for item in raw
    ]


def load_pages(path: Path) -> list[str]:
    return [entry["url"] for entry in _entries(path)]


def load_extractors(path: Path) -> dict[str, str]:
    """`{url: extractor_name}` for the pages that declare one, and no others.

    Fails loud on a name nothing registers rather than dispatching to it later and
    failing on the page read: a typo here should not read as "that page has no
    extractor", the same wrong answer a page shaped differently from what its
    extractor expects would give silently.
    """
    extractors = {}
    for entry in _entries(path):
        name = entry["extractor"]
        if name is None:
            continue
        if name not in _EXTRACTORS:
            raise ValueError(f"{path}: {entry['url']} declares unknown extractor {name!r}")
        extractors[entry["url"]] = name
    return extractors


def fetch(url: str, max_age: float = CACHE_TTL_SECONDS) -> str:
    """The page's HTML, from cache when it was read less than `max_age` ago.

    `max_age=0` is the staleness check reading the site (STU-1094): a record published
    from an hour-old cache is a measurement of the past, and the fan-out that warmed it
    is exactly what runs before somebody asks.
    """
    cached = CACHE_DIR / hashlib.sha256(url.encode()).hexdigest()
    if cached.exists() and time.time() - cached.stat().st_mtime < max_age:
        return cached.read_text(encoding="utf-8")

    request = urllib.request.Request(url, headers={"User-Agent": "email-classifier/1.0"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        html = response.read().decode(response.headers.get_content_charset() or "utf-8",
                                      errors="replace")
    # The page was read; a cache it cannot write to is a local fault, not the site being
    # down, and a caller that catches OSError would report it as the latter (STU-2011).
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_text(html, encoding="utf-8")
    except OSError as exc:
        _log.warning("web cache not written for %s: %s", url, exc)
    return html


def search(query: str, pages: list[str], fetcher=fetch, extractors: dict[str, str] | None = None) -> dict:
    """Every block on the allow-listed pages that contains `query`, with its URL.

    `extractors` is `{url: name}` from `load_extractors` — a page absent from it, or
    passed without the argument at all, gets plain block extraction only.
    """
    needle = fold(query)
    extractors = extractors or {}
    matches: list[dict] = []
    errors: list[dict] = []
    for url in pages:
        try:
            html = fetcher(url)
        except (urllib.error.URLError, OSError) as exc:
            errors.append({"url": url, "error": str(exc)})
            continue
        blocks = to_blocks(html)
        extractor = extractors.get(url)
        if extractor:
            blocks = blocks + _EXTRACTORS[extractor](html)
        seen: set[str] = set()
        for block in blocks:
            folded = fold(block)
            # A block that is only the search word is a navigation label, not an answer.
            if needle not in folded or folded == needle or block in seen:
                continue
            seen.add(block)
            matches.append({"url": url, "passage": block[:PASSAGE_LIMIT]})
    return {"query": query, "matches": matches, "errors": errors, "pages_searched": pages}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True)
    parser.add_argument("--sources", default=str(REPO_ROOT / "rules/web_sources.yaml"))
    args = parser.parse_args()

    sources = Path(args.sources)
    result = search(args.query, load_pages(sources), extractors=load_extractors(sources))
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
