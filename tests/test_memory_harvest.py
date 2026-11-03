"""Answer blocks harvested from sent replies into the club memory (STU-2001)."""

import json

import pytest

from email_classifier import club_memory, memory_harvest
from email_classifier.fetch.outlook.client import OutlookFetcher
from scripts import memory_harvest as script
from tests.conftest import stub_detector

DATES = ("Voici les dates des tournois de la saison :\n"
         "- Tournoi d'automne : 14 novembre\n"
         "- Coupe régionale : 12 décembre\n"
         "- Championnat : 20 mars")


def _reply(date, body):
    return {"date": f"{date}T15:00:00+00:00", "body": body}


def _with(block, greeting, closing):
    return f"{greeting}\n\n{block}\n\n{closing}"


PASTED = [
    _reply("2026-09-18", _with(DATES, "Bonjour Paul,", "Bonne journée,\nLa coordination")),
    _reply("2026-09-25", _with(DATES, "Allo,", "Merci, à bientôt")),
    _reply("2026-10-06", _with(DATES, "Bonsoir madame,", "Au plaisir")),
    _reply("2026-09-20", "Bonjour,\n\nLe gymnase ouvre à 18 h ce jeudi.\n\nMerci"),
]


def test_a_block_pasted_across_replies_is_one_candidate_with_its_send_dates():
    found = memory_harvest.find(PASTED)

    assert [block["text"] for block in found] == [DATES]
    assert found[0]["replies"] == 3
    assert (found[0]["first_sent"], found[0]["last_sent"]) == ("2026-09-18", "2026-10-06")


def test_text_typed_once_proposes_nothing():
    assert memory_harvest.find([PASTED[3], _reply("2026-09-21", "Oui, samedi midi.")]) == []


def test_a_paste_edited_in_one_reply_keeps_the_block_the_others_agree_on():
    edited = DATES.replace("20 mars", "27 mars")
    found = memory_harvest.find([*PASTED[:2], _reply("2026-10-01", _with(edited, "Bonjour,", "Merci"))])

    assert found[0]["text"] == DATES
    # The edited reply does not carry this text, so it does not count or date it.
    assert (found[0]["replies"], found[0]["last_sent"]) == (2, "2026-09-25")


def test_a_line_that_differs_is_dropped_whole_never_cut_mid_date():
    edited = DATES.replace("20 mars", "27 mars")
    found = memory_harvest.find([
        _reply("2026-09-18", _with(DATES, "Bonjour,", "Merci")),
        _reply("2026-09-19", _with(edited, "Allo,", "Au plaisir")),
    ])

    assert [block["text"] for block in found] == [DATES.rsplit("\n", 1)[0]]


def _decide(directory, verdicts):
    path = directory / memory_harvest.CANDIDATES_NAME
    proposed = json.loads(path.read_text(encoding="utf-8"))
    for candidate in proposed:
        candidate["approve"] = verdicts[candidate["status"], candidate["text"][:10]]
    path.write_text(json.dumps(proposed), encoding="utf-8")


def test_approval_refuses_while_a_candidate_is_undecided(tmp_path):
    memory_harvest.harvest(PASTED, tmp_path)

    with pytest.raises(ValueError, match="voici-les-dates"):
        memory_harvest.approve(tmp_path)
    assert not (tmp_path / club_memory.INDEX_NAME).exists()


def test_an_approved_block_lands_in_the_store_the_drafter_reads(tmp_path):
    memory_harvest.harvest(PASTED, tmp_path)
    _decide(tmp_path, {("new", DATES[:10]): True})

    assert memory_harvest.approve(tmp_path) == ["voici-les-dates-des-tournois-de"]

    front = club_memory.entries(tmp_path)[0]
    assert front["source"] == "pasted in 3 sent replies, 2026-09-18 to 2026-10-06"
    assert front["description"] == "Voici les dates des tournois de la saison :"
    assert DATES in club_memory.read(tmp_path, front["name"])
    assert club_memory.load_index(tmp_path).startswith(f"- [{front['name']}]")
    assert not (tmp_path / memory_harvest.CANDIDATES_NAME).exists()


def test_a_rerun_proposes_only_what_moved_since_the_decision(tmp_path):
    signature = "Bonne journée,\nLa coordination du club de volleyball"
    replies = [_reply(r["date"][:10], f"{r['body']}\n\n{signature}") for r in PASTED[:3]]
    memory_harvest.harvest(replies, tmp_path)
    _decide(tmp_path, {("new", DATES[:10]): True, ("new", signature[:10]): False})
    memory_harvest.approve(tmp_path)

    assert memory_harvest.harvest(replies, tmp_path) == []

    moved = DATES.replace("Championnat : 20 mars", "Championnat : 20 mars\n- Finale : 3 avril")
    rerun = memory_harvest.harvest(
        [_reply(r["date"][:10], r["body"].replace(DATES, moved)) for r in replies], tmp_path)
    assert [(c["status"], c["name"]) for c in rerun] == [("changed", "voici-les-dates-des-tournois-de")]


def test_a_harvested_fact_is_tokenized_when_the_drafter_reads_it(tmp_path, monkeypatch):
    """Harvested facts hold real values; the read path still redacts them (STU-879)."""
    stub_detector(monkeypatch)
    block = "Pour les inscriptions, écrivez à inscriptions.club@example.ca avant le 1er octobre."
    memory_harvest.harvest(
        [_reply("2026-09-18", _with(block, "Bonjour,", "Merci")),
         _reply("2026-09-19", _with(block, "Allo,", "Au plaisir"))], tmp_path)
    _decide(tmp_path, {("new", block[:10]): True})
    [name] = memory_harvest.approve(tmp_path)

    shown = club_memory.for_prompt(club_memory.read(tmp_path, name), {})

    assert "inscriptions.club@example.ca" not in shown
    assert "EMAIL_X" in shown


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, prefer=None):
        self.calls.append((url, params))
        if url.endswith("/messages"):
            return {"value": [{"id": "s1", "receivedDateTime": "2026-09-18T15:00:00Z"}],
                    "@odata.nextLink": "https://next"}
        if url == "https://next":
            return {"value": [{"id": "s2", "receivedDateTime": "2026-09-19T15:00:00Z"}]}
        return {"id": url.rsplit("/", 1)[1], "uniqueBody": {"content": f"body of {url[-2:]}"}}


def test_outlook_lists_sent_items_since_the_floor_and_reads_each_unique_body():
    session = _Session()

    replies = OutlookFetcher(session).sent_since("2026-09-18T00:00:00+00:00")

    assert replies == [{"date": "2026-09-18T15:00:00+00:00", "body": "body of s1"},
                       {"date": "2026-09-19T15:00:00+00:00", "body": "body of s2"}]
    url, params = session.calls[0]
    assert url == "/me/mailFolders('sentitems')/messages"
    assert params["$filter"] == "receivedDateTime ge 2026-09-18T00:00:00Z"


def test_the_command_reads_the_policy_provider_and_writes_the_candidates(tmp_path, monkeypatch, capsys):
    asked = []

    class Reader:
        def sent_since(self, since):
            asked.append(since)
            return PASTED

    monkeypatch.setattr(script, "get_sent_reader", lambda provider: Reader())

    assert script.main(["harvest", "--since", "2026-09-18", "--directory", str(tmp_path)]) == 0

    assert asked == ["2026-09-18T00:00:00+00:00"]
    assert (tmp_path / memory_harvest.CANDIDATES_NAME).exists()
    assert "new      voici-les-dates-des-tournois-de  x3 2026-09-18 to 2026-10-06" in capsys.readouterr().out


def test_the_command_refuses_a_harvest_with_no_floor(tmp_path):
    with pytest.raises(SystemExit):
        script.main(["harvest", "--directory", str(tmp_path)])
