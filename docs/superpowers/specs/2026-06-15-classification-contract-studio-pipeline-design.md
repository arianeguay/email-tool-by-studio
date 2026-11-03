# Contrat de classification + pipeline Studio — Design spec
*STU-390 — 2026-06-15*

## Contexte

Étage 2 (classification LLM) + étage 3 (validation RALPH) de l'app email-classifier.
Seuls les courriels que l'étage 1 (STU-389) a marqués `escalate_to_llm` (zone grise)
arrivent ici ; le reste a déjà été tranché par les règles déterministes.

Taxonomie de référence : [Taxonomie de classement — email-classifier](https://linear.app/studioag/document/taxonomie-de-classement-email-classifier-fdf6b27e7c5f)
(4 buckets A/B/C/D + cas LinkedIn). Le schéma d'input est déjà **provider-neutre**
(`provider_category` + `date`) depuis STU-391 (mergé) — STU-390 en dépend, ne le redéfinit pas.

### Décisions d'architecture (figées en brainstorming)

1. **Le moteur Studio pilote le LLM + la boucle RALPH** via des configs `.studio/`.
   Studio est la couche de gouvernance au-dessus du meilleur modèle (Anthropic, modèle fort).
2. **Un validateur, deux appelants.** La table `action`↔`bucket` et toute la logique de
   validation vivent **une seule fois** en Python (`validate_classification`). Deux appelants :
   (a) pytest l'appelle directement sur du JSON mocké ; (b) le moteur Studio l'appelle via un
   **CLI** branché sur le hook `validators` du contrat. Aucune duplication.
3. **Validation = hook `validators` de Studio (ajouté pour ce projet).** Le contrat déclaratif de
   Studio ne valide nativement que la *présence* des champs (`required_fields`). Le hook
   `validators` (ajouté au moteur — voir Studio `external-validator`) exécute une commande qui
   reçoit **la vraie sortie de l'étage** en JSON sur stdin et renvoie `{valid, errors}`. Sortie
   invalide → la commande échoue → RALPH relance (max 3) avec les `errors` en feedback. Le hook
   valide la **sortie réelle** (pas des arguments d'outil auto-rapportés) : l'agent ne peut pas
   tricher.

---

## 1. Flux de données & structure

```
stage1 rule engine  ──escalate_to_llm──▶  [STU-390]  ──▶  Classification JSON validée
(STU-389, mergé)       (zone grise only)   pipeline Studio       (consommé par STU-392)
                                            ├─ agent classifier  → LLM (Anthropic, modèle fort)
                                            ├─ contrat: required_fields + validators:[classification]
                                            └─ boucle RALPH (max 3) → hook valide la sortie réelle
                                                                       (python -m ...validate_cli, stdin)
```

### Fichiers nouveaux / modifiés

```
email_classifier/
  core/
    types.py          # + dataclass Classification (forme contractuelle figée)
    contract.py       # NOUVEAU — source de vérité unique :
                      #   ALLOWED_ACTIONS, enums, ValidationResult,
                      #   validate_classification(data) -> ValidationResult{valid, errors}
  stage2/
    validate_cli.py   # NOUVEAU — lit la classification JSON sur stdin, appelle
                      #   validate_classification, imprime {valid, errors},
                      #   exit 0 si valide / non-zéro si invalide
.studio/                     # NOUVEAU — configs du moteur Studio
  config.yaml                # provider: anthropic, model: claude-opus-4-8
  invariants.md              # INV-08 binaire ; jamais de suppression réelle ; neutralité provider
  skills/taxonomy.skill.md   # taxonomie A/B/C/D + règle de neutralité provider
  agents/classifier.agent.yaml          # classe la zone grise, émet le JSON contractuel
  contracts/classification.contract.yaml  # required_fields + validators:[classification]
  pipelines/classify.pipeline.yaml         # une étape : agent + contrat + RALPH
tests/
  test_contract.py        # NOUVEAU — validateur sur JSON mocké, par paire interdite
  test_validate_cli.py    # NOUVEAU — stdin JSON → exit codes + {valid,errors} == validate_classification
```

### Frontières

- `core/contract.py` est **pur** (aucun LLM, aucune I/O) — le cœur déterministe et testé.
- `stage2/validate_cli.py` lit stdin et appelle le validateur pur ; c'est l'unique glue exposant
  le validateur au hook Studio. **Aucune dépendance Anthropic SDK** côté Python — le moteur Studio
  fait les appels LLM.
- La table `action`↔`bucket` est écrite **une fois** ; pytest et Studio appellent le même validateur.

---

## 2. Cœur du contrat — `core/contract.py` (pur & testé)

### Constantes (source de vérité)

```python
BUCKETS      = ("A", "B", "C", "D")
CONFIDENCES  = ("high", "medium", "low")
ACTIONS      = ("keep_visible", "label_archive", "mark_for_delete", "needs_reply", "escalate_to_llm")

# INV-08 — cohérence action↔bucket
ALLOWED_ACTIONS = {
    "A": frozenset({"keep_visible", "needs_reply"}),
    "B": frozenset({"label_archive", "keep_visible"}),
    "C": frozenset({"label_archive", "mark_for_delete"}),
    "D": frozenset({"mark_for_delete", "label_archive"}),
}   # escalate_to_llm permis pour TOUT bucket (fallback)
```

### Dataclass `Classification` (7 champs figés)

`thread_id, bucket, subtype, confidence, suggested_action, suggested_label (str|null), reasoning`.
`subtype` reste une chaîne libre (guidage, pas un enum validé) — conforme au schéma figé.

### `validate_classification(data) -> ValidationResult`

```python
@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]
```

Vérifie, en collectant **toutes** les erreurs (jamais fail-fast) :

1. **Présence + type** des 7 champs (`suggested_label` str|null), aucun champ étranger.
2. **Enums** : `bucket`/`confidence`/`suggested_action`.
3. **Table action↔bucket (INV-08)** : `suggested_action` ∈ `ALLOWED_ACTIONS[bucket] ∪ {escalate_to_llm}`.

`valid = not errors`. Binaire, pas de score. Pur. La liste `errors` **est** le feedback enrichi
renvoyé à RALPH par le hook.

---

## 3. Configs `.studio/` (le moteur Studio pilote LLM + RALPH)

| Fichier | Rôle |
|---|---|
| `config.yaml` | `provider: anthropic`, `model: claude-opus-4-8`, `apiKey: ${ANTHROPIC_API_KEY}` |
| `invariants.md` | INV-08 (validation binaire) ; jamais de suppression réelle ; **neutralité provider** |
| `skills/taxonomy.skill.md` | taxonomie A/B/C/D + règle de neutralité |
| `agents/classifier.agent.yaml` | input = courriels escaladés ; `skills: [taxonomy]` ; émet le JSON contractuel |
| `contracts/classification.contract.yaml` | `schema.required_fields` (7 champs, gate de présence) **+ `validators: [{name: classification, command: "python -m email_classifier.stage2.validate_cli"}]`** + RALPH `max_attempts: 3` |
| `pipelines/classify.pipeline.yaml` | une étape `classify` : agent classifier + contrat + RALPH |

### Mécanique d'enforcement (hook `validators`)

1. L'agent produit la classification (sortie de l'étage).
2. Le moteur pipe cette sortie en JSON sur stdin de `validate_cli`, qui appelle
   `validate_classification` et renvoie `{valid, errors}` (exit non-zéro si invalide).
3. Invalide → RALPH relance (max 3) avec `errors` en feedback ; pas de crash. La validation porte
   sur la **sortie réelle**, donc INV-08 est réellement garanti.

### Neutralité provider (exigence #4)

Encodée dans `taxonomy.skill.md` + `invariants.md` : classer à partir de features **universelles**
seulement (expéditeur, sujet, headers, `List-Unsubscribe`) ; `provider_category` = indice faible,
jamais structurant ; un courriel sans catégorie (Outlook) classé avec la même qualité. Le contrat
de sortie n'a **aucun champ provider** (un champ étranger comme `provider_category` dans la sortie
est rejeté par le validateur).

---

## 4. Tests & couverture des critères d'acceptation

| Fichier de test | Couvre |
|---|---|
| `test_contract.py` | Toute **paire interdite (bucket, action) → `valid: false`** (paramétré) ; `escalate_to_llm` valide pour tout bucket ; `confidence` hors enum / champ manquant / mauvais type / champ étranger → invalide ; `valid` binaire ; `errors` non vide en échec ; un cas valide par bucket |
| `test_validate_cli.py` | classification valide sur stdin → exit 0 + `{"valid": true}` ; invalide → exit non-zéro + `errors` ; le verdict du CLI == `validate_classification` |
| tests de schéma existants (STU-391) | Input accepté **avec et sans** `provider_category` — moitié déterministe de la neutralité |

### Frontière automatisé / manuel (assumée)

Critères qui exigent un **vrai LLM** (pas du pytest pur) : *classification zone grise (5–10 cas)*,
*retry RALPH sur fail*, *équivalence de neutralité avec/sans `provider_category`*. → Validés par
`studio run` (éval manuelle, runbook). Le retry est garanti structurellement par le hook
`validators` + `ralph max_attempts: 3`.

### Mapping critères d'acceptation → preuve

| Critère STU-390 | Preuve |
|---|---|
| Schéma de contrat implémenté tel que figé | `Classification` + `validate_classification` |
| Validateur rejette toute paire `action`↔`bucket` hors table | `test_contract.py` (paramétré) |
| Validateur rejette `confidence` hors enum / champ manquant / mauvais type | `test_contract.py` |
| Validateur retourne `valid: boolean` binaire (INV-08) | `ValidationResult.valid` |
| Pipeline classe un échantillon zone grise (5–10 cas) | `studio run` — éval manuelle |
| Fail de validation → retry RALPH avec feedback, pas crash | hook `validators` + `ralph max_attempts: 3` + éval manuelle |
| Validation sur JSON mocké sans vrai LLM | `test_contract.py` (validateur isolé) |
| Classification indépendante de toute feature provider (avec/sans `provider_category`) | schéma : pytest ; qualité : éval manuelle |
| `pytest` passe | CI / `pytest` |

---

## 5. Hors scope (YAGNI)

- Validation de `subtype` contre un enum par bucket — chaîne libre, conforme au schéma figé.
- Règle `suggested_label` non-null quand `action == label_archive` — non exigée.
- Orchestrateur bout-en-bout + rapport (STU-392) ; anonymisation PII (STU-393).
- Tout appel LLM côté Python / SDK Anthropic — le moteur Studio s'en charge.

## 6. Dépendance amont

Requiert le hook Studio `validators` (branche Studio `arianedguay/studio-external-validator-hook`,
commit `fc6dcf2`). Doit être mergé/installé avant l'éval manuelle `studio run`. Les tests pytest de
STU-390 n'en dépendent pas (ils appellent `validate_classification`/`validate_cli` directement).
