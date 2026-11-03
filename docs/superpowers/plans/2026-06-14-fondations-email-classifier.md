# Fondations email-classifier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Poser les fondations testables du projet email-classifier : structure du repo Python, schéma d'input JSON, fixtures synthétiques par bucket, moteur de règles déterministe (première-match-gagne) + policy + log de frontière.

**Architecture:** Package Python `email_classifier/` avec sous-modules `core/` (types + schéma), `stage1/` (moteur + policy + log), `stage2/` (placeholder). Les règles et la policy sont chargées depuis YAML (`rules/`). Les tests tournent entièrement sur fixtures JSON synthétiques — zéro appel LLM, zéro API mail.

**Tech Stack:** Python 3.11+, `pyyaml`, `jsonschema`, `pytest`, `hatchling` (build)

**Spec:** `docs/superpowers/specs/2026-06-14-fondations-email-classifier-design.md`

---

## File Map

| Fichier | Responsabilité |
|---------|----------------|
| `pyproject.toml` | Métadonnées, dépendances, config pytest |
| `.gitignore` | Exclut `logs/`, `__pycache__/`, `.venv/`, `.superpowers/` |
| `CLAUDE.md` | Commandes du projet pour les agents |
| `email_classifier/__init__.py` | Package root |
| `email_classifier/core/__init__.py` | Export du module |
| `email_classifier/core/types.py` | `EmailInput`, `RuleMatch` |
| `email_classifier/core/schema.py` | Schéma JSON + `validate()` + `load()` |
| `email_classifier/stage1/__init__.py` | Export du module |
| `email_classifier/stage1/rule_engine.py` | `Rule`, `load_rules()`, `apply()` |
| `email_classifier/stage1/policy.py` | `Policy`, `load_policy()`, `filter_action()` |
| `email_classifier/stage1/frontier_log.py` | `FrontierLogger`, `make_logger()` |
| `email_classifier/stage2/__init__.py` | Placeholder vide |
| `fixtures/bucket_a/*.json` | 7 fixtures (6 machine + 1 humain inventé) |
| `fixtures/bucket_b/*.json` | 5 fixtures |
| `fixtures/bucket_c/*.json` | 5 fixtures |
| `fixtures/bucket_d/*.json` | 3 fixtures |
| `fixtures/linkedin/*.json` | 2 fixtures (alerte auto + recruteur) |
| `rules/rules.yaml` | Jeu de règles complet (tous expéditeurs + catch-all) |
| `rules/policy.yaml` | Policy de déploiement par défaut |
| `tests/conftest.py` | Chemins partagés (`FIXTURES_DIR`, `RULES_DIR`) |
| `tests/test_schema.py` | Validation du schéma + toutes les fixtures |
| `tests/test_rule_engine.py` | première-match-gagne, catch-all, from_domain, load_rules |
| `tests/test_policy.py` | Blocage destructif, passthrough, load_policy |
| `tests/test_frontier_log.py` | Écriture fichier, append, champs, stdout opt-in |

---

## Task 1 : Scaffolding du repo

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `CLAUDE.md`
- Create: `email_classifier/__init__.py`
- Create: `email_classifier/core/__init__.py`
- Create: `email_classifier/stage1/__init__.py`
- Create: `email_classifier/stage2/__init__.py`
- Create: `tests/conftest.py`
- Create: `logs/.gitkeep`

- [ ] **Step 1 : Créer `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "email-classifier"
version = "0.1.0"
description = "Deterministic email classifier — Stage 1 rule engine"
authors = [{name = "Ariane Guay"}]
requires-python = ">=3.11"
dependencies = [
    "pyyaml>=6.0",
    "jsonschema>=4.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
]

[tool.hatch.build.targets.wheel]
packages = ["email_classifier"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2 : Créer `.gitignore`**

```
__pycache__/
*.py[cod]
.venv/
dist/
*.egg-info/
logs/
.superpowers/
```

- [ ] **Step 3 : Créer `CLAUDE.md`**

```markdown
# email-classifier

Classificateur d'emails déterministe (Stage 1) + LLM (Stage 2, STU-390).
App Python séparée du kernel Studio.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Commandes

```bash
pytest          # tous les tests
pytest -v       # verbose
```

## Structure

- `email_classifier/core/` — types partagés + validation JSON schema
- `email_classifier/stage1/` — moteur de règles déterministe + policy + log de frontière
- `email_classifier/stage2/` — placeholder LLM (STU-390)
- `fixtures/` — emails synthétiques par bucket (JSON)
- `rules/` — règles YAML + policy de déploiement
- `logs/` — logs de frontière (gitignorés)
```

- [ ] **Step 4 : Créer les fichiers `__init__.py` vides**

```bash
touch email_classifier/__init__.py
touch email_classifier/core/__init__.py
touch email_classifier/stage1/__init__.py
touch email_classifier/stage2/__init__.py
touch tests/__init__.py
mkdir -p logs && touch logs/.gitkeep
```

- [ ] **Step 5 : Créer `tests/conftest.py`**

```python
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"
RULES_DIR = REPO_ROOT / "rules"
```

- [ ] **Step 6 : Installer les dépendances**

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Expected: installation sans erreur, `pytest --collect-only` retourne 0 tests trouvés.

- [ ] **Step 7 : Commit**

```bash
git add pyproject.toml .gitignore CLAUDE.md email_classifier/ tests/ logs/
git commit -m "chore: scaffold repo structure"
```

---

## Task 2 : Types core (`core/types.py`)

**Files:**
- Create: `email_classifier/core/types.py`

- [ ] **Step 1 : Écrire `email_classifier/core/types.py`**

```python
from dataclasses import dataclass


@dataclass
class EmailInput:
    thread_id: str
    from_address: str
    subject: str
    headers: dict[str, str]
    body_snippet: str
    gmail_category: str | None = None


@dataclass
class RuleMatch:
    rule_id: str
    action: str
    action_original: str | None
    confidence: str
    bucket: str | None
```

Note : `from_address` (pas `from`) — `from` est un mot-clé Python. `rule_id` est toujours un `str` — le catch-all obligatoire garantit qu'il y a toujours un match.

- [ ] **Step 2 : Vérifier que le module s'importe**

```bash
python -c "from email_classifier.core.types import EmailInput, RuleMatch; print('OK')"
```

Expected: `OK`

- [ ] **Step 3 : Commit**

```bash
git add email_classifier/core/types.py
git commit -m "feat: add core types EmailInput and RuleMatch"
```

---

## Task 3 : Schéma JSON + validation (`core/schema.py`)

**Files:**
- Create: `email_classifier/core/schema.py`
- Create: `tests/test_schema.py` (tests minimaux — les tests de fixtures viennent en Task 5)

- [ ] **Step 1 : Écrire le test de validation minimal (fail first)**

```python
# tests/test_schema.py
import pytest
from jsonschema import ValidationError
from email_classifier.core.schema import validate
from email_classifier.core.types import EmailInput


def test_validate_returns_email_input():
    data = {
        "thread_id": "t001",
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {},
        "body_snippet": "Hello",
    }
    result = validate(data)
    assert isinstance(result, EmailInput)
    assert result.thread_id == "t001"
    assert result.gmail_category is None


def test_validate_raises_on_missing_required_field():
    data = {
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {},
        "body_snippet": "Hello",
        # thread_id manquant
    }
    with pytest.raises(ValidationError):
        validate(data)


def test_validate_accepts_optional_gmail_category():
    data = {
        "thread_id": "t002",
        "from_address": "sender@example.com",
        "subject": "Test",
        "headers": {"List-Unsubscribe": "<https://example.com/unsub>"},
        "body_snippet": "Hello",
        "gmail_category": "promotions",
    }
    result = validate(data)
    assert result.gmail_category == "promotions"
```

- [ ] **Step 2 : Lancer le test — vérifier qu'il échoue**

```bash
pytest tests/test_schema.py -v
```

Expected: `FAILED` — `ModuleNotFoundError: No module named 'email_classifier.core.schema'`

- [ ] **Step 3 : Écrire `email_classifier/core/schema.py`**

```python
import json
from pathlib import Path

import jsonschema

from email_classifier.core.types import EmailInput

_SCHEMA: dict = {
    "type": "object",
    "required": ["thread_id", "from_address", "subject", "headers", "body_snippet"],
    "properties": {
        "thread_id": {"type": "string"},
        "from_address": {"type": "string"},
        "subject": {"type": "string"},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "body_snippet": {"type": "string"},
        "gmail_category": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


def validate(data: dict) -> EmailInput:
    jsonschema.validate(data, _SCHEMA)
    return EmailInput(
        thread_id=data["thread_id"],
        from_address=data["from_address"],
        subject=data["subject"],
        headers=data["headers"],
        body_snippet=data["body_snippet"],
        gmail_category=data.get("gmail_category"),
    )


def load(path: Path) -> EmailInput:
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate(data)
```

- [ ] **Step 4 : Lancer les tests — vérifier qu'ils passent**

```bash
pytest tests/test_schema.py -v
```

Expected: 3 tests PASSED

- [ ] **Step 5 : Commit**

```bash
git add email_classifier/core/schema.py tests/test_schema.py
git commit -m "feat: add JSON schema validation"
```

---

## Task 4 : Fixtures synthétiques

**Files:**
- Create: `fixtures/bucket_a/interac_payment.json`
- Create: `fixtures/bucket_a/desjardins_transfer.json`
- Create: `fixtures/bucket_a/anthropic_invoice.json`
- Create: `fixtures/bucket_a/github_security.json`
- Create: `fixtures/bucket_a/amazon_order_confirm.json`
- Create: `fixtures/bucket_a/transunion_credit.json`
- Create: `fixtures/bucket_a/human_real.json`
- Create: `fixtures/bucket_b/amazon_shipping.json`
- Create: `fixtures/bucket_b/intelcom_delivery.json`
- Create: `fixtures/bucket_b/instacart_survey.json`
- Create: `fixtures/bucket_b/duproprio_alert.json`
- Create: `fixtures/bucket_b/artm_transit.json`
- Create: `fixtures/bucket_c/kobo_promo.json`
- Create: `fixtures/bucket_c/canva_marketing.json`
- Create: `fixtures/bucket_c/vercel_newsletter.json`
- Create: `fixtures/bucket_c/linear_changelog.json`
- Create: `fixtures/bucket_c/fable_welcome.json`
- Create: `fixtures/bucket_d/monpetitpret_spam.json`
- Create: `fixtures/bucket_d/creditkarma_spam.json`
- Create: `fixtures/bucket_d/spa_spam.json`
- Create: `fixtures/linkedin/job_alert.json`
- Create: `fixtures/linkedin/inmail_recruiter.json`

- [ ] **Step 1 : Créer les dossiers de fixtures**

```bash
mkdir -p fixtures/bucket_a fixtures/bucket_b fixtures/bucket_c fixtures/bucket_d fixtures/linkedin
```

- [ ] **Step 2 : Créer les fixtures Bucket A**

`fixtures/bucket_a/interac_payment.json`
```json
{
  "thread_id": "thread_interac_001",
  "from_address": "notify@payments.interac.ca",
  "subject": "Vous avez reçu un virement Interac de 150,00 $",
  "headers": {},
  "body_snippet": "Un virement de 150,00 $ vous a été envoyé. Déposez-le avant 30 jours.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_a/desjardins_transfer.json`
```json
{
  "thread_id": "thread_desjardins_001",
  "from_address": "nepasrepondre@desjardins.com",
  "subject": "Virement reçu — 500,00 $ de Caisse Desjardins",
  "headers": {},
  "body_snippet": "Un virement de 500,00 $ a été déposé dans votre compte le 14 juin 2026.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_a/anthropic_invoice.json`
```json
{
  "thread_id": "thread_anthropic_001",
  "from_address": "invoice+statements@mail.anthropic.com",
  "subject": "Your Anthropic invoice for June 2026",
  "headers": {},
  "body_snippet": "Invoice #INV-2026-06: $42.00 USD for Claude API usage. Due: July 1, 2026.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_a/github_security.json`
```json
{
  "thread_id": "thread_github_001",
  "from_address": "noreply@github.com",
  "subject": "New sign-in to your GitHub account",
  "headers": {},
  "body_snippet": "A new sign-in to your GitHub account was detected from Montréal, QC. If this was you, no action needed.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_a/amazon_order_confirm.json`
```json
{
  "thread_id": "thread_amazon_order_001",
  "from_address": "auto-confirm@amazon.ca",
  "subject": "Confirmation de commande #123-4567890-1234567",
  "headers": {},
  "body_snippet": "Merci pour votre commande. Total : 29,99 $. Livraison estimée : 16 juin 2026.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_a/transunion_credit.json`
```json
{
  "thread_id": "thread_transunion_001",
  "from_address": "Transunion_Do_Not_Reply@transunion.ca",
  "subject": "Mise à jour de votre dossier de crédit",
  "headers": {},
  "body_snippet": "Votre dossier de crédit TransUnion a été mis à jour. Consultez votre rapport pour voir les changements.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_a/human_real.json`
```json
{
  "thread_id": "thread_human_001",
  "from_address": "alexandre.tremblay.mtl@gmail.com",
  "subject": "Re: projet samedi",
  "headers": {},
  "body_snippet": "Ça me convient, je serai là vers 14h. Apporte le câble HDMI si t'en as un.",
  "gmail_category": "primary"
}
```

- [ ] **Step 3 : Créer les fixtures Bucket B**

`fixtures/bucket_b/amazon_shipping.json`
```json
{
  "thread_id": "thread_amz_ship_001",
  "from_address": "shipment-tracking@amazon.ca",
  "subject": "Votre colis a été expédié — arrivée prévue le 16 juin",
  "headers": {"List-Unsubscribe": "<mailto:unsub@amazon.ca>"},
  "body_snippet": "Votre commande est en route. Numéro de suivi : TBA123456789CA.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_b/intelcom_delivery.json`
```json
{
  "thread_id": "thread_intelcom_001",
  "from_address": "notifications@qc.intelcom.ca",
  "subject": "Votre livraison est en route pour aujourd'hui",
  "headers": {"List-Unsubscribe": "<mailto:unsub@intelcom.ca>"},
  "body_snippet": "Votre colis sera livré aujourd'hui entre 10h et 14h. Assurez-vous d'être disponible.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_b/instacart_survey.json`
```json
{
  "thread_id": "thread_instacart_001",
  "from_address": "orders@instacart.com",
  "subject": "Comment s'est passée votre commande ?",
  "headers": {"List-Unsubscribe": "<https://instacart.com/unsubscribe>"},
  "body_snippet": "Évaluez votre expérience d'achat du 13 juin 2026. Votre avis nous aide à améliorer le service.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_b/duproprio_alert.json`
```json
{
  "thread_id": "thread_duproprio_001",
  "from_address": "noreply@duproprio.com",
  "subject": "3 nouvelles propriétés correspondent à votre alerte",
  "headers": {"List-Unsubscribe": "<https://duproprio.com/unsub>"},
  "body_snippet": "Nouvelles propriétés dans Rosemont — Le Plateau-Mont-Royal correspondant à vos critères de recherche.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_b/artm_transit.json`
```json
{
  "thread_id": "thread_artm_001",
  "from_address": "nepasrepondre@artm.quebec",
  "subject": "Recharge OPUS — Confirmation de transaction",
  "headers": {},
  "body_snippet": "Votre carte OPUS a été rechargée avec succès. Montant : 10 passages (17,25 $). Date : 14 juin 2026.",
  "gmail_category": "updates"
}
```

- [ ] **Step 4 : Créer les fixtures Bucket C**

`fixtures/bucket_c/kobo_promo.json`
```json
{
  "thread_id": "thread_kobo_001",
  "from_address": "news@shop.kobo.com",
  "subject": "Soldes d'été — jusqu'à 50 % de rabais sur les ebooks",
  "headers": {"List-Unsubscribe": "<https://kobo.com/unsubscribe>"},
  "body_snippet": "Profitez de nos soldes d'été ! Des milliers de titres à prix réduit jusqu'au 30 juin.",
  "gmail_category": "promotions"
}
```

`fixtures/bucket_c/canva_marketing.json`
```json
{
  "thread_id": "thread_canva_001",
  "from_address": "marketing@engage.canva.com",
  "subject": "Vous n'avez pas créé de design depuis un moment",
  "headers": {"List-Unsubscribe": "<https://canva.com/unsubscribe>"},
  "body_snippet": "Revenez sur Canva ! Des modèles gratuits vous attendent. Passez à Pro pour débloquer plus de fonctionnalités.",
  "gmail_category": "promotions"
}
```

`fixtures/bucket_c/vercel_newsletter.json`
```json
{
  "thread_id": "thread_vercel_001",
  "from_address": "ship@info.vercel.com",
  "subject": "What shipped in May 2026",
  "headers": {"List-Unsubscribe": "<https://vercel.com/unsubscribe>"},
  "body_snippet": "Here's everything that shipped at Vercel last month: Edge Functions improvements, new Dashboard UI, and more.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_c/linear_changelog.json`
```json
{
  "thread_id": "thread_linear_001",
  "from_address": "support@updates.linear.app",
  "subject": "Linear changelog — juin 2026",
  "headers": {"List-Unsubscribe": "<https://linear.app/unsubscribe>"},
  "body_snippet": "Nouvelles fonctionnalités : cycles améliorés, vues personnalisées, et intégrations MCP.",
  "gmail_category": "updates"
}
```

`fixtures/bucket_c/fable_welcome.json`
```json
{
  "thread_id": "thread_fable_001",
  "from_address": "welcome@hello.fable.co",
  "subject": "Bienvenue sur Fable — votre lecture de la semaine",
  "headers": {"List-Unsubscribe": "<https://fable.co/unsubscribe>"},
  "body_snippet": "Cette semaine sur Fable : 3 recommandations basées sur votre historique de lecture.",
  "gmail_category": "updates"
}
```

- [ ] **Step 5 : Créer les fixtures Bucket D**

`fixtures/bucket_d/monpetitpret_spam.json`
```json
{
  "thread_id": "thread_mpp_001",
  "from_address": "info@monpetitpret.com",
  "subject": "Débloquez jusqu'à 5000 $ dès aujourd'hui — approbation en 24h",
  "headers": {"List-Unsubscribe": "<mailto:unsub@monpetitpret.com>"},
  "body_snippet": "Besoin d'argent rapidement ? Nos taux compétitifs et notre approbation rapide font de nous le choix idéal.",
  "gmail_category": "promotions"
}
```

`fixtures/bucket_d/creditkarma_spam.json`
```json
{
  "thread_id": "thread_ck_001",
  "from_address": "notifications@creditkarma.ca",
  "subject": "Votre crédit pré-approuvé expire dans 3 jours",
  "headers": {"List-Unsubscribe": "<https://creditkarma.ca/unsubscribe>"},
  "body_snippet": "Ne manquez pas cette offre exclusive. Votre offre de crédit pré-approuvée expire bientôt.",
  "gmail_category": "promotions"
}
```

`fixtures/bucket_d/spa_spam.json`
```json
{
  "thread_id": "thread_spa_001",
  "from_address": "info@spalefinlandais.com",
  "subject": "Offre exclusive — 20 % de rabais sur vos soins ce mois-ci",
  "headers": {"List-Unsubscribe": "<mailto:unsub@spalefinlandais.com>"},
  "body_snippet": "Profitez de notre promotion du mois : massage, soins du visage, sauna finlandais.",
  "gmail_category": "promotions"
}
```

- [ ] **Step 6 : Créer les fixtures LinkedIn**

`fixtures/linkedin/job_alert.json`
```json
{
  "thread_id": "thread_linkedin_alert_001",
  "from_address": "jobalerts-noreply@linkedin.com",
  "subject": "5 nouvelles offres d'emploi pour Développeuse Python à Montréal",
  "headers": {"List-Unsubscribe": "<https://www.linkedin.com/jobs/alert/unsubscribe>"},
  "body_snippet": "De nouvelles offres correspondent à votre alerte : Développeuse Python senior, Ingénieure ML, et plus.",
  "gmail_category": "updates"
}
```

`fixtures/linkedin/inmail_recruiter.json`
```json
{
  "thread_id": "thread_linkedin_inmail_001",
  "from_address": "inmail-hit-reply@linkedin.com",
  "subject": "Alex Chen vous a envoyé un InMail",
  "headers": {},
  "body_snippet": "Bonjour, je suis directeur technique chez TechCo. Votre profil m'intéresse pour un poste senior. Seriez-vous disponible pour un appel ?",
  "gmail_category": "primary"
}
```

- [ ] **Step 7 : Commit**

```bash
git add fixtures/
git commit -m "feat: add synthetic fixtures for all buckets and LinkedIn cases"
```

---

## Task 5 : Tests de validation des fixtures

**Files:**
- Modify: `tests/test_schema.py`

- [ ] **Step 1 : Ajouter les tests de fixtures à `tests/test_schema.py`**

Ajouter après les tests existants :

```python
import json
from pathlib import Path

from tests.conftest import FIXTURES_DIR


def _all_fixture_paths() -> list[Path]:
    return sorted(FIXTURES_DIR.rglob("*.json"))


def test_all_fixtures_are_found():
    paths = _all_fixture_paths()
    assert len(paths) == 22, f"Expected 22 fixtures, found {len(paths)}"


@pytest.mark.parametrize("path", _all_fixture_paths(), ids=lambda p: p.stem)
def test_fixture_validates_against_schema(path: Path):
    data = json.loads(path.read_text(encoding="utf-8"))
    result = validate(data)
    assert isinstance(result, EmailInput)
    assert result.thread_id
    assert result.from_address
```

- [ ] **Step 2 : Lancer les tests**

```bash
pytest tests/test_schema.py -v
```

Expected: 25 tests PASSED (3 existants + 1 count + 22 fixtures paramétrées)

- [ ] **Step 3 : Commit**

```bash
git add tests/test_schema.py
git commit -m "test: validate all fixtures against JSON schema"
```

---

## Task 6 : Moteur de règles (`stage1/rule_engine.py`)

**Files:**
- Create: `email_classifier/stage1/rule_engine.py`
- Create: `tests/test_rule_engine.py`

- [ ] **Step 1 : Écrire les tests (fail first)**

```python
# tests/test_rule_engine.py
import pytest
import yaml
from pathlib import Path

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.stage1.rule_engine import Rule, load_rules, apply
from tests.conftest import RULES_DIR


def _make_email(
    from_address: str = "sender@example.com",
    subject: str = "Test",
    headers: dict | None = None,
    thread_id: str = "t001",
) -> EmailInput:
    return EmailInput(
        thread_id=thread_id,
        from_address=from_address,
        subject=subject,
        headers=headers or {},
        body_snippet="Test body",
    )


def _rules(*rules: Rule) -> list[Rule]:
    """Helper : ajoute toujours un catch-all en fin de liste."""
    return list(rules) + [
        Rule(id="catch_all", conditions={}, bucket=None, confidence="low", action="escalate_to_llm")
    ]


def test_first_match_wins():
    rules = _rules(
        Rule(id="rule_a", conditions={"from_domain": "example.com"}, bucket="A", confidence="high", action="keep_visible"),
        Rule(id="rule_b", conditions={"from_domain": "example.com"}, bucket="C", confidence="low", action="label_archive"),
    )
    match = apply(_make_email(from_address="sender@example.com"), rules)
    assert match.rule_id == "rule_a"
    assert match.action == "keep_visible"


def test_catch_all_fires_when_no_specific_match():
    rules = _rules(
        Rule(id="rule_a", conditions={"from_domain": "specific.com"}, bucket="A", confidence="high", action="keep_visible"),
    )
    match = apply(_make_email(from_address="sender@other.com"), rules)
    assert match.rule_id == "catch_all"
    assert match.confidence == "low"
    assert match.action == "escalate_to_llm"
    assert match.bucket is None


def test_from_domain_extracted_from_address():
    rules = _rules(
        Rule(id="interac", conditions={"from_domain": "payments.interac.ca"}, bucket="A", confidence="high", action="keep_visible"),
    )
    match = apply(_make_email(from_address="notify@payments.interac.ca"), rules)
    assert match.rule_id == "interac"


def test_from_address_exact_match():
    rules = _rules(
        Rule(id="job_alert", conditions={"from_address": "jobalerts-noreply@linkedin.com"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(from_address="jobalerts-noreply@linkedin.com"), rules)
    assert match.rule_id == "job_alert"


def test_from_address_exact_no_partial_match():
    rules = _rules(
        Rule(id="job_alert", conditions={"from_address": "jobalerts-noreply@linkedin.com"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(from_address="inmail-hit-reply@linkedin.com"), rules)
    assert match.rule_id == "catch_all"


def test_subject_contains_case_insensitive():
    rules = _rules(
        Rule(id="virement", conditions={"subject_contains": "virement"}, bucket="A", confidence="high", action="keep_visible"),
    )
    match = apply(_make_email(subject="VIREMENT reçu de Desjardins"), rules)
    assert match.rule_id == "virement"


def test_header_present():
    rules = _rules(
        Rule(id="newsletter", conditions={"header_present": "List-Unsubscribe"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(headers={"List-Unsubscribe": "<https://example.com>"}), rules)
    assert match.rule_id == "newsletter"


def test_header_present_no_match_when_absent():
    rules = _rules(
        Rule(id="newsletter", conditions={"header_present": "List-Unsubscribe"}, bucket="C", confidence="high", action="label_archive"),
    )
    match = apply(_make_email(headers={}), rules)
    assert match.rule_id == "catch_all"


def test_multiple_conditions_and_logic():
    rules = _rules(
        Rule(
            id="desjardins_virement",
            conditions={"from_domain": "desjardins.com", "subject_contains": "virement"},
            bucket="A",
            confidence="high",
            action="keep_visible",
        ),
    )
    # Matche les deux conditions
    match = apply(_make_email(from_address="noreply@desjardins.com", subject="Virement reçu"), rules)
    assert match.rule_id == "desjardins_virement"

    # Matche seulement from_domain → pas de match
    match2 = apply(_make_email(from_address="noreply@desjardins.com", subject="Relevé mensuel"), rules)
    assert match2.rule_id == "catch_all"


def test_load_rules_from_yaml(tmp_path: Path):
    yaml_content = """
rules:
  - id: test_rule
    conditions:
      from_domain: example.com
    bucket: A
    confidence: high
    action: keep_visible
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    rules = load_rules(rules_file)
    assert len(rules) == 2
    assert rules[0].id == "test_rule"
    assert rules[0].conditions == {"from_domain": "example.com"}
    assert rules[1].id == "catch_all"
    assert rules[1].conditions == {}


def test_load_rules_raises_if_no_catch_all(tmp_path: Path):
    yaml_content = """
rules:
  - id: test_rule
    conditions:
      from_domain: example.com
    bucket: A
    confidence: high
    action: keep_visible
"""
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(yaml_content)
    with pytest.raises(ValueError, match="catch-all"):
        load_rules(rules_file)
```

- [ ] **Step 2 : Lancer les tests — vérifier qu'ils échouent**

```bash
pytest tests/test_rule_engine.py -v
```

Expected: tous FAILED avec `ImportError`

- [ ] **Step 3 : Écrire `email_classifier/stage1/rule_engine.py`**

```python
from dataclasses import dataclass
from pathlib import Path

import yaml

from email_classifier.core.types import EmailInput, RuleMatch


@dataclass
class Rule:
    id: str
    conditions: dict[str, str]
    bucket: str | None
    confidence: str
    action: str


def load_rules(path: Path) -> list[Rule]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = [
        Rule(
            id=r["id"],
            conditions=r.get("conditions") or {},
            bucket=r.get("bucket"),
            confidence=r["confidence"],
            action=r["action"],
        )
        for r in data["rules"]
    ]
    if not rules or rules[-1].conditions != {}:
        raise ValueError("rules.yaml must end with a catch-all rule (conditions: {})")
    return rules


def _extract_domain(address: str) -> str:
    return address.split("@")[-1].lower()


def _matches(email: EmailInput, conditions: dict[str, str]) -> bool:
    for key, value in conditions.items():
        if key == "from_address":
            if email.from_address.lower() != value.lower():
                return False
        elif key == "from_domain":
            if _extract_domain(email.from_address) != value.lower():
                return False
        elif key == "subject_contains":
            if value.lower() not in email.subject.lower():
                return False
        elif key == "header_present":
            if value not in email.headers:
                return False
    return True


def apply(email: EmailInput, rules: list[Rule]) -> RuleMatch:
    for rule in rules:
        if _matches(email, rule.conditions):
            return RuleMatch(
                rule_id=rule.id,
                action=rule.action,
                action_original=None,
                confidence=rule.confidence,
                bucket=rule.bucket,
            )
    raise RuntimeError("No catch-all rule — this should not happen after load_rules() validation")
```

- [ ] **Step 4 : Lancer les tests — vérifier qu'ils passent**

```bash
pytest tests/test_rule_engine.py -v
```

Expected: 11 tests PASSED

- [ ] **Step 5 : Commit**

```bash
git add email_classifier/stage1/rule_engine.py tests/test_rule_engine.py
git commit -m "feat: add deterministic rule engine with first-match-wins"
```

---

## Task 7 : Policy (`stage1/policy.py`)

**Files:**
- Create: `email_classifier/stage1/policy.py`
- Create: `tests/test_policy.py`

- [ ] **Step 1 : Écrire les tests (fail first)**

```python
# tests/test_policy.py
import pytest
from pathlib import Path

from email_classifier.core.types import RuleMatch
from email_classifier.stage1.policy import Policy, load_policy, filter_action


def _match(action: str, bucket: str | None = "D") -> RuleMatch:
    return RuleMatch(rule_id="test_rule", action=action, action_original=None, confidence="high", bucket=bucket)


def test_destructive_action_blocked_when_not_allowed():
    policy = Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=[],
        frontier_log_file=True,
        frontier_log_stdout=False,
    )
    result = filter_action(_match("mark_for_delete"), policy)
    assert result.action == "label_archive"
    assert result.action_original == "mark_for_delete"
    assert result.rule_id == "test_rule"  # les autres champs sont préservés
    assert result.bucket == "D"


def test_destructive_action_allowed_when_permitted():
    policy = Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=["mark_for_delete"],
        frontier_log_file=True,
        frontier_log_stdout=False,
    )
    result = filter_action(_match("mark_for_delete"), policy)
    assert result.action == "mark_for_delete"
    assert result.action_original is None


def test_non_destructive_action_never_modified():
    policy = Policy(
        deterministic_can_finalize=True,
        destructive_actions_allowed=[],
        frontier_log_file=True,
        frontier_log_stdout=False,
    )
    for action in ("keep_visible", "label_archive", "needs_reply", "escalate_to_llm"):
        result = filter_action(_match(action, bucket="A"), policy)
        assert result.action == action
        assert result.action_original is None


def test_load_policy_from_yaml(tmp_path: Path):
    yaml_content = """
deterministic_can_finalize: true
destructive_actions_allowed:
  - mark_for_delete
frontier_log:
  file: true
  stdout: false
"""
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml_content)
    policy = load_policy(policy_file)
    assert policy.deterministic_can_finalize is True
    assert policy.destructive_actions_allowed == ["mark_for_delete"]
    assert policy.frontier_log_file is True
    assert policy.frontier_log_stdout is False


def test_load_policy_empty_destructive_list(tmp_path: Path):
    yaml_content = """
deterministic_can_finalize: true
destructive_actions_allowed: []
frontier_log:
  file: true
  stdout: false
"""
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml_content)
    policy = load_policy(policy_file)
    assert policy.destructive_actions_allowed == []
    result = filter_action(_match("mark_for_delete"), policy)
    assert result.action == "label_archive"
```

- [ ] **Step 2 : Lancer les tests — vérifier qu'ils échouent**

```bash
pytest tests/test_policy.py -v
```

Expected: tous FAILED avec `ImportError`

- [ ] **Step 3 : Écrire `email_classifier/stage1/policy.py`**

```python
from dataclasses import dataclass
from pathlib import Path

import yaml

from email_classifier.core.types import RuleMatch

_DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({"mark_for_delete"})


@dataclass
class Policy:
    deterministic_can_finalize: bool
    destructive_actions_allowed: list[str]
    frontier_log_file: bool
    frontier_log_stdout: bool


def load_policy(path: Path) -> Policy:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    log_cfg = data.get("frontier_log", {})
    return Policy(
        deterministic_can_finalize=data["deterministic_can_finalize"],
        destructive_actions_allowed=data.get("destructive_actions_allowed", []),
        frontier_log_file=log_cfg.get("file", True),
        frontier_log_stdout=log_cfg.get("stdout", False),
    )


def filter_action(match: RuleMatch, policy: Policy) -> RuleMatch:
    if match.action in _DESTRUCTIVE_ACTIONS and match.action not in policy.destructive_actions_allowed:
        return RuleMatch(
            rule_id=match.rule_id,
            action="label_archive",
            action_original=match.action,
            confidence=match.confidence,
            bucket=match.bucket,
        )
    return match
```

- [ ] **Step 4 : Lancer les tests — vérifier qu'ils passent**

```bash
pytest tests/test_policy.py -v
```

Expected: 5 tests PASSED

- [ ] **Step 5 : Commit**

```bash
git add email_classifier/stage1/policy.py tests/test_policy.py
git commit -m "feat: add policy engine with destructive action filtering"
```

---

## Task 8 : Log de frontière (`stage1/frontier_log.py`)

**Files:**
- Create: `email_classifier/stage1/frontier_log.py`
- Create: `tests/test_frontier_log.py`

- [ ] **Step 1 : Écrire les tests (fail first)**

```python
# tests/test_frontier_log.py
import json
from pathlib import Path

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.stage1.frontier_log import FrontierLogger


def _make_email(thread_id: str = "t001") -> EmailInput:
    return EmailInput(
        thread_id=thread_id,
        from_address="sender@example.com",
        subject="Test",
        headers={},
        body_snippet="Hello",
    )


def _make_match(rule_id: str = "test_rule", action: str = "keep_visible", action_original: str | None = None) -> RuleMatch:
    return RuleMatch(rule_id=rule_id, action=action, action_original=action_original, confidence="high", bucket="A")


def test_log_creates_file_on_first_entry(tmp_path: Path):
    log_path = tmp_path / "logs" / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    assert not log_path.exists()
    logger.log(_make_email(), _make_match())
    assert log_path.exists()


def test_log_writes_valid_jsonl(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    logger.log(_make_email("thread_001"), _make_match())
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["thread_id"] == "thread_001"
    assert entry["rule_id"] == "test_rule"
    assert entry["action"] == "keep_visible"
    assert entry["action_original"] is None
    assert entry["confidence"] == "high"
    assert entry["bucket"] == "A"
    assert "run_id" in entry
    assert "timestamp" in entry


def test_log_appends_multiple_entries(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    logger.log(_make_email("t001"), _make_match())
    logger.log(_make_email("t002"), _make_match(rule_id="catch_all", action="escalate_to_llm"))
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["thread_id"] == "t001"
    assert json.loads(lines[1])["thread_id"] == "t002"


def test_run_id_is_consistent_within_logger(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    logger.log(_make_email("t001"), _make_match())
    logger.log(_make_email("t002"), _make_match())
    lines = log_path.read_text().strip().splitlines()
    run_ids = [json.loads(l)["run_id"] for l in lines]
    assert run_ids[0] == run_ids[1]


def test_two_loggers_have_different_run_ids(tmp_path: Path):
    log_a = tmp_path / "a.jsonl"
    log_b = tmp_path / "b.jsonl"
    logger_a = FrontierLogger(log_path=log_a)
    logger_b = FrontierLogger(log_path=log_b)
    logger_a.log(_make_email(), _make_match())
    logger_b.log(_make_email(), _make_match())
    run_a = json.loads(log_a.read_text().strip())["run_id"]
    run_b = json.loads(log_b.read_text().strip())["run_id"]
    assert run_a != run_b


def test_action_original_logged_when_policy_revised(tmp_path: Path):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path)
    match = _make_match(action="label_archive", action_original="mark_for_delete")
    logger.log(_make_email(), match)
    entry = json.loads(log_path.read_text().strip())
    assert entry["action"] == "label_archive"
    assert entry["action_original"] == "mark_for_delete"


def test_stdout_false_does_not_print(tmp_path: Path, capsys):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path, stdout=False)
    logger.log(_make_email(), _make_match())
    captured = capsys.readouterr()
    assert captured.out == ""


def test_stdout_true_prints_json(tmp_path: Path, capsys):
    log_path = tmp_path / "frontier.jsonl"
    logger = FrontierLogger(log_path=log_path, stdout=True)
    logger.log(_make_email("t_stdout"), _make_match())
    captured = capsys.readouterr()
    assert captured.out.strip()
    entry = json.loads(captured.out.strip())
    assert entry["thread_id"] == "t_stdout"
```

- [ ] **Step 2 : Lancer les tests — vérifier qu'ils échouent**

```bash
pytest tests/test_frontier_log.py -v
```

Expected: tous FAILED avec `ImportError`

- [ ] **Step 3 : Écrire `email_classifier/stage1/frontier_log.py`**

```python
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from email_classifier.core.types import EmailInput, RuleMatch
from email_classifier.stage1.policy import Policy


class FrontierLogger:
    def __init__(self, log_path: Path, stdout: bool = False) -> None:
        self.log_path = log_path
        self.stdout = stdout
        self.run_id = str(uuid.uuid4())
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, email: EmailInput, match: RuleMatch) -> None:
        entry = {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "thread_id": email.thread_id,
            "rule_id": match.rule_id,
            "action": match.action,
            "action_original": match.action_original,
            "confidence": match.confidence,
            "bucket": match.bucket,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        if self.stdout:
            print(line, flush=True)


def make_logger(policy: Policy, log_path: Path) -> FrontierLogger:
    return FrontierLogger(log_path=log_path, stdout=policy.frontier_log_stdout)
```

- [ ] **Step 4 : Lancer les tests — vérifier qu'ils passent**

```bash
pytest tests/test_frontier_log.py -v
```

Expected: 8 tests PASSED

- [ ] **Step 5 : Commit**

```bash
git add email_classifier/stage1/frontier_log.py tests/test_frontier_log.py
git commit -m "feat: add frontier logger with JSONL file output and optional stdout"
```

---

## Task 9 : Fichiers YAML de règles et validation finale

**Files:**
- Create: `rules/rules.yaml`
- Create: `rules/policy.yaml`

- [ ] **Step 1 : Créer le dossier et les fichiers YAML**

```bash
mkdir -p rules
```

- [ ] **Step 2 : Créer `rules/rules.yaml`**

```yaml
# Règles de classification email-classifier — v1
# Ordre : Bucket A (essentiel) → B (transactionnel) → C (promo) → D (démarchage) → catch-all
# Sémantique : AND implicite, première-match-gagne, catch-all OBLIGATOIRE en dernière position

rules:
  # ── Bucket A — ESSENTIEL ──────────────────────────────────────────────────
  - id: interac_payment
    conditions:
      from_domain: payments.interac.ca
    bucket: A
    confidence: high
    action: keep_visible

  - id: desjardins_financial
    conditions:
      from_domain: desjardins.com
    bucket: A
    confidence: high
    action: keep_visible

  - id: anthropic_invoice
    conditions:
      from_domain: mail.anthropic.com
    bucket: A
    confidence: high
    action: keep_visible

  - id: github_security
    conditions:
      from_domain: github.com
    bucket: A
    confidence: high
    action: keep_visible

  - id: amazon_order_confirm
    conditions:
      from_address: auto-confirm@amazon.ca
    bucket: A
    confidence: high
    action: keep_visible

  - id: transunion_credit
    conditions:
      from_domain: transunion.ca
    bucket: A
    confidence: high
    action: keep_visible

  - id: linkedin_recruiter
    conditions:
      from_address: inmail-hit-reply@linkedin.com
    bucket: A
    confidence: high
    action: keep_visible

  # ── Bucket B — TRANSACTIONNEL JETABLE ─────────────────────────────────────
  - id: amazon_shipping
    conditions:
      from_address: shipment-tracking@amazon.ca
    bucket: B
    confidence: high
    action: label_archive

  - id: intelcom_delivery
    conditions:
      from_domain: qc.intelcom.ca
    bucket: B
    confidence: high
    action: label_archive

  - id: instacart_survey
    conditions:
      from_domain: instacart.com
    bucket: B
    confidence: high
    action: label_archive

  - id: duproprio_alert
    conditions:
      from_domain: duproprio.com
    bucket: B
    confidence: high
    action: label_archive

  - id: artm_transit
    conditions:
      from_domain: artm.quebec
    bucket: B
    confidence: high
    action: label_archive

  # ── Bucket C — INFOLETTRE / PROMO ─────────────────────────────────────────
  - id: kobo_promo
    conditions:
      from_domain: shop.kobo.com
    bucket: C
    confidence: high
    action: label_archive

  - id: canva_marketing
    conditions:
      from_domain: engage.canva.com
    bucket: C
    confidence: high
    action: label_archive

  - id: vercel_newsletter
    conditions:
      from_domain: info.vercel.com
    bucket: C
    confidence: high
    action: label_archive

  - id: linear_changelog
    conditions:
      from_domain: updates.linear.app
    bucket: C
    confidence: high
    action: label_archive

  - id: fable_newsletter
    conditions:
      from_domain: hello.fable.co
    bucket: C
    confidence: high
    action: label_archive

  - id: linkedin_job_alert
    conditions:
      from_address: jobalerts-noreply@linkedin.com
    bucket: C
    confidence: high
    action: label_archive

  # ── Bucket D — DÉMARCHAGE ─────────────────────────────────────────────────
  - id: monpetitpret_spam
    conditions:
      from_domain: monpetitpret.com
    bucket: D
    confidence: high
    action: mark_for_delete

  - id: creditkarma_spam
    conditions:
      from_domain: creditkarma.ca
    bucket: D
    confidence: high
    action: mark_for_delete

  - id: spa_spam
    conditions:
      from_domain: spalefinlandais.com
    bucket: D
    confidence: high
    action: mark_for_delete

  # ── Catch-all — OBLIGATOIRE, toujours en dernière position ────────────────
  - id: catch_all
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
```

- [ ] **Step 3 : Créer `rules/policy.yaml`**

```yaml
# Policy de déploiement — email-classifier v1
# Ne pas désactiver frontier_log.file en production.

deterministic_can_finalize: true
destructive_actions_allowed:
  - mark_for_delete
frontier_log:
  file: true    # toujours actif — ne jamais passer à false en prod
  stdout: false  # passer à true en dev pour voir défiler + grep/jq
```

- [ ] **Step 4 : Écrire un test d'intégration dans `tests/test_rule_engine.py`**

Ajouter à la fin de `tests/test_rule_engine.py` :

```python
def test_load_real_rules_yaml():
    rules = load_rules(RULES_DIR / "rules.yaml")
    assert rules[-1].id == "catch_all"
    assert rules[-1].conditions == {}
    assert len(rules) > 10


def test_all_fixtures_match_expected_bucket(tmp_path: Path):
    """Chaque fixture doit matcher une règle du bucket attendu (pas catch_all),
    sauf human_real.json qui est intentionnellement sans règle dédiée."""
    from email_classifier.core.schema import load as load_fixture
    from tests.conftest import FIXTURES_DIR

    rules = load_rules(RULES_DIR / "rules.yaml")

    expected_buckets = {
        "bucket_a": "A",
        "bucket_b": "B",
        "bucket_c": "C",
        "bucket_d": "D",
    }
    no_rule_fixtures = {"human_real"}  # intentionnellement → catch_all

    for bucket_dir, expected_bucket in expected_buckets.items():
        fixture_dir = FIXTURES_DIR / bucket_dir
        for fixture_path in sorted(fixture_dir.glob("*.json")):
            email = load_fixture(fixture_path)
            match = apply(email, rules)
            if fixture_path.stem in no_rule_fixtures:
                assert match.rule_id == "catch_all", f"{fixture_path.name}: expected catch_all"
            else:
                assert match.bucket == expected_bucket, (
                    f"{fixture_path.name}: expected bucket {expected_bucket}, got {match.bucket} (rule: {match.rule_id})"
                )

    # LinkedIn : alerte → bucket C, recruteur → bucket A
    linkedin_dir = FIXTURES_DIR / "linkedin"
    alert = load_fixture(linkedin_dir / "job_alert.json")
    assert apply(alert, rules).bucket == "C"

    recruiter = load_fixture(linkedin_dir / "inmail_recruiter.json")
    assert apply(recruiter, rules).bucket == "A"
```

- [ ] **Step 5 : Lancer tous les tests**

```bash
pytest -v
```

Expected: tous PASSED. Le résumé final doit montrer les 22 fixtures paramétrées + tous les tests unitaires.

- [ ] **Step 6 : Commit final**

```bash
git add rules/ tests/test_rule_engine.py
git commit -m "feat: add rules.yaml, policy.yaml, and integration test against all fixtures"
```

---

## Validation finale

- [ ] `pytest` passe sans erreur
- [ ] `git log --oneline` montre un commit propre par tâche
- [ ] `logs/` existe et est gitignorée (`git status` ne la montre pas)
- [ ] Aucune fixture ne contient de données personnelles réelles (vérifier `human_real.json` — adresse inventée)
