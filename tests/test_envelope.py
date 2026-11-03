"""The envelope registry loads what the deployment names (STU-1995)."""

import pytest

from email_classifier import envelope


def test_load_imports_each_declared_module(monkeypatch, tmp_path):
    (tmp_path / "relay_form.py").write_text(
        "from email_classifier import envelope\n"
        "envelope.register(lambda body: envelope.Override('a@b.c', 'A') if 'RELAY' in body else None)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(envelope, "_rewriters", [])
    envelope.load(["relay_form"])
    assert envelope.resolve("RELAY form") == envelope.Override("a@b.c", "A")
    assert envelope.resolve("ordinary mail") is None


def test_a_declared_module_that_does_not_import_raises():
    with pytest.raises(ModuleNotFoundError):
        envelope.load(["no_such_rewriter_module"])
