# Coding Conventions

**Analysis Date:** 2026-05-13

## Langue du code

**Toute la documentation interne est en français :**
- Docstrings de modules, classes, fonctions → français
- Commentaires inline → français
- Messages de log et exceptions destinés à l'utilisateur → français
- Noms de classes/fonctions/variables : majoritairement français pour le domaine métier
  (`Reservation`, `Commande`, `personnes`, `heure`, `nom_plat`, `prix_total`,
  `telephone`, `restaurant_phone`) et anglais pour le technique (`get_db`,
  `require_api_key`, `send_to_client`).
- Identifiants HTTP/Telnyx/OpenAI restent en anglais (`stream_url`, `call_control_id`).

**Règle :** un nouveau module doit ouvrir par un docstring `"""..."""` en français
qui décrit son rôle, son emplacement dans l'architecture, et toute subtilité
non évidente (ex: `app/config.py`, `app/services/realtime_service.py`).

## Naming Patterns

**Fichiers :**
- `snake_case.py` partout (`tool_service.py`, `voice_webhook.py`, `sms_service.py`).
- Routers FastAPI nommés par ressource au pluriel : `restaurants.py`, `reservations.py`,
  `commandes.py`, `menu.py`. Exception : `voice_webhook.py` (singulier, non-CRUD).
- Services dans `app/services/` suffixés `_service.py`.

**Modules :**
- Un module = une responsabilité claire (auth, database, config, schemas, models).
- Les fichiers utilitaires transverses vont dans `app/utils/`.

**Fonctions :**
- `snake_case` strict.
- Helpers privés préfixés `_` (`_code`, `_spell`, `_create_reservation`,
  `_build_instructions`, `_safe_send`, `_sign_token`, `_require_login`,
  `_safe_int`). Ces fonctions ne sont jamais réexportées.
- Helpers ultra-courts pour le parsing config : `_s`, `_f`, `_i` (string/float/int)
  dans `app/config.py`. À réserver à ce type de cas concentré.

**Variables :**
- `snake_case` (`caller_phone`, `stream_sid`, `last_item`, `pending_transfer`).
- Variables d'état courte durée dans une boucle : 1-3 lettres OK (`r`, `m`, `it`,
  `cid`, `ev`, `t`).
- Constantes module-level : `UPPER_SNAKE_CASE` (`SYSTEM_PROMPT`, `REALTIME_TOOLS`,
  `COOKIE_NAME`, `SESSION_MAX_AGE`, `TEMPLATES_DIR`). Les constantes privées au
  module gardent le préfixe `_` (`_CODE_CHARS`).

**Classes :**
- `PascalCase` (`Restaurant`, `Reservation`, `Commande`, `MenuItem`, `Settings`, `Base`).
- Schémas Pydantic suffixés `Create` / `Response` :
  `RestaurantCreate`, `RestaurantResponse`, `MenuItemCreate`, etc.
  (cf. `app/schemas.py`).

**Identifiants de log :**
- Chaque appel téléphonique se voit attribuer un `cid` court (8 caractères de
  `uuid.uuid4()`) préfixé `[cid]` dans tous les logs liés
  (`app/services/realtime_service.py:157`). Pattern à reproduire pour toute
  séquence longue.

## Type Hints

**Règle :** type hints obligatoires sur signatures publiques.

**Style moderne PEP 604 (Python 3.10+) :**
- `str | None` plutôt que `Optional[str]`.
- `list[dict]`, `dict[str, Any]` plutôt que `List`, `Dict` de `typing`.
- `Generator[Session]` importé depuis `collections.abc` (cf. `app/database.py:24`).

**Exemples :**
```python
def to_e164(phone: str | None, default_country: str = "FR") -> str | None: ...
def execute_tool_call(restaurant_id: int, name: str, args: dict, *,
                      menu, quota_reservations, ...) -> dict: ...
```

**Tolérance :** les paramètres internes des helpers privés peuvent omettre
les annotations quand le contexte est évident (`_create_reservation` dans
`app/services/tool_service.py:68`). Ne pas étendre cette tolérance aux
fonctions exposées.

## Code Style

**Formatting :**
- Aucun formateur configuré (pas de `pyproject.toml`, `.flake8`, `ruff.toml`,
  `.pre-commit-config.yaml`).
- Convention de fait : 4 espaces, lignes ≤ ~110 caractères, espaces autour des
  opérateurs.
- Plusieurs statements sur une ligne tolérés pour des paires triviales :
  `db.add(r); db.commit()` (`app/services/tool_service.py:87`).
- One-liners `try: ... except ...: ...` autorisés pour les helpers de parsing
  défensif (`app/config.py:30-31`).

**Imports :**
- Ordre : stdlib → tiers → `app.*` (un blank line entre groupes).
- `from X import a, b, c` par ordre alphabétique au sein de l'import.
- Pas de `import *`.
- Import optionnel via `try/except ImportError` toléré pour
  les dépendances vraiment optionnelles (cf. `dotenv` dans `app/config.py:14-18`).

**Path Aliases :**
- Imports absolus depuis la racine du package : `from app.config import settings`,
  `from app.services.tool_service import execute_tool_call`.
- Pas d'imports relatifs (`.foo`, `..bar`).

## Docstrings

**Style :** triple-quoted `"""..."""`, première ligne = résumé court, blank
line, puis paragraphe(s) explicatifs si besoin. **En français.**

**Module :**
```python
"""Configuration MIA — chargée depuis les variables d'environnement (.env).

On utilise une dataclass figée (frozen=True) au lieu de pydantic-settings :
moins de magie, moins de dépendances...
"""
```

**Fonction :**
```python
def _code(prefix: str) -> str:
    """Génère un code court cryptographiquement sûr (ex: R4T2K, C7MN9).

    `secrets` est utilisé plutôt que `random` car ces codes servent
    d'identifiants exposés au client : un code prévisible pourrait
    permettre de deviner les réservations d'autres clients.
    """
```

**Sections Args/Returns** : utilisées seulement quand la signature comporte
beaucoup de paramètres ou des retours composites (cf. `run_realtime_bridge`
dans `app/services/realtime_service.py:144`). Pas de format Google/NumPy strict.

**Règle :** un docstring doit expliquer *pourquoi* (motivation, contraintes,
pièges), pas seulement *quoi*. Voir `_init_session`
(`app/services/realtime_service.py:114`) pour un bon exemple.

## Configuration

**Pattern :** `dataclass(frozen=True)` + helpers `_s` / `_f` / `_i` de parsing
défensif, exposé via un singleton `settings` (cf. `app/config.py`).

**Règle :**
- Toute nouvelle variable d'environnement DOIT passer par `Settings` —
  ne JAMAIS appeler `os.getenv` ailleurs dans le code.
- Documenter dans un commentaire la signification, les valeurs typiques et
  les conséquences d'un mauvais réglage (cf. `vad_threshold`).
- Fournir un défaut sûr pour dev local quand c'est possible.
- Logger un `warning` au startup quand un secret reste à sa valeur par défaut
  ou est manquant (`app/main.py:42`).

## Error Handling

**Principes observés :**

1. **Webhooks et code de production-critique : try/except large + log + retour
   sûr.** Ne JAMAIS faire crasher un appel téléphonique en cours.
   ```python
   try:
       db.add(r); db.commit()
   except Exception as e:
       db.rollback()
       log.exception("Erreur réservation : %s", e)
       return {"success": False, "error": str(e)}
   finally:
       db.close()
   ```
   (`app/services/tool_service.py:88-93`)

2. **API REST : `HTTPException` avec status code + message français.**
   ```python
   raise HTTPException(status_code=404, detail="Restaurant non trouvé")
   ```
   (`app/routers/restaurants.py:28`)

3. **Clients HTTP externes (Telnyx) : retour `bool`, jamais d'exception
   propagée vers l'appelant.** Distinguer `HTTPError` (log warning avec body)
   du reste (log exception). Voir `app/services/telnyx_service.py:43-49`.

4. **Helpers de parsing défensif : retourner un défaut, ne pas raise.**
   ```python
   try: return float(os.getenv(k) or d)
   except ValueError: return d
   ```
   (`app/config.py:30-31`)

5. **WebSocket : double protection** — vérifier l'état (`WebSocketState.CONNECTED`)
   ET try/except, car la socket peut tomber entre les deux
   (`app/services/realtime_service.py:98-111`).

**Patterns d'erreurs OpenAI bénignes :** filtrer explicitement par code
(`item_truncate_invalid_item_id`, `input_audio_buffer_commit_empty`) avant de
fermer la session (`app/services/realtime_service.py:257`). Documenter le
pourquoi.

## Logging

**Framework :** `logging` stdlib configuré au point d'entrée
(`app/main.py:21`) :
```python
logging.basicConfig(format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                    level=logging.INFO)
```

**Loggers nommés par module :**
- `mia` — log principal de l'app (`app/main.py:22`)
- `mia.tools` — exécution des function calls (`tool_service.py:23`)
- `mia.realtime` — bridge WebSocket (`realtime_service.py:38`)
- `mia.conv` — contenu de la conversation (transcripts client/MIA, tool calls)
  (`realtime_service.py:39`)
- `mia.telnyx` — appels API Telnyx (`telnyx_service.py:18`)

**Règles :**
- Niveau `INFO` pour les évènements métier réussis (`Réservation créée : %s`).
- Niveau `WARNING` pour les défauts de configuration ou échecs récupérables
  (clé API manquante, transfert refusé par Telnyx).
- `log.exception()` (et pas `log.error`) quand on capture une exception —
  conserve la stack trace.
- Préfixer les logs d'un appel par `[cid]` pour pouvoir corréler.
- Format paramétré (`log.info("X : %s", v)`), pas de f-string dans les appels
  log (évite l'évaluation si le niveau est désactivé).
- Logs `mia.conv` peuvent inclure des emojis (`👤`, `🤖`, `🔧`, `✓`) pour
  scanner visuellement le déroulé. À garder limité à ce logger.

## Sécurité

**Patterns à reproduire pour toute auth/secret :**

1. **Comparaisons timing-safe :** `hmac.compare_digest` partout
   (`app/auth.py:26`, `app/routers/dashboard.py:57, 95`).
2. **Codes exposés au client : `secrets`, pas `random`** (`tool_service.py:14, 37`).
3. **Alphabet sans ambiguïté orale** pour les codes lus au téléphone
   (`_CODE_CHARS` exclut `0/O`, `1/I/L`, `5/S`, `8/B`).
4. **Cookies de session :** `httponly=True`, `samesite="lax"`, `path` restreint
   (`app/routers/dashboard.py:101-105`).
5. **Webhooks Telnyx hors auth API** — ne pas appliquer `require_api_key`
   à `/voice/*` (cf. note `app/auth.py:6-7`).

## Function Design

**Taille :** la majorité des fonctions tiennent en 5-30 lignes. Les fonctions
plus longues (`run_realtime_bridge` ~225 lignes) sont structurées en sous-fonctions
internes (`receive_from_client`, `send_to_client`, `send_greeting`) pour
partager l'état via closures.

**Paramètres :**
- Forcer le keyword-only avec `*` pour les fonctions à beaucoup d'arguments :
  ```python
  def run_realtime_bridge(client_ws, restaurant, *, menu=None,
                          caller_phone="", call_control_id="", ...)
  ```
  (`app/services/realtime_service.py:144`).
- Tolérer les défauts vides (`""`, `None`, `[]`) pour permettre des appels
  partiels lors du développement.

**Return Values :**
- Tools retournent un `dict` standardisé `{success, recap_vocal, code?, error?,
  transfer?}` (`app/services/tool_service.py`). Toute nouvelle tool doit suivre
  ce shape.
- Clients HTTP externes retournent `bool` (succès/échec), pas de raise.

## Module Design

**Exports :** pas de `__all__`. Tout symbole non-`_préfixé` est public.
Les helpers sont préfixés `_` pour signaler "ne pas importer hors de ce module".

**Barrel Files :** `__init__.py` vides dans `app/`, `app/routers/`,
`app/services/`, `app/utils/`. Pas de re-export depuis les `__init__`.

## SQLAlchemy

**Modèles :**
- Hériter de `app.database.Base` (DeclarativeBase 2.0).
- Toujours `created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))`.
- `relationship(..., back_populates=..., cascade="all, delete-orphan")` côté parent.
- `ForeignKey("...", ondelete="CASCADE")` côté enfant.
- Index sur les colonnes filtrées fréquemment (`code`, `incoming_phone_number`).

**Sessions :**
- API REST / dashboard : utiliser la dépendance FastAPI `Depends(get_db)`
  (`app/database.py:24`) — la session est fermée automatiquement.
- Code hors-requête (tools, services async) : `SessionLocal()` manuel avec
  `try/finally db.close()`, et `db.rollback()` dans le `except`
  (cf. `app/services/tool_service.py:75-93`).

## Async

**Règles observées :**
- Tout l'I/O réseau passe en `async` (FastAPI, websockets).
- Code synchrone bloquant (SQLAlchemy, `urllib.request` Telnyx) appelé depuis
  une route async DOIT passer par `asyncio.to_thread` pour ne pas bloquer la
  boucle qui sert l'audio en parallèle. Exemple :
  ```python
  result = await asyncio.to_thread(execute_tool_call, ...)
  ```
  (`app/services/realtime_service.py:329`).
- Coordination entre tâches concurrentes : `asyncio.Event` + `asyncio.gather`,
  variables d'état partagées via closures + `nonlocal`.

## Anti-patterns à éviter

- Lire `os.getenv` ailleurs que dans `app/config.py`.
- Logger en f-string : `log.info(f"x={x}")` (préférer `log.info("x=%s", x)`).
- Bloquer la boucle asyncio avec du SQL/HTTP synchrone direct dans une route
  async (utiliser `asyncio.to_thread`).
- Comparer des secrets avec `==` (utiliser `hmac.compare_digest`).
- Laisser une exception non capturée traverser le bridge realtime — un appel
  en cours doit être robuste à tout sauf déconnexion explicite.
- Réintroduire `Optional[X]` / `List[X]` — utiliser `X | None` / `list[X]`.

---

*Convention analysis: 2026-05-13*
