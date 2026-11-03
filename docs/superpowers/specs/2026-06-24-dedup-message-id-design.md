# STU-395 — Déduplication par `message_id` (et non `thread_id`)

**Issue:** [STU-395](https://linear.app/studioag/issue/STU-395)
**Date:** 2026-06-24
**Type:** Correction de contrat + sémantique (pas cosmétique)

## Problème

Le run `9fbf5306-eae4-44e8-8760-c017312eff4d` (50 entrées) révèle que `thread_id`
n'est pas unique par message traité. Plusieurs messages distincts partagent un
`thread_id` et l'orchestrateur les collapse en un seul :

- `19ed1de17eaa17cf` : deux factures vétérinaires distinctes (Chléo, puis Félix).
- `19ec512eb15dd7af` : trois messages distincts d'un même fil (proposition de
  rendez-vous, puis deux confirmations).

Conséquence : `totals.emails` ne correspond pas au nombre de messages uniques, donc
`ratio_deterministic` est calculé sur un dénominateur faux et n'est pas fiable.

### Cause racine

L'identité par message existe déjà dans le fetch : `_list_message_ids` retourne le
`id` Gmail (unique par message) et `messages.get(id=…)` l'utilise. Mais la projection
(`fetch/gmail/projection.py`) **jette `raw["id"]`** et ne conserve que `threadId`.
L'orchestrateur déduplique ensuite délibérément par thread (`_dedupe_by_thread`,
docstring : « collapse N messages to one »), fusionnant des objets distincts.

Le fetch ramène déjà l'information ; elle est simplement perdue à la frontière de
projection. Ce n'est pas un cas limite : c'est un choix de modélisation (thread =
unité) que STU-395 inverse (message = unité).

## Décisions (validées)

1. **Comportement multi-messages : chaque message est classé indépendamment.**
   Chléo et Félix sont deux objets distincts → deux verdicts. Le fil à 3 messages →
   3 verdicts. L'unité de traitement et de déduplication est le message individuel.
2. **`thread_id` est conservé à côté de `message_id`.** `message_id` devient l'unité
   de traitement/dedup ; `thread_id` reste le contexte de regroupement (quels messages
   appartiennent à une même conversation). Plus petit blast radius, aucune info perdue.
3. **`message_id` est un champ requis du schéma.** Identité stable garantie : aucun
   message ne peut traverser le pipeline sans identité. Les 23 fixtures existantes
   reçoivent un `message_id` unique stable. Pas de fallback vers `thread_id` (qui
   réintroduirait exactement l'ambiguïté que STU-395 supprime).

## Changements

| Fichier | Changement |
|---|---|
| `core/types.py` | `EmailInput` gagne `message_id: str` (premier champ). `Classification` (contrat figé) inchangé : l'identité du verdict vient de l'input, pas de l'écho LLM. |
| `core/schema.py` | `message_id` ajouté à `required` + `properties`. |
| `fetch/gmail/projection.py` | `"message_id": raw["id"]` — cesse de jeter l'identité déjà fetchée. |
| `orchestrate/pipeline.py` | `_dedupe_by_thread` → `_dedupe_by_message` (clé sur `message_id`) ; les verdicts portent `message_id`. |
| `orchestrate/report.py` | `EmailVerdict` gagne `message_id` ; `render_human` l'affiche. `totals.emails` = messages uniques ; `ratio_deterministic` recalculé sur le bon dénominateur. |
| `stage1/frontier_log.py` | Entrée de log gagne `message_id` (conserve `thread_id`) — renforce la fondation d'idempotence visée par l'issue. |
| 23 fixtures | Chacune reçoit un `message_id` unique stable. |
| `stage2_client.py` | **Inchangé.** Le verdict tire `message_id` de l'objet email ; l'input Studio n'en a pas besoin (YAGNI). |

## Sémantique de déduplication

`_dedupe_by_message` garde la logique actuelle avec une nouvelle clé : une entrée par
`message_id`, le plus récent par `date` gagne, ordre de première apparition préservé.
Comme `message_id` est unique par message réel, la dedup devient un **filet
d'idempotence** (même message fetché deux fois) plutôt qu'un collapse destructif.

## Fixtures de régression

Deux fixtures encodant le bug, servant de tests de non-régression :

- `19ed1de17eaa17cf` → 2 messages, même thread, `message_id`s distincts (facture
  Chléo, facture Félix) → **2 verdicts**.
- `19ec512eb15dd7af` → 3 messages, même thread, `message_id`s distincts (proposition
  RDV + 2 confirmations) → **3 verdicts**.

## Tests

- `test_schema` : `message_id` requis ; rejette un input qui en manque.
- `test_gmail_projection` : `raw["id"]` → `message_id` ; deux messages de même
  `threadId` mais `id` différents projettent vers des `message_id` distincts.
- `test_orchestrate_pipeline` : fil multi-messages → un verdict par message ; la dedup
  ne collapse que les vrais doublons de `message_id`.
- `test_report` / `test_frontier_log` : `message_id` présent dans les verdicts et les
  entrées de log.

## Critères d'acceptation (issue)

- [x] Le schéma expose un `message_id` unique et stable par message, distinct de `thread_id`.
- [x] Aucun identifiant de message n'apparaît deux fois dans `verdicts` (garanti par la
      clé `message_id` + test de régression).
- [x] `totals.emails` correspond au nombre de messages uniques réellement traités.
- [x] Comportement multi-messages défini (chaque message classé indépendamment) et testé
      via les fixtures `19ec512eb15dd7af` (3) et `19ed1de17eaa17cf` (2).
- [x] `ratio_deterministic` recalculé sur la base corrigée.
