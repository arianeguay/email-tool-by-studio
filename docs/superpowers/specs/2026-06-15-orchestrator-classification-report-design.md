# Orchestrateur bout-en-bout + rapport de classification — Design spec
*STU-392 — 2026-06-15 — lecture seule, scope v1*

## Contexte

La glue qui relie les trois étages en un run complet, façon `run_wiki.py` de Wiki Creator.
Point d'entrée qui prend un dump JSON figé (ou un fetch réel STU-391), passe tout dans la
moulinette, et sort un **rapport de classification**. **Lecture seule** : zéro action sur la
boîte (pas d'`apply_label`, pas de `trash`). Le classifier lit, propose, sort un rapport.

Dépend de STU-389 (étage 1 déterministe, mergé), STU-390 (pipeline Studio `classify` +
validation RALPH, mergé) et STU-391 (fetch Gmail read-only + dump JSON, mergé). Cette issue
ne redéfinit aucun de ces contrats — elle les enchaîne.

### Référence d'architecture : wiki-creator

Le pattern est éprouvé dans `wiki-creator-by-studio` :

1. **L'orchestrateur séquence des sous-processus**, il n'embarque pas Studio en mémoire
   (`run_wiki.py` → `subprocess.run(["studio", "run", ...])`).
2. **Classification par item via `--json`** : `scripts/relationship_extraction.py` écrit
   l'item dans un YAML temporaire, lance `studio run <pipeline> --input-file tmp --json`,
   parse `stdout`, et récupère `stages[].output` là où `status == "success"` — avec un
   repli sur le log JSONL du run (`_load_studio_stage_output`). C'est l'analogue exact du
   cas email (classer chaque courriel escaladé).
3. **Taxonomie d'erreurs retournée comme donnée** (`studio_cli_missing`, `studio_run_timeout`,
   `studio_run_failed`, `studio_run_output_missing`, `studio_output_json_parse_error`).

L'orchestrateur email est *plus simple* que `run_wiki.py` : l'étage 1 est du Python pur en
mémoire (pas un pipeline Studio), et il n'y a qu'un seul pipeline Studio (`classify`),
appelé une fois par courriel escaladé — pas de DAG multi-pipelines ni de graphe de
dépendances de fichiers d'artefacts.

---

## Décisions d'architecture (figées en brainstorming)

1. **Frontière étage 2 = un `Stage2Classifier` Protocol Python + impl sous-processus.**
   L'orchestrateur ne connaît que le Protocol. L'impl réelle (`StudioCliClassifier`) shell-out
   par courriel escaladé ; les tests injectent un `FakeClassifier` qui renvoie des
   `Classification` figées. C'est ce qu'exige le critère « pytest passe sur le chemin orchestré
   avec LLM mocké ».
2. **Un appel Studio par courriel escaladé.** L'agent `classifier` (STU-390) classe UN SEUL
   courriel ; on respecte ce contrat. Sur une boîte perso l'ensemble escaladé est petit (tout
   l'argument d'économie : la majorité est tranchée sans token).
3. **Logique d'orchestration dans un package importable** (`email_classifier/orchestrate/`),
   `run_email.py` = mince wrapper CLI (comme `scripts/fetch_gmail.py` au-dessus de `fetch/gmail/`).
   Testabilité du chemin orchestré.
4. **Rapport double sortie** : résumé humain sur stdout (audit manuel) + JSON structuré sur
   disque (`reports/<run-id>.json`, donnée d'éval durable) — comme wiki-creator persiste
   `processing_output/*.json` ET `run_wiki.py` imprime un statut.
5. **Échec étage 2 par courriel : enregistrer + continuer ; erreurs de config : fail loud.**
   Une erreur LLM par courriel (timeout, RALPH épuisé, erreur Studio) devient une entrée
   `status: "error"` dans le rapport et le run continue. Les erreurs de config/setup (CLI
   `studio` absente, `rules.yaml`/`policy.yaml` malformés, dump non conforme au schéma) font
   échouer le run AVANT tout traitement.

---

## 1. Structure des fichiers

```
email_classifier/
  orchestrate/
    __init__.py
    pipeline.py        # NOUVEAU — run(emails, rules, policy, stage2, logger) -> Report
                       #   la boucle cœur + l'invariant de frontière
    stage2_client.py   # NOUVEAU — Stage2Classifier (Protocol), Stage2Result,
                       #   StudioCliClassifier (subprocess + --json + repli JSONL)
    report.py          # NOUVEAU — EmailVerdict / Report dataclasses, agrégation,
                       #   rendu JSON + rendu humain
run_email.py           # NOUVEAU — CLI mince : charge dump | fetch, construit les deps,
                       #   appelle run(), écrit reports/<run-id>.json + imprime le résumé
Makefile               # NOUVEAU — points d'entrée (run / fetch / test / clean), façon wiki-creator
tests/
  test_orchestrate_pipeline.py  # NOUVEAU — invariant de frontière + agrégation (FakeClassifier)
  test_stage2_client.py         # NOUVEAU — parsing stdout/JSONL + taxonomie d'erreurs (subprocess mocké)
  test_report.py                # NOUVEAU — maths d'agrégation + forme JSON + format humain
reports/                        # gitignoré (comme logs/) — rapports de run
```

L'orchestrateur ne dépend que de pièces pures existantes (`stage1.rule_engine`,
`stage1.policy`, `stage1.frontier_log`, `core.contract`, `core.types`) + le Protocol étage 2.
**Aucune dépendance Anthropic SDK côté Python** — Studio fait les appels LLM.

---

## 2. Flux de données — l'invariant de frontière

```
charger les courriels (dump JSON figé | fetch STU-391)
   │
   ▼  pour chaque courriel :
apply(email, rules) -> RuleMatch                 # étage 1 déterministe (STU-389)
filter_action(match, policy)                     # downgrade mark_for_delete, etc.
logger.log(email, match)                         # ligne JSONL du log de frontière (toujours)
   │
   ├─ match.action == "escalate_to_llm" ──▶ stage2.classify(email) -> Stage2Result
   │                                          (LE seul chemin vers le LLM)
   └─ sinon ───────────────────────────────▶ verdict déterministe depuis le RuleMatch
   │
   ▼
agréger tous les verdicts -> Report
```

Le branchement `if match.action == "escalate_to_llm"` est l'unique goulot qui garantit :
- **les courriels tranchés par l'étage 1 ne montent jamais au LLM**, et
- **seuls les `escalate_to_llm` passent à l'étage 2**.

Les deux sont directement testables avec un `FakeClassifier` qui enregistre les `thread_id`
qu'on lui a demandé de classer. v1 suppose `deterministic_can_finalize: true` (le cas `false`
est hors scope).

---

## 3. Frontière étage 2 — `stage2_client.py`

```python
@dataclass
class Stage2Result:
    classification: Classification | None   # présent ssi ok
    error: str | None                       # une étiquette de la taxonomie, sinon None
    # ok == (error is None and classification is not None)

class Stage2Classifier(Protocol):
    def classify(self, email: EmailInput) -> Stage2Result: ...
```

### `StudioCliClassifier` (impl réelle)

Reprend la mécanique éprouvée de `relationship_extraction.py` :

1. Sérialiser l'`EmailInput` (les 7 champs neutres) dans un YAML temporaire.
2. `studio run classify --input-file <tmp> --json` (+`--provider mock` si demandé),
   `subprocess.run(..., capture_output=True, text=True, timeout=...)`, `cwd` = racine projet.
3. `returncode != 0` → `Stage2Result(error="studio_run_failed")`.
4. Parser `stdout` en JSON → `run_payload`. Échec → `studio_output_json_parse_error`.
5. Chercher dans `run_payload["stages"]` l'étape `stage_name == "classify"` avec
   `status == "success"` → `output` (le dict de classification).
6. *(Repli différé, hors v1)* : `relationship_extraction.py` — l'analogue le plus proche
   (classification par item) — fait confiance à `stdout` seul ; le repli JSONL de
   `generate_wiki_pages.py` n'existe que pour les sorties volumineuses. Une classification de
   7 champs tient toujours dans `stdout` de `studio run --json`. On omet donc le repli en v1
   (YAGNI) ; à rajouter si jamais une sortie manquante est observée en pratique.
7. `output` re-validé via `core.contract.validate_classification` (défense en profondeur — le
   hook RALPH l'a déjà validé) puis projeté en `Classification`. Output absent →
   `studio_run_output_missing` ; output présent mais qui re-échoue la validation →
   `studio_run_output_invalid` (ne devrait pas arriver si le hook a fait son travail, mais on
   ne fait jamais confiance en aveugle à une sortie LLM — fail visible, jamais de misroute).

Étiquettes d'erreur : `studio_cli_missing` (FileNotFoundError), `studio_run_timeout`
(TimeoutExpired), `studio_run_failed`, `studio_output_json_parse_error`,
`studio_run_output_missing`, `studio_run_output_invalid`.

### `FakeClassifier` (aide de test, dans `tests/`)

Mappe `thread_id -> Stage2Result`, enregistre la liste des `thread_id` reçus (pour asserter
l'invariant de frontière). Aucun sous-processus, aucun réseau.

---

## 4. Modèle de rapport — `report.py`

```python
@dataclass
class EmailVerdict:
    thread_id: str
    path: str               # "deterministic" | "llm"
    status: str             # "ok" | "error"
    bucket: str | None
    suggested_action: str | None
    confidence: str | None
    rule_id: str | None     # chemin déterministe (incl. catch_all pour les escaladés)
    subtype: str | None     # chemin LLM
    reasoning: str | None   # chemin LLM
    error: str | None       # étiquette de la taxonomie si status == "error"

@dataclass
class Report:
    run_id: str
    totals: dict            # {emails, deterministic, llm, errors}
    ratio_deterministic: float
    by_bucket: dict[str, int]
    by_action: dict[str, int]
    verdicts: list[EmailVerdict]
```

- `run_id` = le `run_id` du `FrontierLogger` → lie le rapport au log de frontière.
- **JSON** : `reports/<run-id>.json` (rapport structuré complet, donnée d'éval durable).
- **Humain** : résumé sur stdout — totaux, ratio déterministe/LLM mis en avant (argument
  d'économie), répartition par bucket et par action, et la liste des escaladés avec leur raison
  (`catch_all`) et leur verdict LLM (ou erreur).

Exemple de sortie humaine :

```
=== Classification Report (run abc123) ===
22 emails | 18 deterministic (82%) | 4 -> LLM (18%) | 0 errors
By bucket:  A:7  B:6  C:5  D:4
By action:  keep_visible:7  label_archive:11  mark_for_delete:4
Escalated (frontier):
  thread_9   catch_all -> LLM -> C / label_archive
```

---

## 5. CLI — `run_email.py`

```
python run_email.py --dump dumps/gmail-dump.json           # dump figé (sans Gmail)
python run_email.py --fetch --query "..." --max-results N   # fetch réel read-only (STU-391)

Options :
  --provider anthropic|mock     # défaut anthropic ; mock => --provider mock passé à studio
  --report-dir reports/         # défaut reports/
  --rules rules/rules.yaml       # défaut
  --policy rules/policy.yaml     # défaut
```

`--dump` et `--fetch` sont mutuellement exclusifs (l'un requis). Le wrapper :
charge les courriels (lit le dump et valide via `core.schema`, ou appelle `fetch.gmail`),
construit `rules`/`policy`/`StudioCliClassifier`/`FrontierLogger`, appelle
`orchestrate.pipeline.run(...)`, écrit le JSON et imprime le résumé.

### Makefile — points d'entrée

Façon `run_wiki.py` / `Makefile` de wiki-creator : cibles `.PHONY` enrobant l'orchestrateur,
le fetch et pytest, avec des variables `?=` surchargeables en ligne de commande
(`make run DUMP=...`).

```makefile
.PHONY: run run-fetch fetch test clean

DUMP     ?= dumps/gmail-dump.json
QUERY    ?=
MAX      ?= 100
PROVIDER ?= anthropic

run:                 # run complet sur un dump figé
	python run_email.py --dump $(DUMP) --provider $(PROVIDER)

run-fetch:           # fetch read-only puis run de bout en bout
	python run_email.py --fetch --query "$(QUERY)" --max-results $(MAX) --provider $(PROVIDER)

fetch:               # fetch seul -> écrit le dump JSON (STU-391)
	python scripts/fetch_gmail.py --query "$(QUERY)" --max-results $(MAX) --output $(DUMP)

test:
	pytest

clean:               # supprime les artefacts de run (garde le suivi git propre)
	rm -rf reports/ logs/
```

`make run` reste mockable : `make run PROVIDER=mock` propage `--provider mock` jusqu'à
`studio run`. Le `Makefile` n'introduit aucune logique — il ne fait qu'exposer la CLI.

---

## 6. Gestion d'erreurs & codes de sortie

- **Erreurs de config/setup → fail loud AVANT tout traitement → exit 1** : `rules.yaml`/
  `policy.yaml` manquants ou malformés (les loaders lèvent déjà), dump qui échoue à
  `schema.validate`, CLI `studio` absente (détectée au premier appel).
- **Erreurs étage 2 par courriel → `status: "error"` dans le rapport, le run continue.**
- **Code de sortie** : `0` si chaque courriel est classé (déterministe ou LLM ok) ;
  **non-zéro (2)** si au moins une erreur par courriel est survenue (signal CI). Le rapport est
  écrit dans les deux cas.

---

## 7. Tests → critères d'acceptation

| Critère STU-392 | Preuve |
|---|---|
| Run complet sur dump figé → rapport sans erreur | `test_orchestrate_pipeline` avec `FakeClassifier` sur les fixtures |
| Les tranchés étage 1 ne montent jamais au LLM | `FakeClassifier` enregistre les appels ; assert = seuls les thread_id catch-all |
| Seuls les `escalate_to_llm` passent à l'étage 2 | idem |
| Rapport agrège les deux chemins + ratio déterministe/LLM | `test_report` (maths d'agrégation) |
| Zéro action sur la boîte (lecture seule) | structurel — aucun code de write/label/trash n'existe |
| Run sur dump figé ET sur fetch réel | dump : pytest ; fetch : runbook manuel |
| Run réel sur boîte perso → rapport auditable | runbook manuel (étend le runbook STU-390) |
| `pytest` passe (LLM mocké) | toute la suite utilise `FakeClassifier` |

`test_stage2_client.py` couvre le parsing stdout/JSONL et toute la taxonomie d'erreurs avec
`subprocess.run` mocké (aucun vrai `studio`, aucun réseau).

### Frontière automatisé / manuel

Le fetch réel et le run réel sur la boîte perso exigent OAuth + un vrai LLM → validés par un
**runbook manuel** (extension du runbook STU-390), pas par pytest.

---

## 8. Hors scope (YAGNI)

- Toute mutation de la boîte (`apply_label`, `trash`) — issue ultérieure.
- Anonymisation PII (STU-393).
- Batch de l'étage 2 (un seul run Studio multi-courriels) — casserait le contrat single-email figé.
- Comportement `deterministic_can_finalize: false` — v1 suppose `true`.
```
