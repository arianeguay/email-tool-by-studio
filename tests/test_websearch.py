"""The club-site search tool (STU-871). No network: the fetcher is injected."""

import urllib.error
from pathlib import Path

import pytest
import yaml

from email_classifier import deployment, websearch

PAGE = """
<html><head><style>.nav { color: red }</style></head>
<body>
  <nav><a href="/x">Sélections</a></nav>
  <h1>Sélections</h1>
  <p>Les inscriptions aux sélections ouvriront le 19 août.</p>
  <div>Calendrier des sélections (à venir)</div>
  <script>var a = "sélections";</script>
</body></html>
"""


# The federation's calendar page, cut down to the shape that matters: a shell whose
# only dates are in the payload handed to the widget inside a <script> (STU-895).
CALENDAR = """
<html><body>
  <h1>Calendrier des tournois</h1>
  <div id='calendar'></div>
  <script>
    PS.component.microsite_events_displayer('.c6a88', {"CLOSE":"Fermer"},
      {"35464":{"IdEvent":"35464","CalendarName":"Concours de Musique Qu\\u00e9bec",
        "StartTime":"2025-11-01 07:00","EndTime":"2025-11-02 22:00",
        "EventName":"Omnium Musique Qu\\u00e9bec","Location":"Sherbrooke"},
       "35465":{"IdEvent":"35465","CalendarName":"Formations",
        "StartTime":"2025-11-18 18:30","EndTime":"2025-11-18 21:30",
        "EventName":"Atelier pour entra\\u00eeneur","Location":null}});
  </script>
</body></html>
"""


def _fetcher(pages: dict[str, str]):
    def fetch(url: str) -> str:
        if url not in pages:
            raise urllib.error.URLError(f"unreachable: {url}")
        return pages[url]

    return fetch


def test_a_passage_carries_the_url_it_came_from():
    result = websearch.search("inscriptions", ["https://site/a"],
                              _fetcher({"https://site/a": PAGE}))

    assert [m["url"] for m in result["matches"]] == ["https://site/a"]
    assert "19 août" in result["matches"][0]["passage"]


def test_the_query_matches_without_its_accents():
    result = websearch.search("selections", ["https://site/a"],
                              _fetcher({"https://site/a": PAGE}))

    assert result["matches"], "an unaccented query has to find the accented page text"


def test_navigation_labels_and_scripts_are_not_passages():
    passages = [
        m["passage"]
        for m in websearch.search("sélections", ["https://site/a"],
                                  _fetcher({"https://site/a": PAGE}))["matches"]
    ]

    # "Sélections" alone is the menu entry and the heading, twice over; `var a =`
    # is script text the parser must not read as page content.
    assert "Sélections" not in passages
    assert not any("var a" in passage for passage in passages)
    assert len(passages) == len(set(passages))


def test_a_month_name_reaches_a_tournament_date():
    # What the tool owes a parent planning their season: not the word "calendrier",
    # a day and a month they can put in a calendar — for a page declared under the
    # extractor that reads that payload (STU-1471).
    result = websearch.search("novembre", ["https://fede/calendrier"],
                              _fetcher({"https://fede/calendrier": CALENDAR}),
                              extractors={"https://fede/calendrier": "federation_calendar"})

    passages = [m["passage"] for m in result["matches"]]
    assert "1 novembre 2025 au 2 novembre 2025 — Omnium Musique Québec — Sherbrooke" \
        " — Concours de Musique Québec" in passages
    assert "18 novembre 2025 — Atelier pour entraîneur — Formations" in passages


def test_a_calendar_page_with_no_extractor_declared_gets_plain_blocks_only():
    """STU-1471: matching `_EVENTS_PAYLOAD` used to be enough to apply the calendar
    extractor to any page; now only a page `rules/web_sources.yaml` declares it for
    does. A second deployment's page shaped just like the federation's, but undeclared,
    must not have its events surfaced."""
    result = websearch.search("novembre", ["https://fede/calendrier"],
                              _fetcher({"https://fede/calendrier": CALENDAR}))

    assert result["matches"] == []


def test_a_page_without_a_calendar_payload_is_unchanged():
    assert websearch.to_events(PAGE) == []


def test_an_unreachable_page_is_reported_not_raised():
    # A source can go down or move; one dead page must not cost the agent the
    # pages that do answer.
    result = websearch.search("inscriptions", ["https://site/a", "https://dead/"],
                              _fetcher({"https://site/a": PAGE}))

    assert result["matches"]
    assert [e["url"] for e in result["errors"]] == ["https://dead/"]


def test_the_pages_searched_come_back_with_the_result():
    # An empty answer has to be legible as "not on these pages", not as a failure.
    result = websearch.search("remboursement", ["https://site/a"],
                              _fetcher({"https://site/a": PAGE}))

    assert result["matches"] == []
    assert result["pages_searched"] == ["https://site/a"]


def test_an_unknown_extractor_name_is_refused_at_load(tmp_path):
    bad = tmp_path / "web_sources.yaml"
    bad.write_text(
        "pages:\n  - url: https://site/a\n    extractor: made_up\n", encoding="utf-8"
    )

    with pytest.raises(ValueError, match="made_up"):
        websearch.load_extractors(bad)


def test_a_page_declared_with_no_extractor_key_is_the_same_as_a_bare_url(tmp_path):
    mixed = tmp_path / "web_sources.yaml"
    mixed.write_text(
        "pages:\n  - url: https://site/a\n  - https://site/b\n", encoding="utf-8"
    )

    assert websearch.load_pages(mixed) == ["https://site/a", "https://site/b"]
    assert websearch.load_extractors(mixed) == {}


def _studio(relative: str) -> dict:
    root = Path(__file__).resolve().parents[1] / ".studio"
    return yaml.safe_load((root / relative).read_text(encoding="utf-8"))


def test_the_agent_chooses_the_words_and_not_the_destination():
    command = _studio("tools/org-web.tool.yaml")["commands"][0]

    assert set(command["parameters"]) == {"query"}


def test_both_drafting_agents_can_read_the_site():
    # QA verifies a cited URL itself, or it can only take the drafter's word for it.
    for agent in ("drafter", "draft-reviewer"):
        agent_yaml = deployment.ROOT / ".studio" / "agents" / f"{agent}.agent.yaml"
        assert "org_web-search_site" in yaml.safe_load(
            agent_yaml.read_text(encoding="utf-8"))["tools"]


def test_the_contract_leaves_room_for_a_search_before_the_draft():
    tool_calls = _studio("contracts/email-draft.contract.yaml")["tool_calls"]

    # All that is left of the block is a loop guard over the reading tools: the
    # draft is written by a stage now, not by a tool the count could pin (STU-1030).
    assert tool_calls["maximum"] > 1
    assert "required_tools" not in tool_calls


def test_a_source_file_listing_no_page_is_refused(tmp_path):
    empty = tmp_path / "web_sources.yaml"
    empty.write_text("pages: []\n", encoding="utf-8")

    with pytest.raises(ValueError, match="nothing it may read"):
        websearch.load_pages(empty)
