"""Batch-stable PII tokenization for the Stage-2 fan-out (STU-843).

Studio's anonymization middleware is run-scoped, and a `map:` fan-out spawns one child
run per email: the middleware is not handed to those children, and its token counter
would not be shared across them anyway. Tokenizing in the leaf therefore gives the same
person a different token in every email. This module tokenizes once, in `classify-pre`,
before the fan-out — one keymap for the whole batch, persisted so a value keeps its
token across runs (which also keeps the fan-out's resume keys stable).

Format categories mirror the kernel detector (Studio
`anonymizer/src/detection/regex-matcher.ts`): same categories, same priority, same
`CATEGORY_N` token shape. Person detection does not: the kernel anchors a name on a
salutation, French mail opens `Bonjour,` and signs off at the bottom, so the anchor
looked at the one place a name never is and the whole club corpus reached the LLM with
every name in cleartext (STU-890). Names come from GLiNER here instead.
"""

import json
import logging
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import yaml

from email_classifier import deployment, lang

# The vocabulary below used to be literals here (STU-1460); it comes from the pack the
# deployment's policy names (STU-1723). See docs/lang-packs.md for what each key feeds
# and why `_CLOSING`/`_FOOTER`/`_NAME_LINE` are forms measured on the first deployment's archive rather than
# generic French.
_LANG = lang.deployment_config()

_log = logging.getLogger("email_classifier.anonymize")

REPO_ROOT = deployment.ROOT

# Most-specific format wins. credit_card beats ssn on the dangerous digit-run collision;
# email is near-unambiguous (the @); phone is the most permissive so it runs late; person
# is last, so an address stays one EMAIL token instead of a name inside it.
_PRIORITY = ("credit_card", "ssn", "email", "phone", "person")

# address is deliberately absent: a number+word+street-suffix pattern false-positives on
# ordinary prose, and tokenizing legitimate text is worse than a miss here.
#
# The second phone alternative takes ten bare digits, only in NANP shape (area code and
# exchange start 2-9) and bounded by non-digits rather than \b, since a 3CX voicemail file
# name `vmail_5149682034_302_...` carries the caller's number between underscores
# (STU-1501). Over the 2175-message archive it adds 218 matches: 212 that file name, 6 a
# `+`-prefixed number, no digit run that is not a phone.
_FORMAT_PATTERNS = {
    "credit_card": re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(
        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"
        r"|(?<!\d)(?:\+?1[-.\s]?)?[2-9]\d{2}[2-9]\d{6}(?!\d)"
    ),
}

# Local parts that name a function rather than a person. Tokenizing one hides nobody —
# `no-reply@amazonaws.com` identifies a vendor, not a human — while removing the sender
# domain, the strongest signal stage 2 has for telling a real vendor notice from
# phishing. Without it the model read its own `EMAIL_7` as "generic anonymized sender"
# and routed genuine account notices to bucket D, which apply may delete (STU-902).
_ROLE_LOCALS = frozenset({
    "account", "accounts", "admin", "alert", "alerts", "billing", "bounce", "bounces",
    "contact", "donotreply", "help", "info", "mailer", "mailerdaemon", "marketing",
    "news", "newsletter", "noreply", "notification", "notifications", "postmaster",
    "questions", "sales", "security", "service", "support", "updates",
})

_TOKEN = re.compile(r"^([A-Z_]+)_(\d+)$")

# STU-401's recall bake-off picked the model and the label: 0.95 overlap recall against
# 0.51 for both spaCy-FR and Presidio, 0.93 against 0.34 on lowercase names. It left the
# false-positive rate open as a deployment gate; STU-890 measured it on `fixtures-club/`,
# where 0.3 is the loosest threshold that still misses no name — 0.4 drops `Jade`, 0.2
# starts claiming `Bonjour` and `parent`.
_MODEL = "urchade/gliner_multi-v2.1"
_LABEL = "personne"
_THRESHOLD = 0.3
_SECOND_PASS_THRESHOLD = 0.6

# Set by a process that loads the weights to tokenize one file and exits. Such a process
# never earns the transfer back — the club-memory shell-out measures 9.9 s on the CPU
# against 12.0 s on the GPU — and it runs while its caller already holds a copy, so the
# second 1.3 GB lands on the same card (STU-984).
_cpu_only = False


def stay_on_the_cpu() -> None:
    """Declare this process a one-shot, for which the GPU costs more than it saves."""
    global _cpu_only
    _cpu_only = True


# The word inside a proper noun, elision-aware: in "l'Association" the word is
# `Association`, and a match starting at `l` would swallow the article whole.
CAPITALIZED_WORD = re.compile(r"(?<![A-Za-zÀ-ÿ])[A-ZÀ-Ý][a-zà-ÿ-]+")

# The all-caps word, which `CAPITALIZED_WORD` cannot reach: it wants a lowercase letter
# after the capital, so an organization's own acronym was never vouched and GLiNER
# claimed it as a person (STU-915). Two letters is the floor: a lone initial belongs to
# somebody's name, not to an organization.
_ACRONYM = re.compile(r"(?<![A-Za-zÀ-ÿ])[A-ZÀ-Ý]{2,}(?![A-Za-zÀ-ÿ])")

# Digits are part of the word: the club's age categories are written `12U` and `U15`,
# and splitting them into a bare `U` left the category invisible to the veto below.
_WORD = re.compile(r"[A-Za-zÀ-ÿ0-9']+")

_VOUCHED_SKILL = lang.deployment_policy().vouched_skill
SKILL = REPO_ROOT / ".studio" / "skills" / f"{_VOUCHED_SKILL}.skill.md" if _VOUCHED_SKILL else None

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)

# Sentence openers, the words for a relative, and the words for an age or a school
# level, which name nobody on their own. Over-redaction is the failure this list
# prevents — at 0.3 GLiNER claims `Je` and `Ma conjointe` on the club corpus, and `The
# U15 girls` once a team is mentioned (STU-986). The veto refuses a span only when every
# one of its words is here, so a single word missing carries the whole phrase through:
# `12 à 14 joueuses` was minted a person on `à` alone, its three other words already
# refused, and `enfant de 12 ans` on `ans` (STU-1103).
#
# From the lang pack since STU-1460: `stopwords` plus `elision_prefixes` (the clitics
# GLiNER hands back on their own out of "J'aimerais") — two keys there, one set here,
# because the veto treats an elided `j` exactly like any other stopword.
_KEPT = frozenset(_LANG["stopwords"]) | frozenset(_LANG.get("elision_prefixes", []))

# What a club mailbox calls somebody without naming them. Measured, not guessed: these
# are the word forms the detector claimed in more than one message of the real archive
# and that a dictionary already accounts for (STU-910) — `athlètes` alone 48 times, and
# the whole set 11.6% of everything it claimed. The skill carries most of them, but
# `vouched()` harvests only its capitalized words, so a role written in lowercase there
# never reached the veto; harvesting the lowercase half instead would vouch 512 words of
# ordinary French prose, which is how STU-911 lost real names.
_ROLES = frozenset(_LANG["role_words"])


# Vouched, but never a veto (STU-909): half the calendar is also a first name — `Avril`,
# `Mai` and `May` are ordinary ones here. Position is what separates the two senses, and
# GLiNER reads it: it claims `Avril` in `Ma fille Avril` and nobody in `Lundi 14 avril`.
_CALENDAR = frozenset(_LANG["calendar_words"])


# Common nouns the detector claims here, and Quebec family names both: `Parent` names
# somebody, `parents` is who the mail is about (STU-981). Case is the whole difference,
# so these veto a detection only written in lowercase — a vouched word cannot, because
# the veto casefolds and would drop the surname on sight (STU-911).
#
# `jeune` sits here rather than beside the `jeunes` already in `_ROLES` because no family
# name is plural: the plural can refuse unconditionally, the singular cannot (STU-1103).
_COMMON_NOUNS = frozenset(_LANG["common_noun_surnames"])


# No name carries a digit, and the club's age categories do: `12U`, `U15`. Listing the
# categories instead would only vouch the spellings the skill happens to publish, and
# the parent writing in picks the other one (STU-986).
_HAS_DIGIT = re.compile(r"\d")
_HYPHENATED = re.compile(r"[\w'-]+")


# `maxsize=1` was the trap (STU-1462): two deployments in one process each call these
# with their own `skill` path, and a size-one cache evicts the first the moment the
# second is asked — the wrong vouched set is a name reaching the LLM in clear, not a
# slow path. No symptom yet, one deployment being the only caller so far; 8 is room for
# a handful of skill paths without growing unbounded on a path nobody ever repeats.
@lru_cache(maxsize=8)
def _skill_people(skill: Path | None = SKILL) -> frozenset[str]:
    """The words of the names the skill's `people:` key declares.

    Harvesting the skill wholesale folds its coaching staff in with its platforms and
    its gyms, and a coach's first name is an ordinary Quebec first name: `Eugenia` and
    `Serge` were vouched words, so the veto dropped them on sight and a parent's
    mention of one reached the LLM in the clear (STU-911). The club publishing its
    staff is not the point — the coincidence is, and so is the day this file names
    somebody who is not staff.

    Declaring them is what separates them, because nothing measurable does: over this
    file the detector claims platform and program names and `parent` alongside
    the two real names, and the scores interleave.
    """
    text = skill.read_text(encoding="utf-8") if skill and skill.exists() else ""
    match = _FRONTMATTER.match(text)
    people = (yaml.safe_load(match[1]) or {}).get("people", []) if match else []
    return frozenset(w.casefold() for name in people for w in _WORD.findall(name))


@lru_cache(maxsize=8)
def vouched(skill: Path | None = SKILL) -> frozenset[str]:
    """`_KEPT`, `_ROLES` and the calendar, plus every skill word that is not a person.

    Reading the skill rather than hand-listing it is what keeps the two in step: a
    program, a platform or a place the club starts publishing is kept the day it lands
    in the skill, without touching this file.
    """
    text = skill.read_text(encoding="utf-8") if skill and skill.exists() else ""
    found = CAPITALIZED_WORD.findall(text) + _ACRONYM.findall(text)
    words = {w.casefold() for w in found} - _skill_people(skill)
    return frozenset(_KEPT | _ROLES | _CALENDAR | words)


@lru_cache(maxsize=8)
def _never_a_person(skill: Path | None = SKILL) -> frozenset[str]:
    """The vouched words allowed to veto a detection: everything but the calendar.

    A vouched word is one a draft may reuse, which is not the same as one that cannot
    name anybody (STU-909). Vetoing on the wider list dropped `Avril` on sight, and the
    direction of that failure is the bad one: an over-redaction costs a draft some
    phrasing, this costs a name sent to the LLM in the clear.
    """
    return vouched(skill) - _CALENDAR


_NO_DETECTOR = (
    "person detection needs the `ner` extra (`pip install -e '.[ner]'`); "
    "without it the tokenizer would find no name at all"
)


def missing_detector() -> str | None:
    """Why person detection cannot run here, or None — `_model`'s refusal, asked early.

    Lets a caller refuse before opening a run instead of raising on the first
    `anonymize()` call, well after the mailbox is fetched (STU-908).
    """
    try:
        import gliner  # noqa: F401
    except ImportError:
        return _NO_DETECTOR
    return None


@lru_cache(maxsize=1)
def _model():
    """The detector, loaded once per process. A missing extra is a refusal, not a fallback.

    Falling back to the format regexes alone would leave a run looking anonymized while
    every name in it reached the LLM in cleartext — the failure STU-890 exists to end.

    The weights are a pinned asset the `ner` extra pulls once, but `from_pretrained`
    still makes a blocking hub round-trip on a warm cache: it sleeps on a socket, so a
    slow hub stalls the run with nothing looking busy, and on the webhook path that
    silently stops the mailbox from being processed (STU-638). `huggingface_hub` reads
    its offline flag live off this global — `local_files_only` and the env var are both
    ignored by the nested tokenizer load — so flipping it pins the load to the cache. A
    genuine miss clears it and pulls once: an offline probe that fails must not mean the
    model never loads (STU-731).

    `transformers` warns at every load that this tokenizer carries "an incorrect regex
    pattern" and to set `fix_mistral_regex=True`. It is a false alarm, and setting the
    flag is the harmful move: the check only reaches mdeberta because the offline pin
    above makes the load look local, and it then flags any config with no
    `transformers_version` key — mdeberta-v3-base's predates the field. The flag swaps
    the sentencepiece Metaspace pre-tokenizer for Mistral's byte-level Split regex; on
    run 33fc5c3d's corpus that drops person spans from 33 to 14 and invents one
    (STU-919).

    `map_location` defaults to `"cpu"`, so a present GPU stays idle unless it is asked
    for; passing it reads the weights straight onto the device, where a later
    `.to("cuda")` would materialize the whole fp32 copy in host memory first (STU-957).

    The GPU is an optimization, so it is attempted and not required. A one-shot process
    declines it outright (`stay_on_the_cpu`); the rest fall back when the card is full,
    because the card is shared with a desktop and with whatever else the run spawned.
    Two 1.3 GB copies on a 6 GB laptop GPU is what made the club-memory shell-out tests
    flaky, and reporting that `CUDA error: out of memory` as the cache miss below is
    what cost STU-984 five runs chasing `HF_HOME`. Both branches therefore catch the one
    exception a miss actually raises (STU-990): `LocalEntryNotFoundError` covers the
    weights being absent offline *and* the pull that then fails to reach the hub, and
    nothing else. Anything else is not fixed by re-pulling, so retrying it buys a second
    doomed load and a message whose every clause is false.
    """
    try:
        from gliner import GLiNER
    except ImportError as exc:
        raise RuntimeError(_NO_DETECTOR) from exc

    import huggingface_hub.constants as hub
    import torch
    from huggingface_hub.errors import LocalEntryNotFoundError

    def load(device):
        previous = hub.HF_HUB_OFFLINE
        hub.HF_HUB_OFFLINE = True
        try:
            return GLiNER.from_pretrained(_MODEL, map_location=device)
        except LocalEntryNotFoundError:
            hub.HF_HUB_OFFLINE = False
            return GLiNER.from_pretrained(_MODEL, map_location=device)
        finally:
            hub.HF_HUB_OFFLINE = previous

    if torch.cuda.is_available() and not _cpu_only:
        try:
            return load("cuda")
        except Exception as exc:
            _log.warning("GLiNER did not load on the GPU, falling back to the CPU: %s", exc)

    try:
        return load("cpu")
    except LocalEntryNotFoundError as exc:
        raise RuntimeError(
            f"{_MODEL} is not in the local Hugging Face cache and the hub pull "
            "failed; re-run `pip install -e '.[ner]'` on a reachable network"
        ) from exc


def _cannot_name(word: str, extra_vetoed: frozenset[str] = frozenset()) -> bool:
    if _HAS_DIGIT.search(word):
        return True
    if word.casefold() in extra_vetoed:
        return True
    return (word.islower() if word.casefold() in _COMMON_NOUNS
            else word.casefold() in _never_a_person())


def _never_names_anybody(value: str, extra_vetoed: frozenset[str] = frozenset()) -> bool:
    """Every word of `value` is one that cannot name a person.

    The test is every word, not the first: `Ma conjointe` needs `conjointe` on the list
    too, and a real name never has all of its words on it.

    `extra_vetoed` is per call rather than a standing list: the sender's own address
    domain (STU-1273), which names an organization the day this text happens to mention
    it, and nothing else on any other call.

    The digit rule reads a hyphen-joined run, not a word: `_WORD` splits on hyphens,
    so `p00-iscream-smtp-54d86dbcf5-4xr2q` offered `iscream` and `smtp` and a mail
    server's pod name was minted a person (STU-1499).
    """
    return all(
        _HAS_DIGIT.search(run)
        or all(_cannot_name(w, extra_vetoed) for w in _WORD.findall(run))
        for run in _HYPHENATED.findall(value)
    )


# `no_reply@emagine.org` -> the labels a word is held against, the same split STU-1263
# already uses for the recall measurement (`scripts/olm_to_corpus.py`'s `_LABEL`).
# Duplicated rather than imported: this file is production and must not depend on
# `scripts/`, which is dev/eval-only.
_DOMAIN_LABEL = re.compile(r"[^a-z0-9]+")


def _domain_labels(address: str | None) -> frozenset[str]:
    """The word-shaped labels of `address`'s own domain, casefolded.

    `no_reply@emagine.org` names "emagine" as a label of the mailbox's own domain, and a
    word matching one names the organization that owns that mailbox, not a person — the
    same argument `is_machine_address` already makes about a local part, one field over,
    and per-entry rather than a list of organizations: nothing here goes stale when a
    vendor is renamed (STU-1273).

    Restricted to a machine sender, which is the evidence: `no_reply@emagine.org` is a
    role address by the same test one field over, so its domain speaks for an
    organization and nobody wrote it. A personal sender's domain does not carry that —
    `julie@dupont.com` says nothing about who "Dupont" is in the body, and a real
    surname that happens to match a vanity or family domain would otherwise be refused
    as a person, the exact cost STU-1263's own measurement flagged and declined to
    bring into production.
    """
    if not address or "@" not in address or not is_machine_address(address):
        return frozenset()
    domain = address.rsplit("@", 1)[-1].casefold()
    return frozenset(_DOMAIN_LABEL.split(domain)) - {""}


def _local_person_spans(
    text: str, from_address: str | None = None
) -> list[tuple[int, int, str]]:
    """GLiNER's spans, minus the ones made entirely of words that never name anybody.

    Asked twice when the first pass claims anything: a confident full name suppresses a
    bare first name elsewhere in the same window (STU-1224), so a parent's signature hid
    their child's name in the prose above it — `Je l'annonce à X ce soir.` scored 0.02
    beside a 0.98 signature, and 0.65 once the signature was masked (STU-1993). The mask
    is same-length filler so the second pass's offsets are the text's own, and a claim
    overlapping a masked span is the filler, not a name.

    With the signature masked the model also reaches for the job title under it, so the
    second pass keeps only claims at `_SECOND_PASS_THRESHOLD` and capitalized word for
    word. Measured on the 1275-message archive: at the first pass's 0.3 it added 50 name
    occurrences and 63 titles, acronyms and prose (`Kinésiologue`, `Director`, `JDQ`); at
    0.6 it adds 44 and 12, and still claims the three live leaks (0.65 to 0.90). At 0.7
    it adds 2 non-persons but loses the 0.65 leak.
    """
    extra_vetoed = _domain_labels(from_address)

    def claims(window: str, threshold: float) -> list[tuple[int, int]]:
        return [
            (entity["start"], entity["end"])
            for entity in _model().predict_entities(window, [_LABEL], threshold=threshold)
            if not _never_names_anybody(entity["text"], extra_vetoed)
        ]

    first = claims(text, _THRESHOLD)
    masked = text
    for start, end in first:
        masked = masked[:start] + "x" * (end - start) + masked[end:]
    second = [
        (start, end) for start, end in claims(masked, _SECOND_PASS_THRESHOLD)
        if masked[start:end] == text[start:end]
        and all(word[0].isupper() for word in text[start:end].split())
    ] if first else []
    return [(start, end, "person") for start, end in first + second]


def _person_spans(
    text: str, from_address: str | None = None
) -> list[tuple[int, int, str]]:
    """The same spans, asked of a detector this process may not have to load (STU-938).

    On the push path this module is imported by three short-lived processes per
    notification, each of which spent ~20s and 2.8 GB loading GLiNER to do a fraction
    of a second of work. When a webhook is serving one, they borrow it instead; with
    nobody serving — `make run`, the tests, a bare script — nothing changes.

    A server that answers badly raises rather than falling back: the local load would
    hide a broken detector behind a run that merely got slow again.
    """
    from email_classifier.detector import remote_spans

    spans = remote_spans(text, from_address)
    return _local_person_spans(text, from_address) if spans is None else spans


# A confident full name earlier in the text suppresses a bare given name later in it:
# on the body run `fb7889ca` sent to stage 2 in clear, GLiNER scores the signature name
# 0.19 against 0.96 for the daughter's full name two lines above, and the same signature
# name comes back at 0.81 once the daughter is named with one word (STU-1224). So the
# miss is contextual, which is why no fixture caught it — a club fixture rarely carries
# both — and why it is not reachable by threshold: 15 of 23 short given names replayed in
# that shape land under 0.3, and buying them costs the archive's false-positive floor
# 8.4% -> 13.2% at 0.15, the trade STU-1046 already refused for a narrower window.
#
# The position is reachable without the model, the way `_known_person_spans` is. A
# closing above and a phone, an address or a mail-client footer below is what a signature
# block is; requiring both is the whole of the precision, and its cost is measured rather
# than argued — over the 1275-message archive the rule adds 4 spans, none of them in a
# bucket `observe_spans` counts as a false positive, and the published floor moves to 8.4%.
# Either side alone adds 157 and pushes the ceiling 30.2% -> 36.3%.
# From `closing_formulas` in the lang pack (STU-1460) — and not generic French: these
# are the forms `_signature_spans` was measured against on the first deployment's archive (below), so
# a pack built for a second francophone deployment inherits the literals but owes its
# own re-measurement. See docs/lang-packs.md.
_CLOSING = re.compile(
    r"\A(?:" + "|".join(_LANG["closing_formulas"]) + r")[\s,!.]*\Z", re.I,
)

# The one line of a signature block the sender never wrote, and the surest marker of
# one. From `client_footers` — same archive-measured caveat as `_CLOSING` above.
_FOOTER = re.compile(r"\A(?:" + "|".join(_LANG["client_footers"]) + r")", re.I)

# One to three capitalized words alone on the line: a fourth is a job title or an
# employer. From the optional `name_line` key — a pack that omits it cannot run
# `_signature_spans` at all, since every candidate line has to match something.
_NAME_LINE = re.compile(_LANG["name_line"])

_LINE = re.compile(r"^([^\r\n]*)", re.M)


def _signature_spans(text: str) -> list[tuple[int, int, str]]:
    """Where a signature block names somebody, without asking the model (STU-1224)."""
    lines = [(m.start(1), m[1]) for m in _LINE.finditer(text)]
    spans = []
    for index, (start, content) in enumerate(lines):
        name = _NAME_LINE.match(content)
        if not name or _never_names_anybody(name[1]):
            continue
        if _closes_above(lines, index) and _signs_below(lines, index):
            spans.append((start + name.start(1), start + name.end(1), "person"))
    return spans


def _closes_above(lines: list[tuple[int, str]], index: int) -> bool:
    for _, content in reversed(lines[:index]):
        if content.strip():
            return bool(_CLOSING.match(content.strip()))
    return False


def _signs_below(lines: list[tuple[int, str]], index: int) -> bool:
    """The next non-blank line is a phone, an address or a footer — or the text ends.

    Ending the text counts because a name is often the last thing in a body, and a window
    that ends there ends on a line boundary: `_windows` never cuts one open.
    """
    for _, content in lines[index + 1:]:
        if not content.strip():
            continue
        return bool(
            _FOOTER.match(content.strip())
            or _FORMAT_PATTERNS["phone"].search(content)
            or _FORMAT_PATTERNS["email"].search(content)
        )
    return True


def is_machine_address(value: str) -> bool:
    """A role address: separators and any `+tag` dropped, the local part is a role name."""
    local = value.rsplit("@", 1)[0].split("+", 1)[0]
    return re.sub(r"[-_.]", "", local).casefold() in _ROLE_LOCALS


# A DKIM or ARC signature header value: `v=1; a=rsa-sha256; c=relaxed/relaxed; ...`.
# Every tag value in it is an algorithm, a domain, a selector or a hash, and the
# detector claimed `relaxed` off `c=relaxed/relaxed` as a person (STU-1994).
_TAG_LIST = re.compile(r"\s*[a-z]\w*=[^;]*(?:;\s*[a-z]\w*=[^;]*)+;?\s*")


def _candidates(
    text: str, persons: bool, from_address: str | None = None
) -> list[tuple[int, int, str]]:
    spans = [
        (m.start(), m.end(), category)
        for category, pattern in _FORMAT_PATTERNS.items()
        for m in pattern.finditer(text)
    ]
    if not persons or _TAG_LIST.fullmatch(text):
        return spans
    return spans + _person_spans(text, from_address) + _signature_spans(text)


def _known_person_spans(text: str, values: Iterable[str]) -> list[tuple[int, int, str]]:
    """Where `text` names a person the keymap already knows (STU-1046).

    A name in the keymap is a fact, and asking the model to re-derive it made its
    redaction depend on how much text happened to surround it: replayed over the
    26-message real corpus of run 8704964d, 4 of the 25 occurrences of two first names
    the keymap already held reached the LLM in clear, all of them in a salutation, well
    under the detector's 384-wordpiece cut. Shrinking the window to 200 characters
    caught all 25 but pushed the archive's false-positive rate from 9.1% to 13.4% and
    its ceiling past the band STU-910 measured, because a smaller window claims more of
    everything. This costs no window and no model call, and claims nothing the batch
    has not already decided is a person.

    Case-sensitively and on whole words: `marc` the ordinary noun is not `Marc`, and a
    substring match would take the name out of `Marcel`.
    """
    return [
        (m.start(), m.end(), "person")
        for value in values
        for m in re.finditer(rf"(?<!\w){re.escape(value)}(?!\w)", text)
    ]


def _resolve(candidates: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Drop any candidate overlapping one of a higher-priority category."""
    occupied: set[int] = set()
    accepted = []
    for start, end, category in sorted(
        candidates, key=lambda c: (_PRIORITY.index(c[2]), c[0])
    ):
        if occupied.isdisjoint(range(start, end)):
            occupied.update(range(start, end))
            accepted.append((start, end, category))
    return sorted(accepted)


# GLiNER truncates its input at 384 wordpieces — roughly 1350 characters of French —
# and reports no entity past the cut while returning normally, so a whole-body call left
# every name below it in the clear (STU-955). Windows close on a sentence or line
# boundary, so a name is split across two only when one sentence alone runs past the
# window, and the cost there is two tokens for one person rather than a name in clear.
_WINDOW = 600
_SENTENCE = re.compile(r"(?<=[.!?\n])(\s+)")


def _windows(text: str) -> list[str]:
    """`text` cut into detector-sized windows that rejoin into it exactly."""
    windows = [""]
    for piece in _SENTENCE.split(text):
        if windows[-1] and len(windows[-1]) + len(piece) > _WINDOW:
            windows.append("")
        windows[-1] += piece
    return windows


def person_spans(
    text: str, from_address: str | None = None
) -> list[tuple[int, int]]:
    """Every person span redaction would replace in `text`, in whole-text offsets.

    Windowed and resolved the way `Tokenizer.anonymize` does it, so a gate on this set
    (STU-925) is a gate on what actually reaches the model — the format regexes still
    win an overlap, and a name inside an address is not a name found in free text.

    `from_address` is optional and defaults to no context, so `make eval-spans` and
    `make observe-spans` — neither of which has a sender to hand — see exactly today's
    spans (STU-1273): the frozen fixtures-club gate and the archive's published
    false-positive floor and ceiling are both unaffected by this parameter existing.
    """
    spans, offset = [], 0
    for window in _windows(text):
        spans += [
            (offset + start, offset + end)
            for start, end, category in _resolve(_candidates(window, True, from_address))
            if category == "person"
        ]
        offset += len(window)
    return spans


def format_spans(text: str) -> list[tuple[int, int]]:
    """Every span the format regexes would replace in `text`, in whole-text offsets.

    The other half of `person_spans`, and the half that outranks it: `_PRIORITY` places
    every format ahead of `person`, so an occurrence inside one of these is replaced
    before the detector is ever consulted. A measurement that counts a name the detector
    declined has to know which of those were already gone (STU-1264).

    Windowed like its sibling so the two are one rule read twice, though the regexes are
    position-independent and would agree whole-text. `persons=False` is what keeps it
    off the detector: it runs the four patterns and nothing else, so a caller pays
    microseconds rather than a second pass over 1.76 GB of weights.
    """
    spans, offset = [], 0
    for window in _windows(text):
        spans += [
            (offset + start, offset + end)
            for start, end, category in _resolve(_candidates(window, False))
        ]
        offset += len(window)
    return spans


class Tokenizer:
    """Value <-> token mapping, consistent for as long as the keymap is carried.

    `persons=False` runs the format regexes alone, for callers with nothing to hide from
    the model. It is spelled out at the call site on purpose: the default has to be the
    safe one, or the next caller silently reinstates STU-890.
    """

    def __init__(
        self, keymap: dict[str, str] | None = None, *, persons: bool = True
    ) -> None:
        self._persons = persons
        self.keymap: dict[str, str] = {}     # token -> original
        self._inverse: dict[str, str] = {}   # original -> token
        self._counters: dict[str, int] = {}
        self._minted: set[str] = set()       # the values this batch tokenized itself
        for token, value in (keymap or {}).items():
            match = _TOKEN.match(token)
            if match:
                key = match[1].lower()
                # Counters advance over the pruned entries too, so a dropped number is
                # never handed to a different value.
                self._counters[key] = max(self._counters.get(key, 0), int(match[2]))
            if token.startswith("PERSON_") and _never_names_anybody(value):
                continue
            self.keymap[token] = value
            self._inverse[value] = token

    def minted(self) -> dict[str, str]:
        """The entries this tokenizer minted, as opposed to the ones it was seeded with.

        The shared keymap is the accumulated state of the machine, so it cannot say
        what any one run saw (STU-924). This can.
        """
        return {t: v for t, v in self.keymap.items() if v in self._minted}

    def token_for(self, value: str, category: str) -> str:
        existing = self._inverse.get(value)
        if existing:
            return existing
        counter = self._counters.get(category, 0) + 1
        self._counters[category] = counter
        token = f"{category.upper()}_{counter}"
        self.keymap[token] = value
        self._inverse[value] = token
        self._minted.add(value)
        return token

    def anonymize(self, text: str, *, from_address: str | None = None) -> str:
        windows = _windows(text)
        return self.redact_known("".join(
            self._anonymize_window(
                window, last=index == len(windows) - 1, from_address=from_address
            )
            for index, window in enumerate(windows)
        ))

    def _known_persons(self) -> list[str]:
        return [v for t, v in self.keymap.items() if t.startswith("PERSON_")]

    def _components(self) -> dict[str, str]:
        """Each word of a multi-word person value, mapped to that value's token.

        Whitespace-separated, so `Marie-Claude Roy` offers `Marie-Claude` and not a bare
        `Marie`: the hyphenated half is the name its owner signs with, and splitting it
        further would claim one of the commonest first names in the corpus off a value
        nobody wrote alone.

        A component two people share names neither of them, so it redacts to the
        unindexed placeholder rather than asserting one — the shared keymap accumulates
        across runs, so a second `Élise` is a matter of time.

        A lowercase word beside capitalized ones is a connector, not a name: the detector
        returned `Alex ou Caro` as one span, and its `ou` then redacted every "ou" in later
        mail (STU-1762). An all-lowercase value keeps its words, since some people sign
        that way, unless one of them cannot name anybody: that is prose, not a signature.
        A bounce's `administrateur de courrier de la partie responsable` turned every later
        "partie" into a person token (STU-2016).
        """
        components: dict[str, str] = {}
        for token, value in self.keymap.items():
            words = value.split()
            if not token.startswith("PERSON_") or len(words) < 2:
                continue
            capitalized = any(w[:1].isupper() for w in words)
            if not capitalized and any(_never_names_anybody(w) for w in words):
                continue
            for word in words:
                if _never_names_anybody(word) or (capitalized and word.islower()):
                    continue
                held = components.setdefault(word, token)
                if held != token:
                    components[word] = "PERSON_X"
        return components

    def redact_known(self, text: str) -> str:
        """Redact a first name the keymap holds only inside a full name (STU-1060).

        `_known_person_spans` matches a held value whole, so `Élise Boucher` at the top
        of a contact-form notification does nothing for the `Élise` the parent signs off
        with — and both being in one window, the mint of the first is not available to
        the second either. Run after the windows instead: by then every name the text
        made this batch mint is in the keymap, whichever window it came from.

        The component carries the whole value's token rather than minting its own, which
        is the point — a second token would give one person two identities for as long
        as the keymap is carried, the failure STU-920 exists to prevent.

        Running last is also what keeps a name inside an address out of reach: the
        format pass has already taken the address whole, and the only one it leaves in
        clear is a role address (STU-902), whose local part is a lowercase function word
        no case-sensitive component can match.

        Whole values are matched here too, for the same reason one window over: a value
        minted in a window was unknown to that window's `_known_person_spans`, so a
        second occurrence the detector missed there went out in clear while the first
        was tokenized (STU-1499).

        Public because one call is not one payload: a caller tokenizing several fields
        re-runs this over the earlier ones once the last is done, or a name minted in the
        head stays in clear in a history turn tokenized before it (STU-1993).
        """
        if not self._persons:
            return text
        targets = self._components()
        targets.update((v, t) for t, v in self.keymap.items() if t.startswith("PERSON_"))
        matches = sorted(
            (
                (m.start(), m.end(), token)
                for value, token in targets.items()
                for m in re.finditer(rf"(?<!\w){re.escape(value)}(?!\w)", text)
            ),
            key=lambda m: (m[0], -m[1]),
        )
        kept, end = [], 0
        for match in matches:
            if match[0] >= end:
                kept.append(match)
                end = match[1]
        for start, stop, token in reversed(kept):
            text = text[:start] + token + text[stop:]
        return text

    def _anonymize_window(
        self, text: str, *, last: bool, from_address: str | None = None
    ) -> str:
        candidates = _candidates(text, self._persons, from_address)
        if self._persons:
            candidates += _known_person_spans(text, self._known_persons())
        spans = _resolve(candidates)
        tail = len(text)
        for start, end, category in reversed(spans):
            value = text[start:end]
            # Kept as a span through _resolve, so the person detector cannot claim a
            # word inside the address it is no longer replacing.
            if category == "email" and is_machine_address(value):
                continue
            if category == "person" and last and end == tail:
                # Gmail cuts `snippet` at ~200 characters without regard for word
                # boundaries, so a name ending the text may be a fragment; minting
                # `PERSON_9 -> 'Carolin'` next to `PERSON_34 -> 'Caroline'` gives one
                # person two identities for as long as the keymap is carried (STU-920).
                # Redacted rather than left in clear: the test is positional and cannot
                # tell that fragment from a whole name signing off an untruncated field.
                # `PERSON_X` carries no index, so restoration passes it through as the
                # placeholder it is. Only the last window ends where the field does:
                # firing on every window boundary would collapse a body's signatures
                # onto one unindexed token. Only a value *this batch* minted keeps its
                # token here: a keymap written before this rule carries the fragments
                # themselves, so reading one back would hand `Carolin` its old token
                # forever (STU-922). A word of a full name this window also claims is
                # left for `redact_known`, which gives it that name's token: the
                # detector's second pass claims a sign-off `Élise` under `Élise Boucher`
                # (STU-1993), and `PERSON_X` would cost the reply who signed.
                if value not in self._minted and any(
                    c == "person" and (s, e) != (start, end) and value in text[s:e].split()
                    for s, e, c in spans
                ):
                    continue
                token = (
                    self._inverse[value] if value in self._minted
                    else f"{category.upper()}_X"
                )
            else:
                token = self.token_for(value, category)
            text = text[:start] + token + text[end:]
        return text


def anonymize_fields(
    item: dict,
    tokenizer: Tokenizer,
    fields: list[str],
    from_address: str | None = None,
) -> dict:
    """Tokenize the named fields of a fan-out item, in place. Headers are a dict."""
    for field in fields:
        value = item.get(field)
        if isinstance(value, str):
            item[field] = tokenizer.anonymize(value, from_address=from_address)
        elif isinstance(value, dict):
            item[field] = {
                k: tokenizer.anonymize(v, from_address=from_address)
                for k, v in value.items()
            }
    return item


def deanonymize(text: str, keymap: dict[str, str]) -> str:
    """Restore original values. Longest token first, so EMAIL_10 beats EMAIL_1."""
    for token, original in sorted(keymap.items(), key=lambda kv: -len(kv[0])):
        text = text.replace(token, original)
    return text


def load_keymap(path: Path) -> dict[str, str]:
    """Best-effort: an unreadable keymap starts a fresh one rather than failing a run."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_keymap(path: Path, keymap: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keymap, ensure_ascii=False, indent=2), encoding="utf-8")


def run_keymap_path(keymap_path: Path, run_id: str) -> Path:
    """The run's own keymap, beside the shared one it was seeded from."""
    return keymap_path.parent / f"{run_id}.json"


def record_minted(path: Path, minted: dict[str, str]) -> None:
    """Merge a stage's minted entries into the run's own keymap (STU-924).

    Merged, not written: `mailbox-full` tokenizes twice under one run id — once in
    `classify-batch`, once in `drafts` — and the second seeds from the shared keymap,
    so it counts the first stage's entries as carried in. The union is the run's.
    """
    existing = load_keymap(path)
    save_keymap(path, {**existing, **minted})


def sent_path(keymap_path: Path, run_id: str) -> Path:
    """Where the run records what it handed the LLM, beside its own keymap."""
    return keymap_path.parent / f"{run_id}-sent.jsonl"


def record_sent(path: Path, payloads: Iterable[dict]) -> None:
    """Append the payloads a stage sent stage 2, as it sent them (STU-1036).

    Recall is a claim about the text that reached the model, and until now the only
    way to make it was to replay the run's recorded input. A push run has no input on
    any disk — deliberately, since STU-859 — so on the path that now writes most
    reports the claim could not be made at all.

    Appended rather than written for `record_minted`'s reason: one run id covers two
    tokenizing stages, and the run is their union.

    The assembled payload, not the tokenizer's output: a field the scope never named
    is exactly where a leak would sit, and recording what the tokenizer returned would
    be blind to it.

    An empty batch still touches the file. A stage that sent nothing and a run that
    never recorded anything are different answers to the recall question — clean, and
    unknown — and without the file they are the same absence (the shape STU-917 gave
    the drafts stage for the same reason).

    Best-effort, and alone among this module's writers in that: `pipeline.run` calls it
    after the escalation loop, so a full disk here would destroy the report of a run
    whose LLM spend has already gone out. Evidence that failed to be written is what
    `audit_run.py recall` exits 2 on; a run killed for it is unrecoverable.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for payload in payloads:
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log.warning("could not record what was sent to %s: %s — the run stands, but "
                     "its recall cannot be settled from its own artifacts", path, exc)


def load_sent(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
