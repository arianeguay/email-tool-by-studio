"""Outlook web mail links -> the id they name.

A link copied out of the browser is the only handle a human has on a
conversation: the ids the pipelines pass around never appear in the UI. Every
Outlook host spells the same path — `.../mail/<folder>/id/<id>` — with the id
percent-encoded, so the segment after `id` is the whole content of the link.

What that id names is NOT decidable here. Outlook writes a conversation id
(`AAQkA...`) into the link when the reading pane shows a thread and a message id
(`AAMkA...`) when it shows one mail, in the same URL shape. The mailbox decides
which it was — see `OutlookFetcher.fetch_input_by_reference`.
"""

from urllib.parse import unquote, urlparse

_ID_SEGMENT = "id"


def message_ref(link: str) -> str:
    """The id an Outlook link points at; a bare id passes through unchanged."""
    if "://" not in link:
        return link
    segments = [segment for segment in urlparse(link).path.split("/") if segment]
    if _ID_SEGMENT not in segments or segments.index(_ID_SEGMENT) == len(segments) - 1:
        raise ValueError(f"no message id in mail link {link!r}")
    return unquote(segments[segments.index(_ID_SEGMENT) + 1])
