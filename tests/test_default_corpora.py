"""A bare invocation reads the deployment's own corpus and goldens (STU-2014).

Every default below once named a file only the kernel's first deployment had, so the
same command against any other deployment failed on a missing path.
"""

import importlib.util
from pathlib import Path

import pytest

from email_classifier import deployment
from email_classifier.eval.__main__ import parse_args as eval_args

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _script(name):
    spec = importlib.util.spec_from_file_location(f"scripts_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("args, fields", [
    (lambda: _script("classify_bakeoff").parse_args([]), ("corpus", "golden")),
    (lambda: _script("seed_outlook").parse_args(["seed"]), ("corpus",)),
    (lambda: _script("deliver_fixtures").parse_args(["deliver"]), ("corpus",)),
    (lambda: eval_args(["spans"]), ("corpus",)),
], ids=["classify_bakeoff", "seed_outlook", "deliver_fixtures", "eval spans"])
def test_a_bare_invocation_reads_the_deployments_files(args, fields):
    parsed = args()
    for field in fields:
        path = getattr(parsed, field)
        assert path.is_relative_to(deployment.ROOT), (field, path)
        assert path.exists(), (field, path)
