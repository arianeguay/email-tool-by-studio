"""Registry of envelope rewriters a deployment fills (STU-1463).

An envelope can be a robot: a club's website contact widget is one,
whose notification carries the real sender's address and name as labelled fields
in the body while the From header is a `noreply@` the parent never reads. Replying
to the envelope answers the robot, so the reply target comes from whichever
registered rewriter recognises the body instead.

The notion is general — a form relay, a ticketing system, a mailing-list digest —
even though one deployment supplies the only rewriter today. A deployment adds its own by
calling `register()` at import time and naming that module under the policy's
`envelope_rewriters`; the kernel never names one by module path.
"""

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Override:
    reply_to: str
    name: str | None


Rewriter = Callable[[str], Override | None]

_rewriters: list[Rewriter] = []


def register(rewriter: Rewriter) -> None:
    _rewriters.append(rewriter)


def load(modules: Iterable[str]) -> None:
    """Import each module a deployment declares, which registers its rewriter.

    One that does not import raises: a declared rewriter that never loads would reply
    to every relay's robot instead of the person behind it.
    """
    for name in modules:
        importlib.import_module(name)


def resolve(body: str) -> Override | None:
    """The first registered rewriter that recognises `body`, or None for ordinary mail."""
    for rewriter in _rewriters:
        override = rewriter(body)
        if override is not None:
            return override
    return None
