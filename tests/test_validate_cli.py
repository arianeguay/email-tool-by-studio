import io
import json
import re
from pathlib import Path

import pytest
import yaml

from email_classifier.stage2.validate_cli import main
from email_classifier import deployment

STUDIO = deployment.ROOT / ".studio"

# The validator's own taxonomy, so the payloads below mean the same thing under any
# deployment: the CLI's job is to apply whichever taxonomy it is pointed at.
TAXONOMY = """\
buckets:
  - name: A
    allowed_actions: [keep_visible, needs_reply]
    clamp: keep_visible
    subtypes: [human_real]
  - name: C
    allowed_actions: [label_archive, mark_for_delete]
    subtypes: [ecommerce_merchant]
"""


@pytest.fixture(autouse=True)
def _taxonomy(tmp_path, monkeypatch):
    path = tmp_path / "taxonomy.yaml"
    path.write_text(TAXONOMY, encoding="utf-8")
    monkeypatch.setenv("EMAIL_CLASSIFIER_TAXONOMY", str(path))


def _payload(bucket="C", action="label_archive", subtype="ecommerce_merchant") -> str:
    return json.dumps(
        {
            "thread_id": "t1",
            "bucket": bucket,
            "subtype": subtype,
            "confidence": "high",
            "suggested_action": action,
            "suggested_label": "Newsletters",
            "reasoning": "promo",
        }
    )


def test_cli_valid_payload_exits_zero(capsys):
    code = main(stdin=io.StringIO(_payload()))
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out == {"valid": True, "errors": []}


def test_cli_forbidden_pair_exits_nonzero(capsys):
    code = main(stdin=io.StringIO(_payload(bucket="A", action="label_archive", subtype="human_real")))
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["valid"] is False
    assert any("not allowed for bucket" in e for e in out["errors"])


def test_cli_invalid_json_exits_nonzero(capsys):
    code = main(stdin=io.StringIO("not json"))
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["valid"] is False
    assert out["errors"]


def test_cli_missing_field_exits_nonzero(capsys):
    code = main(stdin=io.StringIO('{"bucket": "A"}'))
    out = json.loads(capsys.readouterr().out)
    assert code == 1
    assert out["valid"] is False


def test_studio_commands_never_invoke_a_bare_python():
    """A host where `python` is only `python3` runs the command not at all (STU-1008).

    The validator failing that way returns non-zero, which the engine reads as the model
    failing the contract — three retries, then the stage gives up.
    """
    bare = re.compile(r"(?<![\w./$-])python(?![\w3])")

    def commands(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("command", "run") and isinstance(value, str):
                    yield value
                else:
                    yield from commands(value)
        elif isinstance(node, list):
            for item in node:
                yield from commands(item)

    offenders = [
        f"{path.relative_to(STUDIO)}: {command.strip()}"
        for path in sorted(STUDIO.rglob("*.yaml"))
        for command in commands(yaml.safe_load(path.read_text()))
        if bare.search(command)
    ]
    assert offenders == []


def test_classification_validator_runs_the_checkout_interpreter():
    contract = yaml.safe_load((STUDIO / "contracts" / "classification.contract.yaml").read_text())
    command = contract["validators"][0]["command"]
    assert command.startswith("${PYTHON:-.venv/bin/python} ")
