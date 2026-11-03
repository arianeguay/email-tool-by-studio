import unicodedata

from email_classifier import placeholder


def test_the_marker_is_found_whatever_form_the_accents_took():
    body = unicodedata.normalize("NFD", "Bonjour, [À COMPLÉTER : horaire].")

    assert placeholder.holds_placeholder(body)
    assert not placeholder.holds_placeholder("Bonjour, l'horaire est en ligne.")
    assert not placeholder.holds_placeholder("")


def test_a_sent_reply_still_carrying_a_placeholder_is_flagged_and_alerts(monkeypatch):
    sent = []
    monkeypatch.setattr(placeholder.alerts, "notify", sent.append)

    flagged = placeholder.flag_sent({
        "t2": "Le camp coûte [À COMPLÉTER : prix du camp].",
        "t1": "Le camp coûte 120 $.",
    })

    assert flagged == ["t2"]
    assert len(sent) == 1 and "t2" in sent[0] and "[À COMPLÉTER" in sent[0]


def test_clean_sent_replies_raise_no_alert(monkeypatch):
    sent = []
    monkeypatch.setattr(placeholder.alerts, "notify", sent.append)

    assert placeholder.flag_sent({"t1": "Le camp coûte 120 $."}) == []
    assert sent == []
