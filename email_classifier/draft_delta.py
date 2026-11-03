"""How far the reviewer moved each draft before sending it (STU-1100).

`qa_bakeoff` scores a draft against the reviewer *agent* — a model judging a model.
This scores it against the person: they opened the draft, decided what to change, and
sent something. The difference between the two texts is the only quality signal the
pilot produces that nothing in the pipeline can flatter.

Pure by design. The mailbox and the Studio journal are read by the caller, so every
judgement here is testable without a grant.
"""

import difflib
import re
import statistics
import unicodedata

from email_classifier import lang, voice_baseline, voice_counts

_BASELINE = voice_baseline.load()
_LANG = lang.deployment_config()

# Sent essentially as written. Not 1.0: a mail client rewrites whitespace and a
# signature block picks up a stray newline, and neither is the reviewer disagreeing.
UNCHANGED = 0.97
# Changed, but recognisably the same reply. The upper half of this band is a comma
# and a greeting; the lower half is a rewritten sentence. Both are "the substance
# stood", which is what the bucket claims and all it claims.
LIGHT = 0.80
# Below this the reviewer wrote their own reply and kept, at most, the shape.
REWRITTEN = 0.35

UNCHANGED_LABEL = "sent unchanged"
LIGHT_LABEL = "lightly edited"
REWRITTEN_LABEL = "rewritten"
REPLACED_LABEL = "replaced"
ABANDONED_LABEL = "abandoned, reason unknown"
PENDING_LABEL = "awaiting review"

# Ordered worst-last so a summary reads down to the outcomes that cost something.
OUTCOMES = (UNCHANGED_LABEL, LIGHT_LABEL, REWRITTEN_LABEL, REPLACED_LABEL,
            PENDING_LABEL, ABANDONED_LABEL)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_INLINE_SPACE = re.compile(r"[ \t]+")

# The greeting and the closing block, as `club-voice.skill.md` counts them: 87% of the
# archive opens `Bonjour,` on its own line and 84% signs the club's name, so both are fixed
# furniture rather than part of the answer. Anchored to a whole line and refused a
# sentence mark, because `Bonne nouvelle, les inscriptions sont ouvertes.` is an answer.
#
# From `greeting_openers`/`draft_closing_openers` in the lang pack (STU-1460) — detection
# vocabulary, unlike `_SIGNATURE` below, which is voice and comes from the deployment's
# `rules/voice.yaml`. Both keys are optional (a pack lacking them just can't measure a
# greeting/closing) — `(?!)` never matches, rather than the empty group `()` that an empty
# join would compile to, which would match every line's zero-width start and misread every
# reply as all-answer.
_GREETING = re.compile(
    r"^(" + "|".join(_LANG.get("greeting_openers", ["(?!)"])) + r")\b[^.!?]{0,40}$", re.I)
_CLOSING = re.compile(
    r"^(" + "|".join(_LANG.get("draft_closing_openers", ["(?!)"])) + r")\b[^.!?]{0,60}$",
    re.I,
)
_SIGNATURE = voice_baseline.reply_signature(_BASELINE)

# What the same extractor below reads off the club's own sent replies, moved out of
# this file into `rules/voice.yaml` (STU-1472): a deployment cannot replace a Python
# literal without editing the kernel, and this deployment's own figures are what make
# `ARCHIVE` the deployment's rather than the kernel's. `None` on a deployment with no archive
# measured yet, never a zero and never another deployment's numbers borrowed as a default.
#
# `club-voice.skill.md` publishes 277 characters and 37% under 200 over 819 replies. That
# read was done by hand for STU-1124 and its extractor was never committed, so the two
# disagree by ten characters on the median and by one reply in the count: `rules/voice.yaml`
# carries both (`published_*` for the hand read). The figures here are the ones to compare
# a round against: they are what *this* code calls the answering part, so both sides of the
# comparison are cut the same way. Re-derive by running `answer_part` over every non-empty
# `body_snippet` of the archive corpus.
ARCHIVE = _BASELINE.get("archive") if _BASELINE else None

# The round before the three voice rules landed, and the thing STU-1130 asks whether we
# moved off. Re-derived by the extractor above from the two Studio journals naming
# `fb7889ca` on the staging host, for the reason the archive figures are: a round cut one
# way against a baseline cut another measures the cut.
#
# STU-1124 published 371 characters median and "three semicolons in seventeen". The
# published median sits 35 characters below what this extractor reads, and its semicolon
# turns out to be three *occurrences* across two drafts — which is why both counts are
# reported. The shortest draft agrees exactly at 148.
ROUND_BEFORE = {
    "round": "fb7889ca",
    "count": 17,
    "answer_chars_median": 406,
    "under_200_chars_share": 0.235,
    "shortest_answer_chars": 148,
    "with_semicolon": 2,
    "semicolon_occurrences": 3,
    "sent_shorter_than_ours": 9,
    "joined": 13,
    "published_answer_chars_median": 371,
    "published_semicolon_occurrences": 3,
}

# Function words, which survive translation of the subject matter. Deliberately not a
# language-detection dependency: this has to answer over three sentences of business
# French, and the shipped detectors are trained on prose.
_FR = frozenset("le la les des une un et est sont pour avec vous nous votre notre "
                "que qui dans sur par au aux ce cette ne pas plus bonjour merci".split())
_EN = frozenset("the of and is are for with you we your our that which in on by at "
                "this these not more hello thanks please will can".split())
_WORD = re.compile(r"[a-zà-öø-ÿ]+")


def normalise(text: str) -> str:
    """Strip markup and collapse whitespace, so formatting is not read as an edit."""
    return _SPACE.sub(" ", _TAG.sub(" ", text or "")).strip()


def answer_part(text: str) -> str:
    """The reply with the greeting and the closing block cut off (STU-1130).

    This is the span `club-voice.skill.md` measures at a 277-character median, and the
    only span worth measuring: every reply carries the same `Bonjour,` and the same
    `Bonne journée,` and signature, so a length read over the whole body measures how much
    furniture the club bolts on rather than how much it said.

    The closing block is taken from the last run of closing-ish lines back, not from the
    first one found: a reply can wish and then sign, and stopping at the wish would keep
    the signature in the answer. A reply with no closing at all keeps everything after the
    greeting, which is the conservative direction — over-reporting our own length.
    """
    lines = [_INLINE_SPACE.sub(" ", line).strip()
             for line in _TAG.sub(" ", text or "").splitlines()]
    lines = [line for line in lines if line]
    start = 1 if lines and _GREETING.match(lines[0]) else 0
    end = len(lines)
    for index in range(len(lines) - 1, start - 1, -1):
        if _CLOSING.match(lines[index]) or _SIGNATURE.match(lines[index]):
            end = index
        else:
            break
    return " ".join(lines[start:end]).strip()


def semicolons(text: str) -> int:
    """`;` in a reply — 0 of the archive's 831 use one, 3 of 17 drafts did (STU-1130)."""
    return (text or "").count(";")


# The three STU-1129 comma rules, base pattern name paired with its comma-carrying form,
# read straight off `voice_counts.RULES` rather than restated: a second copy of the same
# pair is a second place for the two to drift apart.
COMMA_RULES = {
    "fronted_opener": ("fronted_opener", "comma_after_fronted_opener"),
    "et": ("et", "comma_before_et"),
    "que_qui": ("que_qui", "comma_before_que_qui"),
}


def comma_ratio(texts: list[str], base: str, comma: str) -> dict | None:
    """One STU-1129 comma rule's ratio over a corpus, cut the way the archive was.

    `voice_counts.count` and its `distinct` figure, not a raw pattern count: the archive
    figures in `ARCHIVE["commas"]` were measured with blocks discarded, and a round
    that pastes the same opener into three drafts should not count that paste as three
    decisions any more than the archive did.

    `None` when the base form never occurs, for the reason `_lengths` returns `None`
    rather than 0: a round with no `et` at all has not shown the comma missing, it has
    shown nothing to judge. Also `None` when the deployment has no `base`/`comma` rule
    at all (STU-1472): a fresh deployment's `RULES` is empty until it states its own,
    and a rule nobody defined has nothing to judge either.
    """
    texts = [t for t in texts if t]
    base_pattern, comma_pattern = voice_counts.RULES.get(base), voice_counts.RULES.get(comma)
    if not texts or base_pattern is None or comma_pattern is None:
        return None
    den = voice_counts.count(texts, base_pattern)["distinct"]
    if not den:
        return None
    num = voice_counts.count(texts, comma_pattern)["distinct"]
    return {"num": num, "den": den}


def comma_ratios(texts: list[str]) -> dict:
    """All three ratios, printed as `{num, den}` pairs and never a percentage (STU-1239).

    `0 of 4` and `0 of 40` are different claims, and a share collapses them into the same
    number — exactly the thin-denominator problem `_lengths` already refuses to hide.
    """
    return {name: comma_ratio(texts, base, comma)
            for name, (base, comma) in COMMA_RULES.items()}


def language(text: str) -> str:
    """`fr`, `en`, or `unknown` — never a guess dressed as an answer.

    The reviewer corrects French to a teacher's standard and English much less
    (STU-1100), so a similarity number that does not separate the two measures their
    confidence rather than the drafter. A text this cannot call is reported `unknown`
    and kept out of both, which is the honest place for it: folding it into either
    would put an unscored draft behind a language's number.
    """
    words = set(_WORD.findall(normalise(text).casefold()))
    fr, en = len(words & _FR), len(words & _EN)
    # An accented letter is French evidence English function words cannot supply, and
    # it is what separates a short French reply from a short ambiguous one.
    accents = sum(1 for ch in text or "" if unicodedata.combining(ch)
                  or ch in "àâäçéèêëîïôöùûüÿœæ")
    if fr + accents > en:
        return "fr"
    if en > fr + accents:
        return "en"
    return "unknown"


def similarity(ours: str, theirs: str) -> float:
    return difflib.SequenceMatcher(None, normalise(ours), normalise(theirs)).ratio()


def outcome(ratio: float) -> str:
    """The bucket a ratio falls in. The bucket is the finding; the ratio is evidence.

    A draft can be three words shorter and say the opposite thing, so nothing here
    claims the middle buckets are a semantic judgement — only that the reviewer's text
    still resembles ours by that much.
    """
    if ratio >= UNCHANGED:
        return UNCHANGED_LABEL
    if ratio >= LIGHT:
        return LIGHT_LABEL
    if ratio >= REWRITTEN:
        return REWRITTEN_LABEL
    return REPLACED_LABEL


def compare(drafted: dict[str, str], sent: dict[str, str],
            still_drafted: set[str] | None = None) -> list[dict]:
    """One row per conversation we drafted, joined to what was sent on it.

    Two different absences, and merging them is what makes a mid-review round look
    like a bad one. A conversation whose draft is **still in the folder** has not been
    judged yet — `awaiting review`, and no verdict is owed. A conversation with no
    draft and no sent reply had one and lost it: `abandoned, reason unknown`.

    Unknown is the honest half. The mailbox cannot say why a draft was abandoned, and
    on round fb7889ca the abandoned one was abandoned because the parent reached the
    reviewer on Facebook — it may have been perfect. Scoring that as a failure measures
    the wrong thing, so neither absence carries a ratio.
    """
    still_drafted = still_drafted or set()
    rows = []
    for thread_id, ours in sorted(drafted.items()):
        theirs = sent.get(thread_id)
        if theirs is None:
            label = PENDING_LABEL if thread_id in still_drafted else ABANDONED_LABEL
            rows.append({"thread_id": thread_id, "outcome": label,
                         "similarity": None, "language": language(ours),
                         "ours_chars": len(normalise(ours)), "sent_chars": None,
                         "ours_answer_chars": len(answer_part(ours)),
                         "sent_answer_chars": None,
                         "ours_semicolons": semicolons(ours), "sent_semicolons": None})
            continue
        ratio = similarity(ours, theirs)
        rows.append({"thread_id": thread_id, "outcome": outcome(ratio),
                     "similarity": round(ratio, 3), "language": language(theirs),
                     "ours_chars": len(normalise(ours)),
                     "sent_chars": len(normalise(theirs)),
                     "ours_answer_chars": len(answer_part(ours)),
                     "sent_answer_chars": len(answer_part(theirs)),
                     "ours_semicolons": semicolons(ours),
                     "sent_semicolons": semicolons(theirs)})
    return rows


def summarise(rows: list[dict], ours_texts: list[str] | None = None,
              sent_texts: list[str] | None = None) -> dict:
    """Counts overall and per language, plus the unjoined conversations.

    `sent unchanged` is the number the milestone asks for, and it is reported per
    language because that is the only form of it that is about the drafter.

    `ours_texts` and `sent_texts` are the raw bodies behind `rows` — a comma ratio needs
    the corpus itself, not the per-conversation counts `rows` already reduced it to
    (STU-1239), the same reason `voice_counts.count` takes a list of replies rather than
    a count. Omitted, the comma block simply has nothing to measure, the same as an empty
    round.
    """
    def tally(subset):
        return {label: sum(1 for r in subset if r["outcome"] == label) for label in OUTCOMES}

    by_language = {
        lang: tally([r for r in rows if r["language"] == lang])
        for lang in sorted({r["language"] for r in rows})
    }
    return {"conversations": len(rows), "by_outcome": tally(rows),
            "by_language": by_language, "voice": voice(rows, ours_texts, sent_texts)}


def _lengths(chars: list[int]) -> dict:
    """Median, the short end, and the share under 200 — nulls when nothing was measured.

    A round with nothing to measure gets `null`, never 0: a median over no drafts is not
    a short round, and a zero here would read as the rules working perfectly.
    """
    if not chars:
        return {"count": 0, "answer_chars_median": None,
                "under_200_chars_share": None, "shortest_answer_chars": None}
    return {"count": len(chars),
            "answer_chars_median": round(statistics.median(chars), 1),
            "under_200_chars_share": round(sum(1 for c in chars if c < 200) / len(chars), 3),
            "shortest_answer_chars": min(chars)}


def voice(rows: list[dict], ours_texts: list[str] | None = None,
          sent_texts: list[str] | None = None) -> dict:
    """The figures STU-1124's and STU-1129's rules were meant to move, beside what they face.

    Scope, length, punctuation and now the three comma rules went into
    `club-voice.skill.md` as counts over the archive — what the club does, not evidence
    that our drafts moved toward it. These are the same counts taken on a round, so the
    direction reads off the output instead of being inferred: the median of the
    answering part, the share under 200 characters, the semicolon the archive never
    writes, and the three comma ratios STU-1239 adds.

    `sent` is the reviewer's own replies on this same round, cut by the same extractor.
    It is not a control — thirteen replies is not the archive — but it says whether a
    round's mail was unusual before a gap between `ours` and the archive is read as ours.

    The comma ratios read `ours_texts`/`sent_texts` rather than `rows`: a ratio needs the
    corpus of bodies `voice_counts.count` groups blocks over, which `rows` no longer
    carries once it has been reduced to per-conversation counts.
    """
    joined = [r for r in rows if r["sent_answer_chars"] is not None]
    return {
        "archive": ARCHIVE,
        "round_before": ROUND_BEFORE,
        "ours": {**_lengths([r["ours_answer_chars"] for r in rows]),
                 "with_semicolon": sum(1 for r in rows if r["ours_semicolons"]),
                 "semicolon_occurrences": sum(r["ours_semicolons"] for r in rows),
                 "commas": comma_ratios(ours_texts or [])},
        "sent": {**_lengths([r["sent_answer_chars"] for r in joined]),
                 "with_semicolon": sum(1 for r in joined if r["sent_semicolons"]),
                 "semicolon_occurrences": sum(r["sent_semicolons"] for r in joined),
                 "commas": comma_ratios(sent_texts or [])},
        # The round-before figure this sits against is 9 of 13, and it is the one that
        # needs both halves of a pair: a draft nobody answered says nothing about length.
        "joined": len(joined),
        "sent_shorter_than_ours": sum(
            1 for r in joined if r["sent_answer_chars"] < r["ours_answer_chars"]),
    }
