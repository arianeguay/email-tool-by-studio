import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from email_classifier import anonymize
from email_classifier.anonymize import (
    Tokenizer,
    deanonymize,
    load_keymap,
    record_minted,
    run_keymap_path,
    save_keymap,
)
from tests.conftest import requires_gliner, stub_detector
from email_classifier import deployment


@requires_gliner
def test_same_value_gets_the_same_token_across_texts():
    t = Tokenizer()
    first = t.anonymize("Bonjour Marie-Claire, écris à marie@exemple.ca")
    second = t.anonymize("Salut Marie-Claire — voir marie@exemple.ca")
    assert "PERSON_1" in first and "EMAIL_1" in first
    assert second == "Salut PERSON_1 — voir EMAIL_1"


@requires_gliner
def test_distinct_values_get_distinct_counters():
    t = Tokenizer()
    t.anonymize("Bonjour Marie et bonjour Alexandre, à demain")
    assert sorted(t.keymap) == ["PERSON_1", "PERSON_2"]


@requires_gliner
def test_a_name_is_caught_away_from_any_salutation():
    # The whole of STU-890: `Bonjour,` carries a comma, so the salutation anchor looked
    # at the one spot in a French mail where a name never is.
    t = Tokenizer()
    text = "Bonjour,\n\nMa fille Alicia manque la pratique.\n\nBonne journée,\n\nStéphane\n"
    result = t.anonymize(text)
    assert "Alicia" not in result and "Stéphane" not in result
    assert sorted(t.keymap.values()) == ["Alicia", "Stéphane"]


@requires_gliner
def test_a_lowercase_name_is_caught_too():
    t = Tokenizer()
    assert t.anonymize("merci de prévenir marie ce soir") == "merci de prévenir PERSON_1 ce soir"


@requires_gliner
def test_a_compound_name_is_one_span_and_not_a_half_leak():
    # The bake-off's standing caveat: a mis-delimited `Élie-Anne` would tokenize
    # `Élie` and send `-Anne` on to the LLM in the clear.
    for name in ("Élie-Anne", "Jean-François", "Marie-Claude"):
        text = Tokenizer().anonymize(f"Inscrire {name} au camp")
        assert text == "Inscrire PERSON_1 au camp"


@requires_gliner
def test_a_coach_the_skill_names_is_still_a_person():
    # STU-911: the skill names its coaching staff, so `Eugenia` and `Serge` were
    # vouched club vocabulary and the veto sent a parent's mention of one in the clear.
    t = Tokenizer()
    assert t.anonymize("Eugenia m'a dit que la pratique est annulée") == (
        "PERSON_1 m'a dit que la pratique est annulée"
    )


def test_a_role_title_the_mailbox_receives_names_nobody():
    # STU-1032: `Head Coach` and `Assistant #1` come off an English form the mailbox
    # receives, claimed across four messages each. `head` is on the list only as the
    # other half of the title — the veto needs every word of a span.
    assert anonymize._never_names_anybody("Head Coach")
    assert anonymize._never_names_anybody("Assistant #1")
    # `confirmation de joueuse` is claimed as often and is deliberately not here: a form
    # label is not vocabulary a draft may reuse, so the read carries it instead.
    assert not anonymize._never_names_anybody("confirmation de joueuse")


def test_a_name_beside_a_role_title_is_still_a_person():
    # The other half of the same rule: a role word cannot swallow the name next to it,
    # which is what would make widening the list cost a real name.
    assert not anonymize._never_names_anybody("Coach Loiselle")
    assert not anonymize._never_names_anybody("joueuse Charlotte")


def test_a_phrase_describing_a_child_names_nobody():
    # STU-1103: the nine phrases the staging keymap held as people on 2026-08-30. Each
    # was carried through the all-words veto by one unlisted word — `12 à 14 joueuses`
    # by `à` alone, its three other words already refused.
    for value in (
        "celui qui est inscrit",
        "jeune",
        "filles de 5e-6e année",
        "12 à 14 joueuses",
        "4e-5e-6e année",
        "enfant de 12 ans",
    ):
        assert anonymize._never_names_anybody(value), value
    # The three the same read held that are not ordinary words, and so stay redacted:
    # releasing them is the bad direction — `ravenbelaid` is name-shaped, and none of
    # the three splits into a component that would redact French prose afterwards.
    for value in ("sportsclub", "jedi", "ravenbelaid"):
        assert not anonymize._never_names_anybody(value), value


def test_a_family_name_the_singular_role_collides_with_is_still_a_person():
    # `jeune` vetoes only in lowercase, because `Jeune` is a family name and `jeunes`,
    # its plural in `_ROLES`, cannot be one.
    assert not anonymize._never_names_anybody("Jeune Tremblay")
    assert anonymize._never_names_anybody("jeunes")


@requires_gliner
def test_the_sentence_that_minted_two_phrases_survives_in_clear():
    # STU-1103, measured in the fan-out items of round `fb7889ca`: the drafter and the
    # classifier read « Je suis PERSON_33 la recherche […] qui aura 12 PERSON_42 ». Both
    # tokens came off phrases the veto should have refused, and the keymap being shared
    # meant `à` and `ans` were then redacted in every message the host handled after.
    t = Tokenizer()
    text = (
        "Je suis à la recherche d'un groupe puisque mon fils qui aura 11 ans "
        "souhaite commencer le badminton."
    )
    assert t.anonymize(text) == text


@requires_gliner
def test_a_team_designation_is_not_a_person():
    # STU-986: `U15` was on no list, so the veto — which needs every word of the span —
    # let `The U15 girls` through and the club's core vocabulary reached the LLM as an
    # anonymous person token, in both languages and both spellings of the category.
    t = Tokenizer()
    for text in (
        "The U15 girls practise at école Leblanc.",
        "Les filles U15 pratiquent à l'école Leblanc.",
        "Les garçons 15U s'entraînent le mardi.",
    ):
        assert t.anonymize(text) == text


@requires_gliner
def test_a_name_beside_a_team_designation_is_still_tokenized():
    # The other half: the category is un-nameable per word, not a veto of its own, so a
    # span carrying a real name survives it. `Alicia 12U` is the span the detector
    # actually returns, and over-redacting the category there is the cheap direction.
    t = Tokenizer()
    assert t.anonymize("Ma fille Avril joue dans le 12U.") == (
        "Ma fille PERSON_1 joue dans le 12U."
    )
    assert "Alicia" not in t.anonymize("Alicia 12U est absente jeudi.")


# The opening of the bounce run 00b773c4 sent to stage 2 (STU-1499), the recipient's
# address replaced by a synthetic one.
_BOUNCE = (
    "p00-iscream-smtp-54d86dbcf5-4xr2q a rejeté votre message vers les adresses e-mail "
    "suivantes :\r\n\r\nparent@exemple.ca (parent@exemple.ca)<mailto:parent@exemple.ca>\r\n"
    "La boîte aux lettres du destinataire est pleine et ne peut pas accepter de messages "
    "pour l’instant. Essayez de renvoyer votre message ultérieurement ou contactez "
    "directement le destinataire.\r\n\r\n\r\n"
    "p00-iscream-smtp-54d86dbcf5-4xr2q a généré cette erreur :\r\n"
    "<parent@exemple.ca>: user is over quota\r\n"
)


class _ClaimsFirst:
    """A detector that claims only the first occurrence of each of `values`."""

    def __init__(self, *values):
        self._values = values

    def __call__(self):
        return self

    def predict_entities(self, text, labels, threshold):
        return [
            {"start": i, "end": i + len(v), "text": v}
            for v in self._values
            if (i := text.find(v)) >= 0
        ]


def test_a_hostname_is_not_a_person(monkeypatch):
    # STU-1499: the veto read the span word by word, split on hyphens, and `iscream`
    # and `smtp` carry no digit, so a mail server's pod name was minted `PERSON_164`.
    monkeypatch.setattr(
        anonymize, "_model", _ClaimsFirst("p00-iscream-smtp-54d86dbcf5-4xr2q")
    )
    t = Tokenizer()
    assert t.anonymize(_BOUNCE).count("p00-iscream-smtp-54d86dbcf5-4xr2q") == 2
    assert t.keymap == {"EMAIL_1": "parent@exemple.ca"}


_DKIM = (
    "v=1; a=rsa-sha256; c=relaxed/relaxed; d=exemple.ca; s=selector1;\r\n "
    "h=From:Date:Subject:Message-ID; bh=47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=;\r\n "
    "b=dGhpcyBpcyBub3QgYSByZWFsIHNpZ25hdHVyZQ=="
)


def test_a_signature_header_tag_value_is_not_a_person(monkeypatch):
    # STU-1994: the detector claimed `relaxed` off `c=relaxed/relaxed`, minted
    # `PERSON_338`, and every run whose headers carried a signature reported it.
    monkeypatch.setattr(anonymize, "_model", _ClaimsFirst("relaxed", "selector1"))
    assert anonymize.person_spans(_DKIM) == []
    t = Tokenizer()
    assert t.anonymize(_DKIM) == _DKIM
    assert t.keymap == {}


def test_a_name_minted_in_a_window_is_redacted_everywhere_in_it(monkeypatch):
    # STU-1499: the known-value pass only held what earlier windows had minted, so the
    # detector's miss on a second occurrence in the minting window went out in clear.
    monkeypatch.setattr(anonymize, "_model", _ClaimsFirst("Rosalie"))
    t = Tokenizer()
    assert t.anonymize("Rosalie sera absente. Merci de prévenir Rosalie.") == (
        "PERSON_1 sera absente. Merci de prévenir PERSON_1."
    )


@requires_gliner
def test_a_bounce_mints_no_person_for_its_server():
    t = Tokenizer()
    t.anonymize(_BOUNCE)
    assert "p00-iscream-smtp-54d86dbcf5-4xr2q" not in t.keymap.values()


def test_every_coach_the_skill_names_is_declared_a_person():
    # The declaration is what un-vouches them, and a coach added to the body alone
    # would silently fall back to vocabulary — the failure this test exists to make loud.
    body = anonymize.SKILL.read_text(encoding="utf-8").split("\n---\n", 1)[1]
    declared = anonymize._skill_people()
    for name in re.findall(r"coached by ([^.\n]+)", body):
        assert {w.casefold() for w in re.findall(r"[A-Za-zÀ-ÿ']+", name)} <= declared


@requires_gliner
def test_a_month_that_is_also_a_first_name_is_still_a_person():
    # STU-909: the veto used to discard a span for being made of ordinary words, and
    # `Avril` is both a month and an ordinary Quebec first name.
    t = Tokenizer()
    assert t.anonymize("Inscrire Avril au camp") == "Inscrire PERSON_1 au camp"


@requires_gliner
def test_a_calendar_word_in_a_date_is_still_not_a_person():
    # The other half: letting the calendar past the veto must not start tokenizing
    # dates. Position is what separates the two senses, and the detector reads it.
    t = Tokenizer()
    for text in ("Lundi 14 avril, relâche.", "Le tournoi est en mai, pas en avril."):
        assert t.anonymize(text) == text


@requires_gliner
def test_a_common_noun_the_detector_claims_is_left_in_clear():
    # STU-981: read back next to a token, `parents` was claimed and tokenized, and the
    # sentence lost the word it was about.
    t = Tokenizer()
    text = "Le montant peut être donné tel quel aux parents, sans détail."
    assert t.anonymize(text) == text


@requires_gliner
def test_the_same_word_capitalized_is_still_a_family_name():
    # The other half, and the reason the veto reads case: `Parent` is an ordinary
    # Quebec surname, and vouching the word outright would drop it on sight (STU-911).
    t = Tokenizer()
    assert t.anonymize("Mme Parent a confirmé.") == "PERSON_1 a confirmé."


@requires_gliner
def test_a_name_closing_the_text_is_redacted_without_minting_a_token():
    # STU-920: Gmail cuts `snippet` mid-word, so a name flush against the end of
    # the field may be a fragment — `PERSON_9 -> 'Carolin'` next to
    # `PERSON_34 -> 'Caroline'` is one person with two identities.
    t = Tokenizer()
    assert t.anonymize("Merci à l'avance,\n\nCarolin") == "Merci à l'avance,\n\nPERSON_X"
    assert t.keymap == {}


@requires_gliner
def test_the_rule_reads_position_and_not_whether_the_field_was_truncated():
    truncated, whole = Tokenizer(), Tokenizer()
    assert truncated.anonymize("Merci à l'avance,\n\nCarolin") == whole.anonymize(
        "Merci à l'avance,\n\nCaroline"
    )
    assert truncated.keymap == whole.keymap == {}


@requires_gliner
def test_a_name_the_batch_already_knows_keeps_its_token_at_the_end():
    t = Tokenizer()
    t.anonymize("Caroline confirmera la présence")
    assert t.anonymize("Merci à l'avance,\n\nCaroline") == "Merci à l'avance,\n\nPERSON_1"


def test_the_windows_rejoin_into_the_text_they_came_from():
    assert "".join(anonymize._windows("Une phrase. " * 300 + "\n\nFin")) == (
        "Une phrase. " * 300 + "\n\nFin"
    )


@requires_gliner
def test_a_signature_past_the_detector_s_cut_is_still_tokenized():
    # STU-955: GLiNER truncates at 384 wordpieces — around 1350 characters of French —
    # and reports no entity past the cut while returning normally, so the sign-off, the
    # one place the sender's own name is, reached the LLM in the clear on a body of any
    # ordinary club length.
    t = Tokenizer()
    filler = "Nous avons plusieurs questions sur les horaires et les frais. " * 90
    result = t.anonymize(
        f"Bonjour,\n\n{filler}\n\nSigné, Jean-Guy Bouchard, parent de Alicia Bouchard."
    )
    assert len(result) > 5000
    for name in ("Jean-Guy", "Bouchard", "Alicia"):
        assert name not in result
    # Every window but the last ends mid-body, where nothing was cut: the end-of-text
    # rule firing on those boundaries would spend one unindexed token on each.
    assert "PERSON_X" not in result


@requires_gliner
def test_a_seeded_token_is_not_handed_back_to_a_name_closing_the_text():
    # STU-922: the fragments a pre-STU-920 run minted are on disk, and seeding
    # `_inverse` from them gives `Carolin` back `PERSON_9` on every later run.
    t = Tokenizer({"PERSON_9": "Carolin", "PERSON_34": "Caroline"})
    assert t.anonymize("Merci à l'avance,\n\nCarolin") == "Merci à l'avance,\n\nPERSON_X"
    assert t.anonymize("Merci à l'avance,\n\nCaroline") == "Merci à l'avance,\n\nPERSON_X"
    # Mid-text the seeded entry is still the point of persisting one.
    assert t.anonymize("Caroline confirmera") == "PERSON_34 confirmera"


def test_the_phrases_the_staging_keymap_held_are_pruned_on_load():
    # STU-1103: the six entries of the 2026-08-30 read the widened veto now refuses.
    # Nothing removes them by hand — STU-922 built this filter, and the hand-removal it
    # also did was the one-off cost of a rule that had not existed when they were minted.
    # So the first run after deploy writes the shared keymap back without them.
    seeded = {
        "PERSON_24": "Victoria Doudak",
        "PERSON_25": "celui qui est inscrit",
        "PERSON_29": "jeune",
        "PERSON_32": "filles de 5e-6e année",
        "PERSON_33": "12 à 14 joueuses",
        "PERSON_34": "4e-5e-6e année",
        "PERSON_42": "enfant de 12 ans",
        "PERSON_49": "Maya",
    }
    t = Tokenizer(seeded)
    assert t.keymap == {"PERSON_24": "Victoria Doudak", "PERSON_49": "Maya"}
    # Six numbers freed, and none of them reused: the counter advances over the pruned.
    assert t.token_for("Adaniel Hoxha", "person") == "PERSON_50"


def test_a_pruned_phrase_stops_offering_its_words_as_components():
    # What made a bad mint compound: STU-1060 redacts a word of a multi-word value on
    # its own, so `12 à 14 joueuses` handed `à` a person token in every later message.
    # The component pass reads the same veto, so widening it closes both at once.
    assert "à" not in Tokenizer({"PERSON_33": "12 à 14 joueuses"})._components()
    assert "ans" not in Tokenizer({"PERSON_42": "enfant de 12 ans"})._components()
    # And the half of STU-1060 that must not move with it.
    assert Tokenizer({"PERSON_1": "Élise Boucher"})._components()["Élise"] == "PERSON_1"


@requires_gliner
def test_a_truncated_address_closing_the_text_is_still_tokenized():
    t = Tokenizer()
    assert t.anonymize("écrivez à marie@exemple.ca") == "écrivez à EMAIL_1"


@requires_gliner
def test_an_email_address_stays_one_token_and_not_a_name_inside_it():
    t = Tokenizer()
    assert t.anonymize("écris à marie.tremblay@exemple.ca") == "écris à EMAIL_1"


def _huggingface_hub(monkeypatch):
    """`huggingface_hub.constants` — the real one, or a stub of the two names `_model` reads.

    `huggingface_hub` is not a dependency of this project: it arrives as a transitive of
    `gliner`, so without the `ner` extra the fixture below has nothing to read the live
    `HF_HUB_OFFLINE` global off, and nothing to raise as a cache miss.

    The stub is a fallback and never a shadow: on a machine that *has* the extra these
    tests still run against `huggingface_hub` itself, so a rename of the exception —
    which is exactly what STU-990 had to measure `LocalEntryNotFoundError` against —
    fails there instead of passing against a stub that agrees with the old name.
    """
    if importlib.util.find_spec("huggingface_hub") is not None:
        import huggingface_hub.constants as hub

        return hub

    class LocalEntryNotFoundError(Exception):
        """Stands in for the one exception a miss raises; `_model` catches it by name."""

    package = ModuleType("huggingface_hub")
    package.constants = ModuleType("huggingface_hub.constants")
    package.constants.HF_HUB_OFFLINE = False
    package.errors = ModuleType("huggingface_hub.errors")
    package.errors.LocalEntryNotFoundError = LocalEntryNotFoundError
    for name, module in (
        ("huggingface_hub", package),
        ("huggingface_hub.constants", package.constants),
        ("huggingface_hub.errors", package.errors),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return package.constants


def _cache_miss(reason="not in cache"):
    """What `from_pretrained` raises when the weights are absent, offline or online."""
    from huggingface_hub.errors import LocalEntryNotFoundError

    return LocalEntryNotFoundError(reason)


def _out_of_memory():
    """`torch.OutOfMemoryError` without the extra: a RuntimeError, as torch's is."""
    return RuntimeError("CUDA error: out of memory")


@pytest.fixture
def gliner_loads(monkeypatch):
    """Stand in for the detector's load, reporting the offline flag each attempt saw.

    The point of the fixture is that the load path is testable with no weights and no
    network: `install(...)` takes one outcome per expected `from_pretrained` call, an
    exception standing for a cache miss, and returns what each call saw — the offline
    flag and the device. `torch`, `gliner` and — when the extra is absent — the two
    `huggingface_hub` names the load reads are stubbed, so the suite still runs without
    the `ner` extra (STU-857, STU-994).
    """
    hub = _huggingface_hub(monkeypatch)

    def install(*outcomes, cuda=False):
        seen: list[bool] = []
        devices: list[str] = []

        def from_pretrained(name, map_location):
            seen.append(hub.HF_HUB_OFFLINE)
            devices.append(map_location)
            outcome = outcomes[len(seen) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(hub, "HF_HUB_OFFLINE", False)
        # `stay_on_the_cpu` is process-wide and one-way, so it is restored here too.
        monkeypatch.setattr(anonymize, "_cpu_only", False)
        monkeypatch.setitem(
            sys.modules,
            "gliner",
            SimpleNamespace(GLiNER=SimpleNamespace(from_pretrained=from_pretrained)),
        )
        monkeypatch.setitem(
            sys.modules,
            "torch",
            SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: cuda)),
        )
        return seen, devices

    anonymize._model.cache_clear()
    yield install
    anonymize._model.cache_clear()


def test_a_warm_cache_loads_without_reaching_the_network(gliner_loads):
    # STU-638: the hub check blocks on a socket even when the weights are already
    # there, so a slow hub hangs a classification with nothing looking busy.
    import huggingface_hub.constants as hub

    seen, _ = gliner_loads("detector")
    assert anonymize._model() == "detector"
    assert seen == [True]
    assert hub.HF_HUB_OFFLINE is False, "the flag is global; leaving it set breaks the pull"


def test_a_cache_miss_still_pulls_from_the_hub(gliner_loads):
    # STU-731's half: an offline probe that fails must not mean the model never loads.
    seen, _ = gliner_loads(_cache_miss(), "detector")
    assert anonymize._model() == "detector"
    assert seen == [True, False]


def test_a_present_gpu_is_where_the_weights_land(gliner_loads):
    # STU-957: `map_location` defaults to the CPU, so without this the RTX sits idle
    # while `predict_entities` runs — an hour of wall clock over the 1275-message archive.
    _, devices = gliner_loads("detector", cuda=True)
    anonymize._model()
    assert devices == ["cuda"]


def test_no_gpu_needs_no_configuration(gliner_loads):
    _, devices = gliner_loads(_cache_miss(), "detector", cuda=False)
    anonymize._model()
    assert devices == ["cpu", "cpu"], "a CPU-only machine must load exactly as before"


def test_a_one_shot_process_declines_the_gpu(gliner_loads):
    # STU-984: the club-memory tool loads the weights to tokenize one file and exits.
    # Measured, that is 12.0 s on the GPU against 9.9 s on the CPU — and it puts a
    # second copy on the card its caller is already holding one on.
    _, devices = gliner_loads("detector", cuda=True)
    anonymize.stay_on_the_cpu()
    anonymize._model()

    assert devices == ["cpu"]


def test_a_gpu_too_full_for_the_weights_falls_back_to_the_cpu(gliner_loads, caplog):
    # STU-984: the club-memory tool shells out mid-run, so a second copy of the model
    # lands on the same 6 GB card as the caller's and the child dies with `CUDA error:
    # out of memory`. The GPU is an optimization; a slower detector beats no detector.
    _, devices = gliner_loads(_out_of_memory(), "detector", cuda=True)
    with caplog.at_level("WARNING"):
        assert anonymize._model() == "detector"

    assert devices == ["cuda", "cpu"]
    assert "out of memory" in caplog.text, "a silent fallback hides a permanently dead GPU"


def test_weights_absent_everywhere_is_a_readable_refusal(gliner_loads):
    # An unreachable hub raises the same error as an empty cache — measured — so both
    # clauses of the message are true whichever half failed.
    gliner_loads(_cache_miss(), _cache_miss("no route to host"))
    with pytest.raises(RuntimeError, match="local Hugging Face cache"):
        anonymize._model()


def test_a_gpu_does_not_turn_a_cache_miss_into_a_gpu_story(gliner_loads):
    """The CPU attempt has the last word, so the refusal still names the real cause."""
    gliner_loads(*[_cache_miss()] * 4, cuda=True)
    with pytest.raises(RuntimeError, match="local Hugging Face cache"):
        anonymize._model()


def test_a_full_card_is_not_reported_as_a_missing_cache(gliner_loads):
    """STU-990: the CPU load's failure is re-labelled, and OOM wore the wrong label.

    Every clause of the missing-cache message is false of an OOM — the weights are
    there, the pull succeeded, and the reinstall it asks for changes nothing. STU-987
    spent its diagnosis on the chained `__cause__` because the top line pointed away
    from the cause.
    """
    oom = _out_of_memory()
    seen, _ = gliner_loads(oom, "detector")
    with pytest.raises(RuntimeError) as raised:
        anonymize._model()

    assert raised.value is oom, "an OOM reaches the caller as itself, not as a cache miss"
    assert seen == [True], "a second load cannot free VRAM the first one could not fit"


def test_person_detection_refuses_to_run_without_the_extra(monkeypatch):
    # The failure this replaces was silent: a run that looked anonymized while every
    # name in it went to the LLM in cleartext. Absent detector must stop the run.
    monkeypatch.setitem(sys.modules, "gliner", None)
    anonymize._model.cache_clear()

    with pytest.raises(RuntimeError, match="ner"):
        Tokenizer().anonymize("Bonjour Marie")


def test_persons_false_leaves_the_format_categories_alone():
    t = Tokenizer(persons=False)
    assert t.anonymize("Marie au 514-555-1234") == "Marie au PHONE_1"


@pytest.mark.parametrize("written", [
    "4023827847",
    "+14023827847",
    'Fichier : "vmail_4023827847_302_20260903.wav"',
])
def test_ten_bare_digits_in_phone_shape_are_tokenized(written):
    # STU-1501: a 3CX voicemail wrote the caller's number with no separators.
    t = Tokenizer(persons=False)
    out = t.anonymize(f"De : {written} fin")
    assert "4023827847" not in out
    assert "PHONE_1" in out


@pytest.mark.parametrize("digits", [
    "0123456789",       # a NANP area code never starts with 0 or 1
    "4021827847",       # nor an exchange
    "40238278470",      # longer runs are not this number with a digit stuck on
    "140238278470123",
])
def test_a_digit_run_that_is_not_a_phone_is_left_alone(digits):
    t = Tokenizer(persons=False)
    text = f"Commande {digits} confirmée"
    assert t.anonymize(text) == text


def test_credit_card_beats_the_overlapping_ssn_and_phone_patterns():
    t = Tokenizer(persons=False)
    assert t.anonymize("carte 4111 1111 1111 1111 fin") == "carte CREDIT_CARD_1 fin"


def test_a_machine_sender_reaches_the_model_in_clear():
    # STU-902: the token was read as the phishing signal, so a vendor account notice
    # landed in bucket D — which apply is allowed to delete.
    t = Tokenizer(persons=False)
    text = "no-reply@amazonaws.com et noreply@club.example et marie@exemple.ca"
    assert t.anonymize(text) == (
        "no-reply@amazonaws.com et noreply@club.example et EMAIL_1"
    )
    assert t.keymap == {"EMAIL_1": "marie@exemple.ca"}
    assert deanonymize(t.anonymize(text), t.keymap) == text


@requires_gliner
def test_a_machine_sender_is_not_claimed_by_the_person_detector():
    # Dropping the address from the keymap must not drop it from the resolved spans:
    # an unshielded `no-reply@club.example` is a name waiting to be found inside.
    t = Tokenizer()
    text = "Bonjour,\n\nÉcrit par no-reply@club.example pour Alicia."
    result = t.anonymize(text)
    assert "no-reply@club.example" in result
    assert list(t.keymap.values()) == ["Alicia"]


def test_a_role_local_part_is_matched_past_its_separators_and_tag():
    for address in ("do.not.reply@x.com", "No_Reply@x.com", "notifications+42@x.com"):
        assert anonymize.is_machine_address(address)
    for address in ("marie@x.ca", "info.marie@x.ca", "supporter@x.ca"):
        assert not anonymize.is_machine_address(address)


# --- a word that names the sender's own domain, not a person (STU-1273) -----------


def test_domain_labels_reads_the_domain_only():
    assert anonymize._domain_labels("no_reply@emagine.org") == {"emagine", "org"}
    assert anonymize._domain_labels("no address here") == frozenset()
    assert anonymize._domain_labels(None) == frozenset()


def test_domain_labels_declines_a_personal_sender():
    # julie@dupont.com is a real person's own address, not a role address: her domain
    # says nothing about who "Dupont" is elsewhere in the body, and vetoing on it would
    # refuse a real surname that happens to share her family's own domain.
    assert anonymize._domain_labels("julie@dupont.com") == frozenset()


def test_a_word_that_only_names_its_own_domain_is_not_claimed():
    # The real leak: PERSON_22 = "emagine", minted from no_reply@emagine.org.
    assert anonymize._never_names_anybody(
        "emagine", anonymize._domain_labels("no_reply@emagine.org")
    )
    # A real name is not excused just because the same message also names its domain.
    assert not anonymize._never_names_anybody(
        "Marie Tremblay", anonymize._domain_labels("no_reply@emagine.org")
    )


@requires_gliner
def test_a_product_named_after_its_own_sending_domain_is_not_a_person():
    # STU-1273: reproduces this laptop's own keymap leak, PERSON_22 = "emagine", minted
    # from a message sent by no_reply@emagine.org.
    text = "emagine (formerly Netplus Sport) switches IT systems – important information"
    assert anonymize.person_spans(text) != []
    assert anonymize.person_spans(text, from_address="no_reply@emagine.org") == []


def test_the_domain_veto_reaches_the_tokenizer(monkeypatch):
    """A detector that claims "emagine" mints it without context, not with the sender."""
    monkeypatch.setattr(
        anonymize, "_model",
        lambda: SimpleNamespace(
            predict_entities=lambda text, labels, threshold: (
                [{"start": 0, "end": len("emagine"), "text": "emagine"}]
            )
        ),
    )
    text = "emagine a facturé le club"
    assert Tokenizer().anonymize(text) == "PERSON_1 a facturé le club"
    assert Tokenizer().anonymize(text, from_address="no_reply@emagine.org") == text


def test_deanonymize_restores_the_original_text():
    t = Tokenizer(persons=False)
    text = "Marie-Claire, 514-555-1234"
    assert deanonymize(t.anonymize(text), t.keymap) == text


def test_deanonymize_replaces_the_longest_token_first():
    keymap = {f"EMAIL_{n}": f"a{n}@x.ca" for n in range(1, 12)}
    assert deanonymize("EMAIL_11", keymap) == "a11@x.ca"


def test_a_seeded_tokenizer_reuses_tokens_and_keeps_counting():
    t = Tokenizer({"EMAIL_1": "marie@x.ca"}, persons=False)
    assert t.anonymize("écris à marie@x.ca") == "écris à EMAIL_1"
    assert t.anonymize("ou à luc@x.ca") == "ou à EMAIL_2"


def test_keymap_round_trips_through_disk(tmp_path):
    path = tmp_path / "nested" / "keymap.json"
    save_keymap(path, {"PERSON_1": "Marie"})
    assert load_keymap(path) == {"PERSON_1": "Marie"}


def test_an_unreadable_keymap_starts_a_fresh_one(tmp_path):
    path = tmp_path / "keymap.json"
    path.write_text("not json", encoding="utf-8")
    assert load_keymap(path) == {}
    assert load_keymap(tmp_path / "absent.json") == {}


def test_minted_holds_what_this_batch_tokenized_not_what_it_was_seeded_with():
    """STU-924: the shared keymap is the machine's accumulated state, so an audit
    reading it attributes every earlier run's entries to the one it is auditing."""
    t = Tokenizer({"EMAIL_1": "ancienne@x.ca"}, persons=False)
    t.anonymize("écrire à nouvelle@x.ca")

    assert t.keymap == {"EMAIL_1": "ancienne@x.ca", "EMAIL_2": "nouvelle@x.ca"}
    assert t.minted() == {"EMAIL_2": "nouvelle@x.ca"}


def test_a_value_carried_in_stays_carried_in_when_the_batch_sees_it_again():
    """It keeps the seeded token, so the run did not mint it — counting it would put
    the entry on every run that ever mentioned that address."""
    t = Tokenizer({"EMAIL_1": "parent@x.ca"}, persons=False)
    assert t.anonymize("écrire à parent@x.ca") == "écrire à EMAIL_1"
    assert t.minted() == {}


def test_the_run_keymap_merges_the_stages_that_write_under_one_run_id(tmp_path):
    """`mailbox-full` tokenizes twice under one run id — classify-batch, then drafts.
    The second seeds from the shared keymap, so overwriting would drop the first."""
    path = run_keymap_path(tmp_path / "keymap.json", "run-1")
    record_minted(path, {"EMAIL_2": "nouvelle@x.ca"})
    record_minted(path, {"PERSON_5": "Marie"})

    assert path == tmp_path / "run-1.json"
    assert load_keymap(path) == {"EMAIL_2": "nouvelle@x.ca", "PERSON_5": "Marie"}


def test_a_name_the_keymap_already_holds_is_tokenized_without_the_detector(monkeypatch):
    # STU-1046: the detector's recall depends on how much text surrounds a name, so a
    # first name it claimed in one message reached the LLM in clear in the salutation of
    # the next. A value already in the keymap is a fact and does not need re-deriving.
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_7": "Sylvain"})
    assert t.anonymize("Bonjour Sylvain, merci") == "Bonjour PERSON_7, merci"


def test_a_known_name_is_matched_on_whole_words_and_on_case(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_1": "Marc"})
    # `marc` is an ordinary French noun and `Marcel` is somebody else.
    assert t.anonymize("Marcel a jeté le marc de café") == "Marcel a jeté le marc de café"


def test_an_address_holding_a_known_name_stays_one_email_token(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_1": "Sylvain"})
    assert t.anonymize("écris à Sylvain@exemple.ca") == "écris à EMAIL_1"


def test_a_first_name_the_keymap_holds_only_inside_a_full_name(monkeypatch):
    # STU-1060: a contact-form notification writes the parent's name in full at the top
    # and signs off with the first name alone; the whole-value match reaches neither.
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_3": "Élise Boucher"})
    assert t.anonymize("Élise Boucher a écrit. Merci, Élise") == (
        "PERSON_3 a écrit. Merci, PERSON_3"
    )


def test_a_component_carries_the_whole_value_token_rather_than_its_own(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_3": "Élise Boucher"})
    t.anonymize("Merci, Élise")
    # One person, one identity for as long as the keymap is carried (STU-920).
    assert t.minted() == {}
    assert t.keymap == {"PERSON_3": "Élise Boucher"}


def test_a_component_two_people_share_names_neither_of_them(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_1": "Élise Boucher", "PERSON_2": "Élise Tremblay"})
    assert t.anonymize("Merci, Élise") == "Merci, PERSON_X"
    assert t.anonymize("Élise Tremblay a écrit") == "PERSON_2 a écrit"


def test_a_component_is_matched_on_whole_words_and_on_case(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_1": "Marc Boucher"})
    assert t.anonymize("Marcel a jeté le marc de café") == "Marcel a jeté le marc de café"


def test_an_address_holding_a_component_stays_one_email_token(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_1": "Élise Boucher"})
    assert t.anonymize("écris à Boucher@exemple.ca") == "écris à EMAIL_1"


def test_a_component_that_never_names_anybody_is_not_claimed(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_1": "Ma Boucher"})
    assert t.anonymize("Ma fille joue avec Boucher") == "Ma fille joue avec PERSON_1"


def test_a_lowercase_connector_inside_a_person_value_is_not_claimed(monkeypatch):
    # STU-1762: a span that was two names joined by "ou" turned every later "ou"
    # into a person token.
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_112": "Alex ou Caro"})
    assert t.anonymize("Vous pouvez venir samedi ou dimanche") == (
        "Vous pouvez venir samedi ou dimanche"
    )
    assert t.anonymize("Demandez à Alex ou Caro.") == "Demandez à PERSON_112."


def test_an_all_lowercase_person_value_still_offers_its_words(monkeypatch):
    stub_detector(monkeypatch)
    t = Tokenizer({"PERSON_1": "élise boucher"})
    assert t.anonymize("merci, élise.") == "merci, PERSON_1."


def test_a_lowercase_phrase_in_the_keymap_offers_no_word_as_a_name():
    # STU-2016: a bounce's role phrase was minted a person, and its `partie` then
    # redacted every later "fait partie".
    t = Tokenizer({"PERSON_237": "administrateur de courrier de la partie responsable"})
    text = "ne fait pas partie de la sélection"
    assert t.redact_known(text) == text


# --- the signature block a suppressed detector drops (STU-1224) -------------------

_SIGNED = (
    "Hello I registered my daughter , Ysolde Marchetti-Vang for the fall session.\n\n"
    "I just want to be sure her place is confirmed.\n\n"
    "Thank you\n\nOttavia\n514-555-0148\nSent from my iPhone"
)


def _signed(name_block: str) -> str:
    return f"Bonjour,\n\nMa fille joue au U15.\n\n{name_block}"


def test_a_name_between_a_closing_and_a_phone_is_claimed_without_the_model():
    spans = anonymize._signature_spans(_SIGNED)
    assert [_SIGNED[start:end] for start, end, _ in spans] == ["Ottavia"]


@pytest.mark.parametrize("footer", [
    "Sent from my iPhone", "Envoyé de mon iPhone", "Get Outlook for Android",
])
def test_a_mail_client_footer_marks_the_block_as_well_as_a_phone(footer):
    text = _signed(f"Merci\n\nOttavia\n{footer}")
    assert [text[s:e] for s, e, _ in anonymize._signature_spans(text)] == ["Ottavia"]


def test_a_name_ending_the_text_is_a_signature_when_a_closing_precedes_it():
    text = _signed("Cordialement,\n\nOttavia")
    assert [text[s:e] for s, e, _ in anonymize._signature_spans(text)] == ["Ottavia"]


def test_a_capitalized_line_with_no_closing_above_names_nobody():
    """The either-side rule adds 146 spans to the archive against the strict rule's 4,
    and pushes the published ceiling 29.8% -> 35.5%: a venue or a team on its own line
    is a line, not a signature."""
    text = "Bonjour,\n\nLa pratique est déplacée.\n\nGymnase Leblanc\n514-555-0148"
    assert anonymize._signature_spans(text) == []


def test_a_closing_with_ordinary_prose_below_names_nobody():
    text = _signed("Merci\n\nOttavia\nJe rappelle demain pour confirmer le groupe.")
    assert anonymize._signature_spans(text) == []


def test_a_word_that_never_names_anybody_is_not_a_signature():
    text = _signed("Merci\n\nBonjour\n514-555-0148")
    assert anonymize._signature_spans(text) == []


def test_a_line_of_four_capitalized_words_is_a_title_and_not_a_name():
    text = _signed("Merci\n\nClub De Musique Rivière\n514-555-0148")
    assert anonymize._signature_spans(text) == []


@requires_gliner
def test_the_signature_survives_a_full_name_that_suppresses_it():
    """A confident full name earlier scores this signature 0.15, under the 0.3 threshold,
    and it reached stage 2 in clear on live mail (STU-1224)."""
    assert "Ottavia" in {_SIGNED[s:e] for s, e in anonymize.person_spans(_SIGNED)}


@requires_gliner
def test_the_signature_name_is_tokenized_and_not_sent_in_clear():
    redacted = anonymize.Tokenizer().anonymize(_SIGNED)
    assert "Ottavia" not in redacted
    assert "Ysolde" not in redacted


# --- a child's name the parent's signature suppresses in prose (STU-1993) ---------

# Synthetic bodies in the shape that reached stage 2 with the child's name in clear:
# the signature's confident full name holds the child under 0.3 in each (0.21, 0.18
# and 0.15 alone, measured 2026-11-02).
_SUPPRESSED = [
    ("Bonjour,\r\n\r\nMerci pour cette super nouvelle !\r\nJe l'annonce à Léonie "
     "demain.  C'est pour intégrer l'équipe 1?\r\n\r\nAvant de confirmer, nous "
     "aimerions connaître le gymnase des entraînements.\r\n\r\nMerci,\r\n\r\n"
     "Nathalie Bergeron\r\n", "Léonie"),
    ("Allo,\r\n\r\nMerci de nous prévenir si des places se libèrent . Lou aimerait "
     "beaucoup rejoindre le groupe et s'est fait beaucoup d'amis ces dernières années "
     "avec plusieurs jeunes du groupe .\r\n\r\nMerci de considérer sa demande."
     "\r\n\r\n\r\nGeneviève Lacasse\r\nGraphiste\r\n"
     "www.atelierplume.ca<http://www.atelierplume.ca/>\r\n(450) 555-0187\r\n", "Lou"),
    ("Bonsoir,\r\n\r\nC réglé\r\n Lou Lafrenière,  centre\r\n\r\nMerci\r\n\r\n"
     "[https://ci3.googleusercontent.com/mail-sig/"
     "AIorK4xQ9vT2bLm8Zr_PkW3sY7nDe5HcQ1aJf0XoUg4Vt6Rb]"
     "\r\n\r\n\r\nNathalie Bergeron\r\n\r\nAnalyste sénior\r\n\r\n"
     "C : (514) 555-0142\r\n", "Lou"),
]


class _SuppressedBySignature:
    """Claims the signature; claims the child only once the signature is masked."""

    def predict_entities(self, text, labels, threshold):
        spans = [("Nathalie Bergeron", 0.95)] if "Nathalie Bergeron" in text else []
        if "Nathalie Bergeron" not in text:
            spans += [("Léonie", 0.8), ("xxxxxxxx", 0.5), ("Avant de confirmer", 0.4)]
        return [
            {"start": text.find(word), "end": text.find(word) + len(word), "text": word,
             "score": score}
            for word, score in spans if word in text
        ]


def test_a_second_pass_with_the_claims_masked_finds_what_they_suppressed(monkeypatch):
    monkeypatch.setattr(anonymize, "_model", _SuppressedBySignature)
    text = _SUPPRESSED[0][0]
    assert {text[s:e] for s, e, _ in anonymize._local_person_spans(text)} == {
        "Nathalie Bergeron", "Léonie",
    }


def test_nothing_claimed_means_no_second_pass(monkeypatch):
    calls = []

    class Counts:
        def predict_entities(self, text, labels, threshold):
            calls.append(text)
            return []

    monkeypatch.setattr(anonymize, "_model", Counts)
    anonymize._local_person_spans("Bonjour, merci.")
    assert len(calls) == 1


class _ClaimsTheSignOffToo:
    def predict_entities(self, text, labels, threshold):
        return [
            {"start": m.start(), "end": m.end(), "text": m[0], "score": 0.9}
            for m in re.finditer(r"Élise Boucher|Élise\Z", text)
        ]


def test_a_sign_off_claimed_beside_the_full_name_keeps_that_name_s_token(monkeypatch):
    """Ending the text, a claimed first name is the STU-920 fragment case; when a full
    name in the same window holds it, it is that person and keeps their token."""
    monkeypatch.setattr(anonymize, "_model", _ClaimsTheSignOffToo)
    redacted = Tokenizer().anonymize("Nom: Élise Boucher\n\nMerci, Élise")
    assert redacted == "Nom: PERSON_1\n\nMerci, PERSON_1"


@requires_gliner
@pytest.mark.parametrize("text,child", _SUPPRESSED)
def test_a_child_named_beside_a_parent_s_signature_is_claimed(text, child):
    assert any(child in text[s:e] for s, e in anonymize.person_spans(text))


def test_format_spans_reports_what_outranks_the_detector():
    """`_PRIORITY` puts every format ahead of `person`, so an occurrence inside one of
    these is replaced before the detector is consulted. A measurement counting a name the
    detector declined has to know which were already gone (STU-1264).
    """
    text = "Écrire à marie.tremblay@example.com ou appeler Marie Tremblay."
    spans = anonymize.format_spans(text)
    assert [text[s:e] for s, e in spans] == ["marie.tremblay@example.com"]


def test_format_spans_needs_no_detector():
    """Regexes and nothing else — `persons=False` never loads the weights, which is what
    lets the recall sweep ask this without paying a second pass over the archive."""
    called = []
    original = anonymize._person_spans
    anonymize._person_spans = lambda text: called.append(text) or []
    try:
        anonymize.format_spans("Écrire à marie@example.com")
    finally:
        anonymize._person_spans = original
    assert called == []


def test_format_spans_are_in_whole_text_offsets_across_windows():
    """Windowed like `person_spans`, so the two are one rule read twice rather than two
    that answer differently once a body runs past `_WINDOW`."""
    filler = "Bonjour. " * 120
    text = f"{filler}Écrire à marie@example.com."
    assert len(filler) > anonymize._WINDOW
    spans = anonymize.format_spans(text)
    assert [text[s:e] for s, e in spans] == ["marie@example.com"]
