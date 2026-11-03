"""Which provider a QA measurement ran on (STU-1175).

No artifact any of the five QA harnesses wrote named it. Diagnosing STU-1174 needed the
provider of one gate run and the only way to get it was to read the Makefile and reason
about what `defaults.provider` was that day — a number that cannot say what produced it
cannot be checked later.

`cost.leg` already follows this rule and STU-1145 settled why: the provider is recorded
*by the run*, never re-derived from the tree an audit happens to find itself in, because
it resolves as CLI flag over agent field over `defaults.provider` and a tree read weeks
later answers for the tree. The QA harnesses have exactly that shape, so they record it
the same way — and through `provenance.resolved_provider`, which is that resolution
order stated once. A second statement of it here would be a second answer to the same
question, which is what `qa_replay.py` and `coverage_read.py` exist to prevent for the
verdict.

Per agent and not per run, for the reason provenance resolves per agent: the middle term
is an agent naming its own provider, and neither of these two does today. Collapsing it
while it happens to be empty is how the next pin goes unrecorded.
"""

from pathlib import Path

from email_classifier.orchestrate.provenance import resolved_provider

# The agent whose calls each family of harnesses is measuring. `make qa-gate`,
# `make qa-erosion` and `make qa-live-sources` all drive the reviewer through
# `qa_replay`; `make qa-coverage` and `make qa-coverage-gate` drive the reader through
# `coverage_read`. Nothing else in either pipeline calls a model.
REVIEWER = "draft-reviewer"
READER = "coverage-reader"

# The field name, one for every artifact, so a reader of two of them is reading the same
# key. `logs/qa-coverage.json` already carried it holding the *requested* value; this is
# what it holds now.
KEY = "provider"


def resolve(repo_root: Path, agent: str, override: str | None = None) -> str | None:
    """The provider an agent's calls actually ran on, this run.

    `override` is what a `--provider` flag passed, when the harness has one — the
    coverage pair does and the reviewer trio does not. Left unset it is not silence but
    the absence of the first term: resolution falls through to the agent's own pin and
    then to `defaults.provider`, which is what those runs are on.

    None only where the tree could not answer — an unreadable config, a checkout without
    `.studio/`. Recorded as null rather than guessed at, by STU-912's direction: a
    measurement nobody could take must not read as one that came back empty.
    """
    return resolved_provider(repo_root / ".studio", override, agent)


def line(provider: str | None) -> str:
    """The summary's provider sentence, worded once for all five harnesses.

    Beside the tool-health line and for its reason (STU-1147): what answered and what
    ran are the two facts that say whether the numbers under them are about production
    at all, and a human reads them the day they print.
    """
    if provider:
        return f"Measured on provider `{provider}`."
    return (
        "Measured on a provider this tree cannot name: neither the agent nor "
        "`defaults.provider` resolved. What ran is unrecorded, which is not the same "
        "as nothing having run."
    )
