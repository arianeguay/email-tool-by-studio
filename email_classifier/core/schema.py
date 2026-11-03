import json
from pathlib import Path

import jsonschema

from email_classifier.core.types import EmailInput

# Provider-neutral input contract. Every field is generic on purpose: no field
# may name or assume a mail provider. Any fetch (Gmail today, Outlook tomorrow)
# must project its raw API response down to exactly these fields. Provider-
# specific vocabulary belongs in that fetch's projection, never here.
_SCHEMA: dict = {
    "type": "object",
    "required": ["message_id", "thread_id", "from_address", "subject", "headers", "body_snippet", "date"],
    "properties": {
        "message_id": {"type": "string"},
        "thread_id": {"type": "string"},
        "from_address": {"type": "string"},
        "subject": {"type": "string"},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "body_snippet": {"type": "string"},
        "date": {"type": "string"},
        # Provider's native category hint, if any. Nullable, low-weight signal.
        "provider_category": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


def validate(data: dict) -> EmailInput:
    jsonschema.validate(data, _SCHEMA)
    return EmailInput(
        message_id=data["message_id"],
        thread_id=data["thread_id"],
        from_address=data["from_address"],
        subject=data["subject"],
        headers=data["headers"],
        body_snippet=data["body_snippet"],
        date=data["date"],
        provider_category=data.get("provider_category"),
    )


def load(path: Path) -> EmailInput:
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate(data)


def load_many(path: Path) -> list[EmailInput]:
    """Load a fetch dump: a JSON array of EmailInput objects, each validated."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [validate(item) for item in data]
