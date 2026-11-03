"""Studio `validators` hook entrypoint (STU-390).

The Studio engine pipes the stage output (a candidate classification) as JSON on stdin.
We run the full validate_classification and print {"valid": bool, "errors": [...]} as
JSON, exiting non-zero when invalid. A non-zero exit makes the hook fail, which drives a
RALPH retry with the errors as feedback. Validation runs against the REAL output, so the
agent cannot fake it.

The taxonomy to validate against is loaded relative to the process's own cwd, not to
this file's location (STU-1461): Studio spawns this as a subprocess and runs it with a
cwd of its own choosing — normally the deployment's own project root — which need not
be where the installed `email_classifier` package physically lives (an editable
install pointed at a different checkout, for instance). Resolving off `Path(__file__)`
would risk validating one deployment's output against another's buckets; `Path.cwd()`
is the one signal this process actually has about which deployment it is running for.
`$EMAIL_CLASSIFIER_TAXONOMY` overrides it for a caller that sets its own cwd for other
reasons (a test, a worktree with no `rules/` of its own).
"""

import json
import os
import sys
from pathlib import Path
from typing import TextIO

from email_classifier.core.contract import load_taxonomy, validate_classification

_ENV_TAXONOMY = "EMAIL_CLASSIFIER_TAXONOMY"


def _taxonomy_path() -> Path:
    override = os.environ.get(_ENV_TAXONOMY)
    return Path(override) if override else Path.cwd() / "rules" / "taxonomy.yaml"


def main(stdin: TextIO | None = None) -> int:
    stream = stdin if stdin is not None else sys.stdin
    raw = stream.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"valid": False, "errors": [f"invalid JSON: {exc}"]}))
        return 1

    taxonomy = load_taxonomy(_taxonomy_path())
    result = validate_classification(data, taxonomy)
    print(json.dumps({"valid": result.valid, "errors": result.errors}))
    return 0 if result.valid else 1


if __name__ == "__main__":
    sys.exit(main())
