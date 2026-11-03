"""The name-detection gate (STU-925) and the diffing it is built on.

Two gates share it, over two corpora, for two different questions. The first is recall:
every name the seed corpus carries has to still be found. The second is the claiming
path itself (STU-1054) — whatever `person_spans` does before a bucket is assigned, over
a corpus long enough that the detector's window falls inside a body.

Only the gates need the detector; the comparison is pure and runs everywhere.
"""

from email_classifier.eval import spans
from email_classifier.eval.harness import load_corpus

from tests.conftest import FIXTURES_DIR


def test_a_missing_span_and_an_unexpected_one_are_both_divergences():
    """Over-redaction is a failure too: STU-909 and STU-911 were both that bug."""
    golden = {"m1": {"body_snippet": [spans.Span(0, 6, "Alicia")]}}
    detected = {"m1": {"body_snippet": [spans.Span(10, 17, "Patrick")]}}

    result = spans.compare(detected, golden)

    assert [(d.kind, d.span.text) for d in result.divergences] == [
        ("missing", "Alicia"),
        ("unexpected", "Patrick"),
    ]


def test_a_span_that_moved_is_a_divergence():
    """A count that holds while a span slides is not a gate."""
    golden = {"m1": {"subject": [spans.Span(0, 6, "Alicia")]}}
    detected = {"m1": {"subject": [spans.Span(8, 14, "Alicia")]}}

    assert len(spans.compare(detected, golden).divergences) == 2


def test_a_corpus_and_a_golden_that_do_not_cover_each_other_are_reported():
    result = spans.compare({"m1": {}}, {"m2": {}})

    assert (result.uncovered, result.absent) == (["m1"], ["m2"])


def test_the_golden_round_trips_through_the_file(tmp_path):
    detected = {"m1": {"body_snippet": [spans.Span(0, 6, "Alicia")]}, "m2": {}}
    path = tmp_path / "ner-spans.json"

    spans.write_golden(path, detected)

    assert spans.load_golden(path) == detected


def test_every_field_scope_full_tokenizes_is_scanned(monkeypatch):
    """A header carries no prose today — which is the expectation a name in one breaks."""
    monkeypatch.setattr(spans, "person_spans", lambda text: [(0, len(text))])
    email = load_corpus(FIXTURES_DIR / "bucket_a")[0]

    found = spans.detect(email)

    assert set(found) == {"from_address", "subject", "body_snippet"} | {
        f"headers.{name}" for name in email.headers
    }
