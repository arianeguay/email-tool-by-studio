"""The deployment's own voice baseline, loaded rather than imported (STU-1472)."""

from email_classifier import draft_delta, voice_baseline


def test_a_missing_file_is_null_not_an_error(tmp_path):
    """A fresh deployment with no archive measured yet ships no such file."""
    assert voice_baseline.load(tmp_path / "voice.yaml") is None


def test_an_empty_file_is_also_null(tmp_path):
    path = tmp_path / "voice.yaml"
    path.write_text("", encoding="utf-8")
    assert voice_baseline.load(path) is None


def test_a_deployment_with_no_such_keys_gets_nothing_borrowed():
    """Absent means empty, never another deployment's literals borrowed as a default."""
    for baseline in (None, {}, {"archive": {"replies": 1}}):
        assert not voice_baseline.reply_signature(baseline).match("EMC")
        assert not voice_baseline.reply_signature(baseline).match("")
        assert voice_baseline.quote_markers(baseline) == ()


def test_a_deployment_without_a_signature_strips_no_sign_off(monkeypatch):
    reply = "Bonjour,\n\nLes sélections ont lieu le 14 septembre.\n\nEMC"

    monkeypatch.setattr(draft_delta, "_SIGNATURE",
                        voice_baseline.reply_signature({"reply_signature": r"^EMC$"}))
    assert draft_delta.answer_part(reply) == "Les sélections ont lieu le 14 septembre."
    monkeypatch.setattr(draft_delta, "_SIGNATURE", voice_baseline.reply_signature(None))
    assert draft_delta.answer_part(reply) == (
        "Les sélections ont lieu le 14 septembre. EMC"
    )
