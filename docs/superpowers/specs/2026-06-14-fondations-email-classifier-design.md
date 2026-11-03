# Fondations email-classifier — Design spec
*STU-389 — 2026-06-14*

## Contexte

Première brique du projet email-classifier. App Python séparée du kernel Studio (même modèle que wiki-creator-by-studio). Cette issue pose les fondations testables sans LLM ni API mail : structure de repo, format d'input, fixtures réelles synthétiques, et moteur de règles déterministe (étage 1).

Taxonomie de référence : [Taxonomie de classement — email-classifier](https://linear.app/studioag/document/taxonomie-de-classement-email-classifier-fdf6b27e7c5f) — 4 buckets (A essentiel, B transactionnel jetable, C infolettre/promo, D démarchage) + cas LinkedIn.

---

## 1. Structure du repo

```
email-classifier/
├── .studio/                          # pipelines, agents, contracts (STU-390+)
├── email_classifier/
│   ├── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── types.py                  # EmailInput, RuleMatch, Classification
│   │   └── schema.py                 # JSON schema + validation
│   ├── stage1/
│   │   ├── __init__.py
│   │   ├── rule_engine.py            # chargement YAML + première-match-gagne
│   │   └── policy.py                 # filtrage actions destructives
│   └── stage2/                       # vide — placeholder pour STU-390
│       └── __init__.py
├── fixtures/
│   ├── bucket_a/
│   ├── bucket_b/
│   ├── bucket_c/
│   ├── bucket_d/
│   └── linkedin/
├── rules/
│   ├── rules.yaml
│   └── policy.yaml
├── logs/                             # exclu du git (.gitignore)
├── tests/
│   ├── conftest.py
│   ├── test_schema.py
│   ├── test_rule_engine.py
│   └── test_policy.py
├── .gitignore
├── pyproject.toml
└── CLAUDE.md
```

Rationale structure sous-modules : la séparation `core/` / `stage1/` / `stage2/` anticipe l'ajout de l'étage LLM (STU-390) sans restructuration. La frontière est claire dès le départ.

---

## 2. Schéma d'input JSON

### Types (`core/types.py`)

```python
@dataclass
class EmailInput:
    thread_id: str
    from_address: str          # "from" est réservé en Python
    subject: str
    headers: dict[str, str]    # inclut List-Unsubscribe, X-Mailer, etc.
    body_snippet: str
    gmail_category: str | None  # "promotions" | "updates" | etc. — optionnel, bruyant

@dataclass
class RuleMatch:
    rule_id: str                 # id de la règle matchée — toujours renseigné (catch_all garantit un match)
    action: str                  # action finale après policy
    action_original: str | None  # renseigné si la policy a revu à la baisse
    confidence: str              # "high" | "medium" | "low"
    bucket: str | None           # "A" | "B" | "C" | "D" — None si catch_all
```

### Validation (`core/schema.py`)

- Schéma JSON (`jsonschema`) co-localisé dans `core/`
- `validate(data: dict) -> EmailInput` — lève `ValidationError` explicite si champ obligatoire manquant ou type incorrect
- `gmail_category` est optionnel dans le schéma (`required` ne l'inclut pas)
- Le schéma sert à valider les fixtures en test

### Champs obligatoires

`thread_id`, `from_address`, `subject`, `headers`, `body_snippet`

### Actions valides par bucket (référence taxonomie)

| Bucket | Actions valides |
|--------|----------------|
| A | `keep_visible`, `needs_reply` |
| B | `label_archive`, `keep_visible` |
| C | `label_archive`, `mark_for_delete` |
| D | `mark_for_delete`, `label_archive` |
| no_match | `escalate_to_llm` |

---

## 3. Fixtures

### Principe

Fixtures synthétiques mais réalistes, construites à partir des expéditeurs machine réels du recensement de la boîte perso. Les vrais courriels arrivent post-STU-391 (fetch Gmail) — les fixtures v1 servent à tester la mécanique des règles (matching, première-match-gagne, catch-all), pas la détection fine.

**Contrainte repo public** : aucune fixture ne contient un vrai courriel d'une vraie personne. Le sous-type "humain réel" (bucket A) est 100% inventé (expéditeur et contenu fictifs).

### Expéditeurs par bucket

**Bucket A** — `notify@payments.interac.ca`, `nepasrepondre@desjardins.com`, `invoice+statements@mail.anthropic.com`, `noreply@github.com`, `auto-confirm@amazon.ca`, `Transunion_Do_Not_Reply@transunion.ca` + 1 humain inventé

**Bucket B** — `shipment-tracking@amazon.ca`, `notifications@qc.intelcom.ca`, `orders@instacart.com`, `noreply@duproprio.com`, `nepasrepondre@artm.quebec`

**Bucket C** — `news@shop.kobo.com`, `marketing@engage.canva.com`, `ship@info.vercel.com`, `support@updates.linear.app`, `welcome@hello.fable.co`

**Bucket D** — `info@monpetitpret.com`, `notifications@creditkarma.ca`, `info@spalefinlandais.com`

**LinkedIn** — `jobalerts-noreply@linkedin.com` (alerte auto, bucket C) + `inmail-hit-reply@linkedin.com` (recruteur en direct, bucket A — à ne pas confondre)

### Format fixture

Chaque fixture est un fichier JSON valide contre le schéma `EmailInput`. Headers plausibles fabriqués (incluant `List-Unsubscribe` pour les cas C/D). Contenu synthétique, jamais de données identifiantes.

---

## 4. Moteur de règles déterministe (étage 1)

### Format `rules/rules.yaml`

```yaml
rules:
  - id: interac_payment
    conditions:
      from_domain: payments.interac.ca
    bucket: A
    confidence: high
    action: keep_visible

  - id: desjardins_transfer
    conditions:
      from_domain: desjardins.com
      subject_contains: "virement"
    bucket: A
    confidence: high
    action: keep_visible

  - id: linkedin_job_alert
    conditions:
      from_address: jobalerts-noreply@linkedin.com
    bucket: C
    confidence: high
    action: label_archive

  - id: catch_all          # obligatoire, toujours en dernière position
    conditions: {}
    bucket: null
    confidence: low
    action: escalate_to_llm
```

**Conditions disponibles** : `from_address` (exact), `from_domain` (domaine extrait de l'adresse), `subject_contains` (substring case-insensitive), `header_present` (clé présente dans `headers`).

**Sémantique** : AND implicite — toutes les conditions d'une règle doivent être vraies. `conditions: {}` = catch-all (matche toujours).

**Première-match-gagne** : le moteur itère dans l'ordre de déclaration et s'arrête au premier match.

**Catch-all obligatoire** : validation au chargement — si la dernière règle n'est pas `conditions: {}`, le moteur lève une erreur de configuration.

### Format `rules/policy.yaml`

```yaml
deterministic_can_finalize: true
destructive_actions_allowed:
  - mark_for_delete
frontier_log:
  file: true       # défaut, toujours actif — ne pas désactiver en prod
  stdout: false    # passer à true en dev (grep, jq)
```

### API `stage1/rule_engine.py`

```python
def load_rules(path: Path) -> list[Rule]: ...
def apply(email: EmailInput, rules: list[Rule]) -> RuleMatch: ...
```

### API `stage1/policy.py`

```python
def load_policy(path: Path) -> Policy: ...
def filter_action(match: RuleMatch, policy: Policy) -> RuleMatch: ...
```

Si l'action du match est destructive et absente de `destructive_actions_allowed` : l'action est remplacée par `label_archive`, `action_original` est renseigné avec l'action initiale.

---

## 5. Log de frontière

### Comportement

- **Fichier `.jsonl`** : toujours écrit, comportement par défaut, sans geste manuel. Append ligne par ligne avec flush immédiat — si le run plante à mi-chemin, les lignes déjà écrites sont préservées.
- **Stdout JSON** : opt-in via `frontier_log.stdout: true` dans `policy.yaml`. Désactivé par défaut. Même format.
- **`logs/`** est dans `.gitignore` — jamais commité.

### Format d'une ligne

```jsonl
{
  "run_id": "a3f1c2d4-...",
  "timestamp": "2026-06-14T18:30:00Z",
  "thread_id": "18f3a...",
  "rule_id": "interac_payment",
  "action": "keep_visible",
  "action_original": null,
  "confidence": "high",
  "bucket": "A"
}
```

`action_original` est `null` si la policy n'a pas revu l'action à la baisse. `rule_id` vaut l'`id` de la règle qui a matché — `"catch_all"` quand aucune règle spécifique n'a matché.

### Rationale

Le coût d'un log perdu est asymétrique : un run sans log = corpus d'éval irrécupérable (les courriels ne repasseront pas à l'identique). La persistance fichier ne peut pas dépendre d'une redirection stdout manuelle. Le `run_id` permet de distinguer les runs accumulés dans le même fichier et de mesurer la dérive des règles dans le temps.

---

## 6. Tests

Tous les tests tournent sur fixtures uniquement — zéro appel LLM, zéro appel API mail.

| Fichier | Ce qui est testé |
|---------|-----------------|
| `test_schema.py` | Toutes les fixtures valident sans erreur ; fixture invalide lève `ValidationError` |
| `test_rule_engine.py` | Première-match-gagne (courriel qui matche deux règles → tranché par la première) ; catch-all (aucun match → `confidence: low`, action non-destructive) ; `from_domain` extrait correctement de `from_address` |
| `test_policy.py` | `destructive_actions_allowed: []` → aucune action destructive émise même si une règle la demande ; policy permissive → action destructive préservée |

---

## Critères d'acceptation (repris de STU-389)

- Le schéma d'input valide toutes les fixtures sans erreur
- Au moins un fixture par bucket (A, B, C, D) + un cas alerte LinkedIn + un cas recruteur LinkedIn
- Le moteur charge règles + policy depuis YAML
- Première-match-gagne vérifiée par test
- Catch-all : courriel sans match → `confidence: low` + action non-destructive
- `destructive_actions_allowed: []` → aucune action destructive émise (test explicite)
- Le log de frontière est produit pour chaque courriel avec `run_id` + `timestamp`
- `pytest` passe
