"""Tests for the bucket-mixed fixture selection (STU-845)."""

import json

import pytest

from email_classifier.seed import corpus


def _corpus(tmp_path, layout):
    for bucket, names in layout.items():
        directory = tmp_path / bucket
        directory.mkdir()
        for name in names:
            (directory / f"{name}.json").write_text(
                json.dumps(
                    {
                        "message_id": name,
                        "thread_id": f"t_{name}",
                        "from_address": f"{name}@x.com",
                        "subject": name,
                        "headers": {},
                        "body_snippet": name,
                        "date": "2026-06-01T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
    return tmp_path


def _thread(tmp_path, name, messages):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    for index, (thread_id, date) in enumerate(messages, start=1):
        (directory / f"{index:02d}.json").write_text(
            json.dumps(
                {
                    "message_id": f"{name}_{index:02d}",
                    "thread_id": thread_id,
                    "from_address": "parent@x.com",
                    "subject": name,
                    "headers": {},
                    "body_snippet": name,
                    "date": date,
                }
            ),
            encoding="utf-8",
        )
    return tmp_path


def test_load_buckets_keys_on_directory_name_and_skips_empty_ones(tmp_path):
    (tmp_path / "bucket_empty").mkdir()
    buckets = corpus.load_buckets(_corpus(tmp_path, {"bucket_a": ["a1", "a2"]}))

    assert list(buckets) == ["bucket_a"]
    assert [e.message_id for e in buckets["bucket_a"]] == ["a1", "a2"]


def test_load_buckets_refuses_a_corpus_with_no_bucket(tmp_path):
    with pytest.raises(ValueError, match="no fixture bucket"):
        corpus.load_buckets(tmp_path)


def test_load_threads_keeps_each_conversation_whole_and_in_arrival_order(tmp_path):
    _thread(tmp_path, "suivi", [("t_suivi", "2026-06-01T00:00:00+00:00"),
                                ("t_suivi", "2026-06-03T00:00:00+00:00")])
    _thread(tmp_path, "merci", [("t_merci", "2026-06-02T00:00:00+00:00"),
                                ("t_merci", "2026-06-04T00:00:00+00:00")])

    threads = corpus.load_threads(tmp_path)

    assert [[m.message_id for m in thread] for thread in threads] == [
        ["merci_01", "merci_02"],
        ["suivi_01", "suivi_02"],
    ]


def test_load_threads_skips_a_directory_holding_a_single_flat_message(tmp_path):
    _thread(tmp_path, "flat", [("t_flat", "2026-06-01T00:00:00+00:00")])
    _thread(tmp_path, "real", [("t_real", "2026-06-01T00:00:00+00:00"),
                               ("t_real", "2026-06-02T00:00:00+00:00")])

    assert [thread[0].thread_id for thread in corpus.load_threads(tmp_path)] == ["t_real"]


def test_load_threads_refuses_a_directory_that_is_not_one_conversation(tmp_path):
    """Seeding it would chain two unrelated threads into one mailbox conversation."""
    _thread(tmp_path, "mixed", [("t_a", "2026-06-01T00:00:00+00:00"),
                                ("t_b", "2026-06-02T00:00:00+00:00")])

    with pytest.raises(ValueError, match="spans 2 thread ids"):
        corpus.load_threads(tmp_path)


def test_load_threads_refuses_replies_that_predate_what_they_answer(tmp_path):
    _thread(tmp_path, "backwards", [("t_b", "2026-06-05T00:00:00+00:00"),
                                    ("t_b", "2026-06-01T00:00:00+00:00")])

    with pytest.raises(ValueError, match="not in date order"):
        corpus.load_threads(tmp_path)


def test_load_threads_refuses_a_corpus_with_no_conversation(tmp_path):
    with pytest.raises(ValueError, match="no conversation"):
        corpus.load_threads(tmp_path)


def test_parse_mix_reads_weights_and_rejects_a_malformed_entry():
    assert corpus.parse_mix("bucket_a=40, bucket_c=60") == {"bucket_a": 40.0, "bucket_c": 60.0}
    assert corpus.parse_mix("") == {}
    with pytest.raises(ValueError, match="malformed"):
        corpus.parse_mix("bucket_a")


def test_allocate_sums_exactly_to_the_count():
    allocated = corpus.allocate({"a": 1, "b": 1, "c": 1}, 10)

    assert sum(allocated.values()) == 10
    assert sorted(allocated.values()) == [3, 3, 4]


def test_allocate_refuses_weights_that_sum_to_zero():
    with pytest.raises(ValueError, match="above zero"):
        corpus.allocate({"a": 0}, 10)


def test_select_honours_the_requested_mix(tmp_path):
    buckets = corpus.load_buckets(
        _corpus(tmp_path, {"bucket_a": ["a1", "a2"], "bucket_c": ["c1", "c2"]})
    )

    drawn = corpus.select(buckets, 10, {"bucket_a": 80, "bucket_c": 20})

    assert len(drawn) == 10
    assert sum(1 for bucket, _ in drawn if bucket == "bucket_a") == 8


def test_select_defaults_to_the_corpus_proportions(tmp_path):
    buckets = corpus.load_buckets(
        _corpus(tmp_path, {"bucket_a": ["a1", "a2", "a3"], "bucket_c": ["c1"]})
    )

    drawn = corpus.select(buckets, 4)

    assert sum(1 for bucket, _ in drawn if bucket == "bucket_a") == 3


def test_select_exhausts_a_bucket_before_repeating_a_fixture(tmp_path):
    buckets = corpus.load_buckets(_corpus(tmp_path, {"bucket_a": ["a1", "a2", "a3"]}))

    drawn = corpus.select(buckets, 6)

    counts = {email.message_id for _, email in drawn}
    assert counts == {"a1", "a2", "a3"}
    assert len(drawn) == 6


def test_select_is_reproducible_for_a_given_random_seed(tmp_path):
    buckets = corpus.load_buckets(_corpus(tmp_path, {"bucket_a": ["a1", "a2", "a3"]}))

    first = corpus.select(buckets, 3, seed=7)
    second = corpus.select(buckets, 3, seed=7)

    assert [e.message_id for _, e in first] == [e.message_id for _, e in second]


def test_select_refuses_a_mix_naming_an_unknown_bucket(tmp_path):
    buckets = corpus.load_buckets(_corpus(tmp_path, {"bucket_a": ["a1"]}))

    with pytest.raises(ValueError, match="unknown bucket"):
        corpus.select(buckets, 2, {"bucket_z": 100})
