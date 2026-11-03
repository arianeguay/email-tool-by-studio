from dataclasses import dataclass

from email_classifier.core.types import EmailInput


@dataclass(frozen=True)
class GuardSet:
    """A named set of contradicting-signal keywords.

    The guard is a presence detector biased to OVER-trigger: a missed signal
    would let a transactional/security/human email be wrongly archived, so
    detection is deliberately generous and bilingual (FR + EN). A false trip
    only keeps a promo visible — the acceptable failure direction.
    """

    name: str
    money: frozenset[str]
    order: frozenset[str]
    deadline: frozenset[str]
    security: frozenset[str]
    appointment: frozenset[str]
    human: frozenset[str]

    def all_keywords(self) -> frozenset[str]:
        return (
            self.money | self.order | self.deadline
            | self.security | self.appointment | self.human
        )


# Generous, bilingual corpus. Biased to OVER-trigger: a missed signal would let
# a transactional/security/human email be wrongly archived; a false trip only
# keeps a promo visible (it still reaches the LLM). Expanded after the STU-396
# final review flagged real protected-mail variants the starter set missed
# (OTP/2FA, email confirmation, receipts/billing, appointments). This is Ariane's
# owned artifact — keep growing the word-lists with real signals from the mailbox it serves;
# keep the six-category structure. Multi-word tokens (e.g. "credit card") are used
# where a bare substring would collide with promo words ("incredible"); a leading
# space (e.g. " cad") does the same for short tokens (avoids "cadeau") — don't strip it.
NOT_TRANSACTIONAL = GuardSet(
    name="not_transactional",
    money=frozenset({
        "$", " cad", "dollars", "montant", "total", "solde", "prix",
        "facture", "invoice", "paiement", "payment", "remboursement", "refund",
        "reçu", "receipt", "billing", "facturation", "subscription", "abonnement",
        "renew", "renouvel", "statement", "relevé", "credit card",
        "carte de crédit", "declined", "refusée",
    }),
    order=frozenset({
        "commande", "order", "suivi", "tracking", "expédié", "shipped",
        "livraison", "delivery", "colis",
    }),
    deadline=frozenset({
        "échéance", "due", "expire", "deadline", "dernier délai", "avant le",
        "action required", "action requise", "overdue", "en retard", "past due",
    }),
    security=frozenset({
        "sécurité", "security", "mot de passe", "password", "connexion",
        "sign-in", "sign in", "vérification", "verify", "compte", "account",
        "code", "otp", "passcode", "one-time", "login", "log in",
        "confirm", "confirmer", "authentification", "authentication",
        "reset", "réinitialis", "2fa", "two-factor",
    }),
    appointment=frozenset({
        "rendez-vous", "rdv", "appointment", "réservation", "booking",
        "meeting invite", "meeting request", "réunion", "consultation",
        "invitation", "reminder", "rappel", "calendar", "calendrier",
        "scheduled", "planifié",
    }),
    human=frozenset({
        "peux-tu", "can you", "merci de", "réponds", "reply please",
        "pouvez-vous", "could you", "let me know", "what do you think",
        "your thoughts",
    }),
)

# How much of the body the guard reads (STU-997).
#
# The word-lists above were sized against a Gmail `snippet`, which the API caps:
# on `dumps/gmail-dump.json` the longest is 255 characters, the median 253. Since
# STU-935 the Outlook projection puts the whole `uniqueBody` in `body_snippet`,
# uncut — median 947 characters on run 8704964d, maximum 14,470 — so the same
# generous token set was suddenly read against fifty times the text it was
# calibrated for. The result was not a bias, it was a veto: `catch_all` fired on
# 26 of 26 messages, `ratio_addressable` came out 0, and both One-Click messages
# were held on words sitting in a newsletter's footer.
#
# So the guard reads a bounded, provider-comparable amount of text rather than
# however much the provider happened to return: the subject in full, plus a
# LEADING SLICE of the body of the length the tokens were calibrated against.
# 255 is that length on both sides of the projection — it is Gmail's snippet cap
# and it is also what Graph caps `bodyPreview` at, so the bound is not a Gmail
# artifact imported into Outlook. A transactional signal that appears only past
# it is in the footer of bulk mail, not in the message; that is not
# transactional mail. Cutting can only drop a hit, never invent one, so the
# guard's over-trigger bias is unchanged for anything the bound still covers —
# what it stops being is unconditional on one provider.
#
# Raising this bound re-decalibrates the token set, and narrowing tokens one at a
# time to compensate does not converge (STU-892 narrowed `meeting`; the same
# newsletter was filed again five tokens later). If a projection change moves how
# much text `body_snippet` carries, that is a reason to re-read this number, not
# to widen it.
BODY_SCAN_LIMIT = 255

GUARDS: dict[str, GuardSet] = {NOT_TRANSACTIONAL.name: NOT_TRANSACTIONAL}


def is_satisfied(guard_name: str, email: EmailInput) -> bool:
    """True when NO contradicting signal appears in subject + the head of the body.

    Satisfied=True means a universal rule is ALLOWED to archive. Any keyword
    hit → False → the email stays visible (falls through to LLM escalation).

    The body is read up to `BODY_SCAN_LIMIT` characters only — see the note there
    for why the amount of text scanned is bounded rather than whatever the
    provider returned. The subject is never cut: it is short on every provider,
    and it is where a real transactional message says so.
    """
    guard = GUARDS[guard_name]
    body = email.body_snippet[:BODY_SCAN_LIMIT]
    haystack = f"{email.subject}\n{body}".lower()
    return not any(keyword in haystack for keyword in guard.all_keywords())
