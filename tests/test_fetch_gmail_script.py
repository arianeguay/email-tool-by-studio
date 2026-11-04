"""What `scripts/fetch_gmail.py` asks for when the grant is dead (STU-1163).

The script built its own client and so defaulted to `interactive=True`, where a
grant that no longer redeems blocks forever on `run_local_server` — `make fetch`
indistinguishable from a hang. STU-948 closed that shape for the webhook worker,
but its fix lives in `providers._interactive`, which reads
`EMAIL_CLASSIFIER_HEADLESS`, and a script that imports `email_classifier.fetch.gmail`
directly is not on that path.
"""

import importlib.util
from pathlib import Path

import pytest

from email_classifier import providers
from email_classifier.fetch.grant import GrantLost

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fetch_gmail():
    path = REPO_ROOT / "scripts" / "fetch_gmail.py"
    spec = importlib.util.spec_from_file_location("scripts_fetch_gmail", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def asked(monkeypatch):
    """What interactivity the script asked the Gmail client for, refusing to build one."""
    seen = []

    def _get_credentials(interactive=True, **kwargs):
        seen.append(interactive)
        raise GrantLost("dead grant")

    monkeypatch.setattr(
        "email_classifier.fetch.gmail.get_credentials", _get_credentials
    )
    return seen


def test_a_headless_host_gets_grant_lost_instead_of_a_consent_window(
    fetch_gmail, asked, monkeypatch
):
    monkeypatch.setenv(providers.HEADLESS_ENV, "1")

    with pytest.raises(GrantLost):
        fetch_gmail.main([])

    assert asked == [False]


def test_a_human_at_a_keyboard_still_gets_the_browser(fetch_gmail, asked, monkeypatch):
    monkeypatch.delenv(providers.HEADLESS_ENV, raising=False)

    with pytest.raises(GrantLost):
        fetch_gmail.main([])

    assert asked == [True]


def test_the_default_policy_is_the_deployments_not_the_cwds(
    fetch_gmail, tmp_path, monkeypatch
):
    """STU-2096: the kernel repo has no `rules/` at its root, so a cwd-relative
    default made `make fetch` die there whatever deployment root was set."""
    from email_classifier import deployment

    monkeypatch.setattr(deployment, "ROOT", tmp_path / "deployment")
    monkeypatch.chdir(tmp_path)

    args = fetch_gmail.parse_args([])

    assert args.policy == tmp_path / "deployment" / "rules" / "policy.yaml"
