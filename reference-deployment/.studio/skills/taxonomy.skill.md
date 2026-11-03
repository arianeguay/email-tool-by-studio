---
name: taxonomy
description: A/B/C/D email classification taxonomy and per-bucket valid actions (Cantabile reference deployment).
---

# Taxonomie de classement (source de vérité)

Quatre buckets, du plus important au plus jetable. Ce skill lit
`rules/taxonomy.yaml` — les lettres et les sous-types sont ceux de ce déploiement de
référence (STU-1459), pas ceux d'un club de volleyball.

## Bucket A — PRIORITAIRE
Une question d'un parent ou d'un élève, un problème de paiement, un incident de sécurité.
Reste visible.
Sous-types (`subtype`, vocabulaire fermé): `human_question` (question humaine),
`payment_issue` (problème de paiement), `safety_incident` (incident de sécurité).
Actions valides: `keep_visible`, `needs_reply`.

## Bucket B — SUIVI
Rappel ou confirmation automatique, utile sur le coup. Archiver + label.
Sous-types (`subtype`, vocabulaire fermé): `lesson_reminder` (rappel de cours),
`recital_confirmation` (confirmation de spectacle).
Actions valides: `label_archive`, `keep_visible`.

## Bucket C — INFOLETTRE
Contenu marketing légitime (école elle-même ou marchand d'instruments). Archiver + label.
Sous-types (`subtype`, vocabulaire fermé): `newsletter` (infolettre),
`instrument_store_promo` (promo marchand d'instruments).
Actions valides: `label_archive`, `mark_for_delete`.

## Bucket D — DÉMARCHAGE
Sollicitation non désirée. Corbeille via `mark_for_delete` (jamais suppression réelle).
Sous-types (`subtype`, vocabulaire fermé): `cold_solicitation` (démarchage froid).
Actions valides: `mark_for_delete`, `label_archive`.

## Garde-fous
- `subtype` est un enum fermé: recopier le token exact ci-dessus, jamais sa glose
  française ni une traduction. Une valeur hors liste, ou d'un autre bucket, échoue la
  validation du contrat.
- En cas de doute, défaut sur A (visible).
- `escalate_to_llm` est permis pour tout bucket (fallback).
- Neutralité provider: features universelles seulement; `provider_category` = indice faible.
