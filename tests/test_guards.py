import pytest

from email_classifier.core.types import EmailInput
from email_classifier.stage1.guards import (
    BODY_SCAN_LIMIT,
    GUARDS,
    NOT_TRANSACTIONAL,
    is_satisfied,
)


def _email(subject: str = "", body: str = "") -> EmailInput:
    return EmailInput(
        message_id="m", thread_id="t", from_address="x@y.com",
        subject=subject, headers={}, body_snippet=body,
        date="2026-06-10T00:00:00+00:00",
    )


def test_registry_contains_not_transactional():
    assert GUARDS["not_transactional"] is NOT_TRANSACTIONAL


def test_guard_satisfied_when_no_signal():
    email = _email(subject="Summer sale — new arrivals", body="Browse our new collection today")
    assert is_satisfied("not_transactional", email) is True


def test_guard_trips_on_money_fr():
    email = _email(subject="Votre facture", body="Montant total à payer")
    assert is_satisfied("not_transactional", email) is False


def test_guard_trips_on_security_en():
    email = _email(subject="New sign-in detected", body="Secure your account now")
    assert is_satisfied("not_transactional", email) is False


def test_guard_scans_body_not_just_subject():
    email = _email(subject="Hello", body="Your order has shipped")
    assert is_satisfied("not_transactional", email) is False


def test_guard_satisfied_on_newsletter_prose_using_meeting():
    email = _email(
        subject="Postmark FYI — August",
        body="A year of MCP server growth, meeting customers in Boston, and a repair shop CRM spotlight",
    )
    assert is_satisfied("not_transactional", email) is True


def test_guard_trips_on_real_meeting_request():
    email = _email(subject="Meeting request: season kickoff", body="Agenda attached")
    assert is_satisfied("not_transactional", email) is False


def test_guard_trips_on_appointment_confirmation():
    email = _email(
        subject="Votre rendez-vous chez le physio",
        body="Consultation jeudi à 14h",
    )
    assert is_satisfied("not_transactional", email) is False


# ── STU-997: the guard reads a bounded amount of body ─────────────────────────
# The two One-Click messages run 8704964d held back, in the shape that mattered:
# ordinary bulk newsletters whose only transactional words sit far below the
# fold. Reproduced here rather than read from `dumps/8704964d-….json`, which is
# gitignored and never reaches a web sandbox; shortened from 14,470 and 7,884
# characters, since what the bound turns on is where the signal sits, not how
# much filler follows it.
SUPABASE_NEWSLETTER = """Launch Week 15 wrapped on Friday. Here is everything the team built, in one place: a faster Postgres branching flow, edge functions that boot in under 50ms, realtime presence for a million concurrent peers, and a redesigned dashboard for growing teams.

Postgres branching, rebuilt
Every branch now starts from a copy-on-write snapshot instead of a fresh restore, so a branch off a 200 GB database is ready in seconds rather than twenty minutes. Branches inherit extensions, roles and row-level policies, and the diff view shows exactly which migrations diverge from production.

Edge Functions, faster cold starts
We rewrote the isolate pool. Median cold start is now 47ms across all regions, down from 210ms. Functions written against the Deno APIs need no change; redeploy and the new pool picks them up.

Realtime presence at a million peers
Presence state is now sharded per channel rather than per node, which is what kept the old ceiling at roughly 200,000 peers. The client library is unchanged.

Security and access
The new organization-level security settings page collects everything that used to live in three places: enforce 2FA for every member, require SSO sign in for the dashboard, review which personal access tokens are live, and rotate the ones that are not. Members without 2FA on their account are listed there too, so an owner can chase them down before the enforcement date rather than after it.

If you use the CLI, `supabase login` now accepts a device code flow, which means it works on a machine with no browser. Run it, read the code, paste it into the dashboard on your phone, done.

You are receiving this because you signed up for the Supabase newsletter. Manage your account preferences or unsubscribe."""

POSTMARK_NEWSLETTER = """FYI is Postmark's monthly note about the state of sending email, written for the people who own deliverability at their company rather than for the people who buy it.

This month: what a year of MCP server growth taught us about developer tooling, a look inside the analytics rebuild, and a repair shop CRM that moved 1.2 million messages a month onto our infrastructure without a single support ticket.

A year of the MCP server
We built the Postmark MCP server as a weekend experiment and it is now the third most common way our API gets called. The interesting part is not the volume, it is the shape: agents retry more politely than humans do, and they read our error strings, which means our error strings finally matter.

Customer story: a repair shop CRM
They run scheduling, estimates and reminders for eleven thousand independent shops. Their whole transactional volume — the estimate is ready, the part arrived, the invoice is attached, your delivery window moved, your part shipped — used to go out through a general-purpose provider whose deliverability they could not explain to their own customers. They moved it in a weekend.

The move itself was boring, which is the point. They pointed their DNS at us, we helped them verify the sending domain, and they cut over one message type at a time starting with the least critical one.

Ask us anything
We host an open office hour on the first Thursday of the month. No agenda, no slides — can you bring a question and we will try to answer it.

You are receiving FYI because you have a Postmark account or you subscribed on our site. Update your preferences or unsubscribe at any time."""


@pytest.mark.parametrize(
    "subject, body",
    [
        ("Launch Week 15 is a wrap", SUPABASE_NEWSLETTER),
        ("Postmark FYI — August", POSTMARK_NEWSLETTER),
    ],
    ids=["supabase", "postmark"],
)
def test_guard_satisfied_on_a_newsletter_whose_signals_sit_below_the_fold(subject, body):
    """Both were vetoed on run 8704964d, on words no reader would call the message.

    `security`/`2fa`/`account`/`sign in`/`code` for one, `invoice`/`delivery`/
    `shipped`/`verify`/`can you` for the other — all of them past the bound, in a
    footer or a customer story. Read whole, the body vetoes every archive rule;
    read to the bound, this is the promo it is.
    """
    assert is_satisfied("not_transactional", _email(subject=subject, body=body)) is True


@pytest.mark.parametrize(
    "body", [SUPABASE_NEWSLETTER, POSTMARK_NEWSLETTER], ids=["supabase", "postmark"]
)
def test_those_newsletters_do_carry_the_signals_further_down(body):
    """Pins that the fixtures above are the STU-997 shape and not just clean prose.

    Without this, narrowing the token set would make the two tests above pass for
    the wrong reason and the bound could be removed unnoticed.
    """
    assert len(body) > BODY_SCAN_LIMIT
    assert any(keyword in body.lower() for keyword in NOT_TRANSACTIONAL.all_keywords())


_SIGNAL = "Your order has shipped."
_FILLER = "New in the summer collection this week. "


def test_guard_reads_the_body_right_up_to_the_bound():
    """The bound cuts the tail, not the body.

    The signal sits at the last character the guard reads, under a kilobyte of
    text — so this fails if the bound is ever narrowed, not only if it is dropped.
    """
    head = _FILLER * ((BODY_SCAN_LIMIT - len(_SIGNAL)) // len(_FILLER) + 1)
    head = head[: BODY_SCAN_LIMIT - len(_SIGNAL)]
    body = head + _SIGNAL + _FILLER * 20
    assert len(body) > BODY_SCAN_LIMIT
    assert body.index(_SIGNAL) + len(_SIGNAL) == BODY_SCAN_LIMIT

    assert is_satisfied("not_transactional", _email(subject="Hello", body=body)) is False


def test_guard_stops_at_the_bound():
    """One character further down and the same signal no longer counts.

    The pair with the test above is the whole of STU-997: the guard still reads a
    body, it just stops reading it somewhere.
    """
    body = (_FILLER * 20)[:BODY_SCAN_LIMIT] + _SIGNAL + _FILLER * 20
    assert body.index(_SIGNAL) == BODY_SCAN_LIMIT  # the first character not read

    assert is_satisfied("not_transactional", _email(subject="Hello", body=body)) is True


def test_guard_reads_a_gmail_length_snippet_whole():
    """The Gmail arm must not move: nothing about short snippets changed.

    A `snippet` is capped at 255 characters, so the bound reaches the last one —
    here a signal sitting at the very end of a full-length snippet.
    """
    body = _FILLER * 5 + "Voici votre facture."
    assert len(body) <= BODY_SCAN_LIMIT
    assert is_satisfied("not_transactional", _email(subject="Boutique", body=body)) is False


def test_guard_trips_on_a_receipt_of_ordinary_length():
    email = _email(
        subject="Your receipt from Café Olimpico",
        body="Total: 14,50 $ — paiement par carte de crédit. Merci!",
    )
    assert is_satisfied("not_transactional", email) is False
