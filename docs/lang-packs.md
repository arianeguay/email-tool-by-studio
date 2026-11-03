# Lang packs (`email_classifier/lang_packs/<lang>.json`)

A **lang pack** is the per-language detection vocabulary `anonymize.py` and the
archive-measurement scripts under `scripts/` run on. One JSON file per language lives
in `email_classifier/lang_packs/` — `fr_ca.json` today — loaded by
`email_classifier.lang.load_lang_config(language)`.

This is the kernel/deployment seam STU-1460 exists to cut. If the French vocabulary
stayed a literal, the kernel would have no tokenizer without its first deployment; if it
moved without being separated from that deployment's own voice, the kernel would not be
generic, it would be francophone *and* one club. **Detection only.** A deployment's voice
skill (counts over its real replies) and the `reply_signature` and `quote_markers` in its
`rules/voice.yaml` (which match the club by name) are not lang-pack keys and never will be:
they say how one club writes, not what French looks like, and a second francophone
deployment does not inherit them.

## Loading and validation

`load_lang_config(language)` loads and validates the pack. It **fails loudly** —
raising `LangPackError` with an actionable message — when:

- the file `lang_packs/<language>.json` does not exist,
- it is not readable JSON / not a JSON object,
- a **required** key (below) is missing.

There is no fallback language. A deployment naming a pack that does not exist stops
rather than running person detection on an empty vocabulary or somebody else's — the
same direction `anonymize.missing_detector()` already refuses in (STU-890).

A deployment names its pack with `language:` in its `rules/policy.yaml` (default
`fr_ca`, STU-1723). `lang.deployment_config()` is the one place that reads it, for
the modules that build their vocabulary at import time and cannot be handed a policy;
it reads `$EMAIL_CLASSIFIER_POLICY` if set, else `rules/policy.yaml`.

## Adding a language

1. Copy `lang_packs/fr_ca.json` to `lang_packs/<code>.json`.
2. Translate every **required** key below to the target language, keeping the
   `closing_formulas`/`client_footers` entries as regex fragments (they get joined
   with `|` and compiled, not matched literally) and `quote_markers` entries as
   `{"pattern": <regex>, "flags": [...]}` objects.
3. Fill the **optional** keys the deployment needs; the rest default to empty.
4. Measure the false-positive floor and the signature-anchor arms against *that*
   deployment's own archive (`make observe-spans`, `make signature-anchor`) before
   shipping — `closing_formulas`, `client_footers` and `name_line` in `fr_ca.json`
   are forms measured on the first deployment's archive, not generic French (see below), and a pack built
   by translation alone carries no such measurement of its own.
5. Run `pytest tests/test_lang.py` — validation runs on load, so a missing key
   surfaces immediately.

## Required keys

Present in every pack; a missing one is a hard error at load time.

| Key | Feeds | Role |
| --- | --- | --- |
| `stopwords` | `anonymize._KEPT` | Sentence openers, relative/age/school-level words, and the elided clitics — words that never name a person on their own, the veto over a detected span. |
| `role_words` | `anonymize._ROLES` | Role nouns a mailbox uses without naming anybody (`entraineur`, `coach`, `athlète`). |
| `calendar_words` | `anonymize._CALENDAR` | Days and months. Vouched but never a veto: half the calendar is also an ordinary first name (`Avril`, `May`), and position — not the word — decides which sense GLiNER read. |
| `common_noun_surnames` | `anonymize._COMMON_NOUNS` | Words that are both a common noun and a real surname (`parent`/`Parent`), case-sensitive: only the lowercase form vetoes. |
| `closing_formulas` | `anonymize._CLOSING` | Regex fragments for a sign-off line (`merci`, `cordialement`, `sincerely`...), joined with `\|` inside `\A(?:...)[\s,!.]*\Z`. Half of `_signature_spans`' two-sided rule (STU-1224). |
| `client_footers` | `anonymize._FOOTER` | Regex fragments for the one line a sender never wrote themselves (`sent from my`, `envoyé de mon`), the surest marker of a signature block. |
| `quote_markers` | `scripts/olm_to_corpus.py`'s `_QUOTE_MARKERS` | The generic French/English half of where a quoted previous message starts (`Message d'origine`, `Original Message`, a quoted-header block, `>`). A deployment's own quote conventions (a club-specific sign-off, a bare separator its mail client writes) are `quote_markers` in its `rules/voice.yaml` (STU-1728), appended after these; a deployment that declares none gets only these. |
| `dictionaries` | the four `--dictionary` scripts (`observe_spans.py`, `olm_to_corpus.py`, `measure_signature_anchor.py`, `measure_address_anchor.py`, `measure_kinship_anchor.py`) | Default hunspell dictionary paths for the archive-measurement tooling. Dev/eval only — production person detection never touches hunspell. |

## Optional keys

A pack may omit these; the consumer degrades to an empty collection.

| Key | Feeds | Role |
| --- | --- | --- |
| `name_line` | `anonymize._NAME_LINE` | The regex for "one to three capitalized words alone on a line" that `_signature_spans` reads as a candidate name. The character classes (`[A-ZÀ-Ý]`, `[a-zà-ÿ'’-]`) are Latin-1-accent French; a language with a different alphabet needs its own. |
| `elision_prefixes` | folded into `anonymize._KEPT` at load | The elided clitics a detector hands back on their own (French `j'`, `l'`, `qu'`...). Kept apart from `stopwords` because they are a distinct linguistic phenomenon (elision), not ordinary function words — merged into the same veto set at load time either way. |
| `greeting_openers` | `draft_delta._GREETING` | Sentence-opening greetings (`bonjour`, `hello`) `draft_delta.answer_part` strips before measuring how much of a reply is the actual answer. |
| `draft_closing_openers` | `draft_delta._CLOSING` | Sentence-opening sign-offs (`cordialement`, `sincerely`) the same extractor strips. Distinct from `closing_formulas` above: different regex shape (a whole-line prefix match, not an anchored whole-line match), and a different consumer (`draft_delta` measures edit distance, `anonymize` gates a name). |

## `fr_ca.json` carries a measurement, not just a translation

`closing_formulas`, `client_footers` and `name_line` are not generic French — they
are the forms `_signature_spans` was measured against on the first deployment's 1275-message
archive (STU-1224, re-stamped 2026-09-24 for STU-1460): the two-sided rule (a
closing above **and** a phone/address/footer below) adds 4 spans to the published
false-positive count, none of them false positives, holding the floor at 8.4%. Both
`make observe-spans` and `make signature-anchor` were re-run against the extracted
pack on 2026-09-24 and published byte-identical evidence but for the date —
`eval/olm-spans-evidence.json` and `eval/signature-anchor-evidence.json` are the
record. A
`fr_ca` pack built for a second francophone deployment inherits these literals but
not this measurement — say so, with the date, so the next deployment knows it owes
its own `make observe-spans` / `make signature-anchor` pass rather than trusting a
number taken on a different mailbox.

## The required/optional split

Required = the keys a working deployment genuinely needs, populated in the shipped
`fr_ca.json`. Optional = keys that are empty or advisory, or that only some
consumers read. `REQUIRED_KEYS` and `OPTIONAL_KEYS` in `email_classifier/lang.py`
are the source of truth; the tables above mirror them.
