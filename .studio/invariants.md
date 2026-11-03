# Invariants — email-classifier (étage 2)

- **INV-08 — validation binaire.** A classification is either valid or not; there is
  no score. `suggested_action` must be coherent with `bucket` per the taxonomy table.
- **Jamais de suppression réelle.** `mark_for_delete` only marks; never assume real
  deletion. Under any doubt between A and another bucket, prefer A (visible).
- **Neutralité provider.** Classify only from universal signals: sender, subject,
  headers, presence of `List-Unsubscribe`. `provider_category` is a weak, noisy hint —
  never a deciding feature. An email with no `provider_category` (e.g. Outlook) must be
  classified with the same quality as a Gmail one.
