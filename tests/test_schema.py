import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from email_classifier.core.schema import load_many, validate
from email_classifier.core.types import EmailInput
from tests.conftest import FIXTURES_DIR


def test_validate_returns_email_input():
    data = {
        "message_id": "m001",
        "thread_id": "t001",
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {},
        "body_snippet": "Hello",
        "date": "2026-06-10T14:30:00+00:00",
    }
    result = validate(data)
    assert isinstance(result, EmailInput)
    assert result.message_id == "m001"
    assert result.thread_id == "t001"
    assert result.provider_category is None


def test_validate_raises_on_missing_message_id():
    data = {
        "thread_id": "t001",
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {},
        "body_snippet": "Hello",
        "date": "2026-06-10T14:30:00+00:00",
        # message_id missing
    }
    with pytest.raises(ValidationError):
        validate(data)


def test_validate_raises_on_missing_required_field():
    data = {
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {},
        "body_snippet": "Hello",
        "date": "2026-06-10T14:30:00+00:00",
        # thread_id missing
    }
    with pytest.raises(ValidationError):
        validate(data)


def test_validate_accepts_optional_provider_category():
    data = {
        "message_id": "m002",
        "thread_id": "t002",
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {"List-Unsubscribe": "<https://example.com/unsub>"},
        "body_snippet": "Hello",
        "date": "2026-06-10T14:30:00+00:00",
        "provider_category": "promotions",
    }
    result = validate(data)
    assert result.provider_category == "promotions"


def test_load_many_reads_array_dump(tmp_path: Path):
    dump = tmp_path / "dump.json"
    dump.write_text(
        json.dumps(
            [
                {
                    "message_id": "m1",
                    "thread_id": "t1",
                    "from_address": "a@b.com",
                    "subject": "One",
                    "headers": {},
                    "body_snippet": "first",
                    "date": "2026-06-10T14:30:00+00:00",
                },
                {
                    "message_id": "m2",
                    "thread_id": "t2",
                    "from_address": "c@d.com",
                    "subject": "Two",
                    "headers": {"List-Unsubscribe": "<x>"},
                    "body_snippet": "second",
                    "date": "2026-06-11T09:00:00+00:00",
                    "provider_category": "promotions",
                },
            ]
        ),
        encoding="utf-8",
    )
    result = load_many(dump)
    assert [e.message_id for e in result] == ["m1", "m2"]
    assert [e.thread_id for e in result] == ["t1", "t2"]
    assert all(isinstance(e, EmailInput) for e in result)
    assert result[1].provider_category == "promotions"


def test_load_many_raises_on_invalid_element(tmp_path: Path):
    dump = tmp_path / "bad.json"
    dump.write_text(
        json.dumps([{"thread_id": "t1"}]),  # missing required fields
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_many(dump)


def _all_fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.rglob("*.json"))


@pytest.mark.parametrize("path", _all_fixture_paths(), ids=lambda p: p.stem)
def test_fixture_validates_against_schema(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    result = validate(data)
    assert isinstance(result, EmailInput)
    assert result.thread_id
    assert result.from_address
