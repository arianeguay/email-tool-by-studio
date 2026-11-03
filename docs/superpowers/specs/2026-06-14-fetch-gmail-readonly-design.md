# Fetch Gmail (lecture seule) — Design spec
*STU-391 — 2026-06-14*

## Contexte

Étage d'alimentation (Option A) du projet email-classifier. Un script Python
interroge Gmail en **lecture seule** et produit le JSON d'input consommé par
l'étage 1 (STU-389). Le fetch vit **hors** du pipeline Studio : le kernel reste
domain-agnostic, l'app possède l'orchestration et l'état. Conséquence clé : tout
le système reste testable sur JSON figé, sans taper l'API à chaque run.

Cette issue révise et remplace les anciennes STU-320 (OAuth) et STU-321 (tool
plugin TS), repensées ici comme un script Python dans l'app.

Format de sortie du fetch = format d'input de l'étage 1 : `EmailInput`
(`thread_id`, `from_address`, `subject`, `headers`, `body_snippet`, `date`,
`provider_category`), validé par le schéma de `core/schema.py`. Contrainte forte
du schéma : `additionalProperties: False` — le dump doit **projeter** la réponse
riche de l'API Gmail vers exactement ces champs.

### Contrat neutre (provider-agnostic)

Le schéma de `core/schema.py` est le contrat **neutre** entre n'importe quel
fetch et tout l'aval. Aucun champ ne nomme ni ne présuppose un provider. Premier
déploiement sur deux providers : boîte perso (Gmail) et club sportif (Outlook /
Microsoft Graph). Un futur `outlook.py` + `outlook_projection.py` devra produire
**exactement** ce même format ; `core/schema.py` et tout l'aval ne bougent pas.

Trois corrections de neutralité (revue post-implémentation) :

1. **`gmail_category` → `provider_category`** — le nom portait du vocabulaire
   Gmail dans un contrat censé neutre. Renommé en champ neutre nullable.
   *Sémantique* : indice secondaire et bruité (catégorisation native du provider
   — onglet Gmail, Focused/Other Outlook). **Jamais** une feature structurante :
   le recensement de corpus montre que la catégorisation Gmail est à moitié non
   fonctionnelle. L'aval s'appuie sur des features universelles (expéditeur,
   sujet, headers, `List-Unsubscribe`), pas sur ce champ.
2. **Ajout de `date`** (ISO 8601, requis) — champ neutre manquant. La projection
   Gmail le remplit depuis `internalDate` (epoch ms → ISO 8601 UTC).
3. **Dossier par provider** — la projection est Gmail-spécifique malgré un nom
   générique. Restructuré en `fetch/gmail/{client,projection}.py` (et plus tard
   `fetch/outlook/...`) pour que la frontière entre providers soit imposée par le
   système de modules, et que l'ajout d'Outlook soit purement additif. Voir §1.

---

## 1. Architecture & découpage

Le plumbing OAuth/API est lourd en I/O et non testable hors-ligne ; la
**projection** (message Gmail → `EmailInput`) est de la logique pure et porte les
critères d'acceptation (conformité schéma, extraction `List-Unsubscribe`). On
sépare donc les deux pour que la partie intéressante soit testable sans toucher
Google.

Un **dossier par provider** : chaque provider est une unité autonome. L'ajout
d'Outlook est purement additif (`fetch/outlook/`), sans toucher au code Gmail ni
à `core/`. La séparation est imposée par le système de modules, pas juste par des
préfixes de noms.

```
email_classifier/fetch/
├── __init__.py
├── gmail/
│   ├── __init__.py      # façade : ré-exporte l'API publique du provider
│   ├── projection.py    # PUR : dict message Gmail brut → dict EmailInput neutre
│   └── client.py        # EFFETS : client OAuth + boucle list/get API
└── outlook/             # plus tard, purement additif (client.py + projection.py)
scripts/
└── fetch_gmail.py       # CLI : fetch.gmail → validate → write
core/schema.py           # + helper load_many() pour le dump tableau
```

- `gmail/projection.py` n'importe rien de Google → ses tests tournent dans la
  suite existante (légère). Namespacé sous `gmail/`, le nom `projection` est sans
  ambiguïté (pas besoin du préfixe `gmail_`).
- Les librairies `google-*` ne sont importées que dans `gmail/client.py`, et vont
  dans un extra optionnel `[fetch]` du `pyproject.toml` pour garder la suite
  étage 1 sans dépendances lourdes.
- `fetch/gmail/__init__.py` est la **façade** : les consommateurs importent
  `from email_classifier.fetch import gmail` ; `client` et `projection` sont
  internes. Pas de Protocol partagé tant que l'orchestrateur (STU-392) n'en a pas
  besoin (YAGNI : abstraction à un seul implémenteur).

---

## 2. La projection (le cœur)

Chaque message est récupéré avec `format='metadata'` + `metadataHeaders=[...]` de
l'API Gmail. Élégant : l'API ne renvoie **que** les headers nommés → l'allowlist
curée est appliquée à la frontière de l'API (privacy par construction, données
minimales sur le fil).

| Champ EmailInput | Source |
|---|---|
| `thread_id` | `message.threadId` |
| `from_address` | header `From`, parsé en adresse nue via `email.utils.parseaddr` |
| `subject` | header `Subject` (`""` si absent) |
| `headers` | headers de l'allowlist présents sur le message |
| `body_snippet` | `message.snippet`, dé-échappé des entités HTML |
| `date` | `message.internalDate` (epoch ms) → ISO 8601 UTC |
| `provider_category` | dérivé des `labelIds` (voir plus bas) |

**Allowlist de headers** (constante de module, extensible) :
`List-Unsubscribe`, `List-Unsubscribe-Post`, `Precedence`, `Auto-Submitted`,
`X-Mailer`. (`From`/`Subject` sont aussi demandés à l'API mais consommés dans
leurs propres champs, pas dupliqués dans `headers`.)

**Mapping catégorie** depuis `labelIds` : `CATEGORY_PERSONAL→"primary"`,
`CATEGORY_PROMOTIONS→"promotions"`, `CATEGORY_UPDATES→"updates"`,
`CATEGORY_SOCIAL→"social"`, `CATEGORY_FORUMS→"forums"` ; `null` si aucune.

---

## 3. OAuth (lecture seule, structurelle)

Scope = `https://www.googleapis.com/auth/gmail.readonly`, et **uniquement** lui.
Les seuls appels API du codebase sont `messages().list()` / `messages().get()`.
La lecture seule est structurelle, pas seulement promise.

`credentials.json` / `token.json` par défaut dans `secrets/` (gitignoré),
surchargeables via `GMAIL_CREDENTIALS_FILE` / `GMAIL_TOKEN_FILE`. Le premier run
fait le flux InstalledApp (consentement navigateur) et cache le refresh token ;
les runs suivants rafraîchissent silencieusement.

---

## 4. CLI (`scripts/fetch_gmail.py`)

```
--query "..."        # recherche Gmail brute (label:, after:, before: natifs)
--max-results N      # plafond (défaut 100)
--output PATH        # défaut : dumps/gmail-dump.json
```

On s'appuie sur la syntaxe `q` native de Gmail pour fenêtre temporelle/label
plutôt que d'inventer des flags parallèles : `--query "after:2026/06/01
label:promotions"` couvre déjà « query, fenêtre, label » de l'issue.

Sortie : **un seul fichier JSON** contenant un tableau d'objets `EmailInput`. Un
dump = un snapshot de boîte à un instant T = un artefact atomique (unité propre
pour l'orchestrateur STU-392 et l'anonymiseur STU-394). Le script valide chaque
objet projeté contre le schéma **avant** d'écrire, et échoue bruyamment si un
élément n'est pas conforme.

Pourquoi un tableau et pas un fichier par email (comme les fixtures) : les
fixtures sont des cas de test curés et **classés** (rangés par bucket) ; un dump
brut est l'inverse — un snapshot plat **non classé** (les buckets sont assignés
*par* la classification, en aval). Matcher le layout des fixtures serait une
fausse économie.

---

## 5. Tests & gitignore

- `tests/test_projection.py` — dicts Gmail bruts synthétiques → l'output projeté
  passe `schema.validate` ; `List-Unsubscribe` survit ; mapping catégorie ;
  parsing `From` (`"Name <a@b>"` → `a@b`) ; headers absents → `{}` ; pas de
  catégorie → `null`.
- `.gitignore` gagne `secrets/` et `dumps/`.
- Pas de test d'API live (I/O) ; `gmail.py` reste assez mince pour relecture.

---

## Critères d'acceptation (repris de STU-391)

- OAuth2 lecture seule fonctionnel, token refresh géré
- Le script produit un JSON conforme au schéma d'input de STU-389 (validé)
- Le header `List-Unsubscribe` est correctement extrait
- Le script accepte des filtres (query, fenêtre temporelle, max résultats)
- Un dump réel de la boîte perso passe le schéma d'input sans erreur
- Zéro écriture sur la boîte (lecture seule vérifiée — scope + appels)
- Les credentials ne sont jamais committés
