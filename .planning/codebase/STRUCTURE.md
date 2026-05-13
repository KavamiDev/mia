# Codebase Structure

**Analysis Date:** 2026-05-13

## Directory Layout

```
mia/
├── app/                              # Code applicatif Python (package FastAPI)
│   ├── __init__.py
│   ├── main.py                       # Point d'entrée FastAPI, monte routers + CORS
│   ├── config.py                     # Settings dataclass figée chargée depuis .env
│   ├── database.py                   # Engine SQLAlchemy + SessionLocal + get_db()
│   ├── models.py                     # ORM : Restaurant, Reservation, Commande, MenuItem
│   ├── schemas.py                    # Pydantic : *Create / *Response pour l'API REST
│   ├── auth.py                       # Garde X-API-Key (timing-safe)
│   ├── routers/                      # Endpoints HTTP / WebSocket
│   │   ├── __init__.py
│   │   ├── voice_webhook.py          # POST /voice/incoming + WS /voice/media-stream
│   │   ├── dashboard.py              # /dashboard/* (login HMAC + pages Jinja2)
│   │   ├── restaurants.py            # CRUD /restaurants
│   │   ├── reservations.py           # Lecture /reservations
│   │   ├── commandes.py              # Lecture /commandes
│   │   └── menu.py                   # /menu/restaurant/{id} + bulk
│   ├── services/                     # Logique métier + intégrations externes
│   │   ├── __init__.py
│   │   ├── realtime_service.py       # Bridge WS Telnyx <-> OpenAI Realtime (asyncio)
│   │   ├── tool_service.py           # create_reservation / create_commande / transfer
│   │   ├── telnyx_service.py         # Telnyx Call Control : answer, transfer
│   │   └── sms_service.py            # Telnyx Messaging API
│   └── utils/
│       ├── __init__.py
│       └── phone.py                  # normalize_phone, to_e164, matches_phone
├── dashboard/
│   └── templates/                    # Templates Jinja2 admin
│       ├── base.html
│       ├── login.html
│       ├── restaurants.html
│       ├── restaurant_form.html
│       ├── menu.html
│       ├── reservations.html
│       └── commandes.html
├── prompts/
│   └── mia_system_prompt.md          # Prompt système (personnalité + règles MIA)
├── scripts/
│   └── init_db.sql                   # Création des tables PostgreSQL + index
├── .planning/                        # Documentation GSD (codebase maps, plans)
│   └── codebase/                     # ARCHITECTURE.md, STRUCTURE.md, ...
├── .env.example                      # Modèle de configuration (toutes les vars attendues)
├── .gitignore
├── README.md                         # Quickstart : install, lancement, API
└── requirements.txt                  # Dépendances Python pinned
```

## Directory Purposes

**`app/`:**
- Purpose : Package applicatif Python, importé via `from app.x import y`. Tout le code serveur vit ici.
- Contains : Modules racine (`main`, `config`, `database`, `models`, `schemas`, `auth`) + sous-packages `routers/`, `services/`, `utils/`.
- Key files : `app/main.py` (ASGI app), `app/config.py` (singleton `settings`).

**`app/routers/`:**
- Purpose : Surface HTTP et WebSocket. Un fichier par domaine fonctionnel.
- Contains : Un `APIRouter` par module avec son préfixe et ses dépendances d'auth.
- Key files : `voice_webhook.py` (le plus dense — webhook + WS bridge), `dashboard.py` (templates Jinja2 + auth cookie).

**`app/services/`:**
- Purpose : Logique métier hors HTTP — réutilisable depuis n'importe quel router.
- Contains : Bridge audio temps réel, exécution des function calls OpenAI, clients HTTP Telnyx (voix + SMS).
- Key files : `realtime_service.py` (le coeur — bridge bidirectionnel), `tool_service.py` (création résa/commande).

**`app/utils/`:**
- Purpose : Helpers purs sans dépendance applicative.
- Contains : `phone.py` (normalisation E.164).

**`dashboard/templates/`:**
- Purpose : Vues Jinja2 du dashboard admin (séparées de `app/` car non-Python).
- Contains : `base.html` (layout commun) + une page par section.
- Pointed à par `dashboard.py:31` via `Path(__file__).parent.parent.parent / "dashboard" / "templates"`.

**`prompts/`:**
- Purpose : Prompts système versionnés (FR).
- Contains : `mia_system_prompt.md` chargé une fois au démarrage par `realtime_service.py:42`.

**`scripts/`:**
- Purpose : Scripts opérationnels (init DB, migrations manuelles).
- Contains : `init_db.sql` — appliqué via `psql -d mia -f scripts/init_db.sql`.

**`.planning/codebase/`:**
- Purpose : Documents de cartographie GSD consommés par les commandes `/gsd-plan-phase` et `/gsd-execute-phase`.
- Generated : Oui (par cet agent).
- Committed : À discrétion du projet.

## Key File Locations

**Entry Points:**
- `app/main.py:24` — instance FastAPI `app`, lancée par `uvicorn app.main:app`.
- `app/routers/voice_webhook.py:54` — webhook Telnyx `POST /voice/incoming`.
- `app/routers/voice_webhook.py:102` — WebSocket `/voice/media-stream`.
- `app/routers/dashboard.py:85` — entrée dashboard `GET /dashboard/login`.

**Configuration:**
- `app/config.py` — chargement `.env` + `Settings` dataclass.
- `.env.example` — variables attendues (à copier en `.env`).
- `requirements.txt` — versions pinned (FastAPI 0.109, SQLAlchemy 2.0.25, openai 1.12.0, websockets ≥12).

**Core Logic:**
- `app/services/realtime_service.py` — bridge audio asyncio (3 tâches concurrentes).
- `app/services/tool_service.py` — implémentation des function calls OpenAI.
- `app/services/telnyx_service.py` — answer / transfer Telnyx via `urllib.request`.
- `app/services/sms_service.py` — envoi SMS Telnyx.
- `app/models.py` — schéma ORM.
- `prompts/mia_system_prompt.md` — prompt système MIA.

**Persistance:**
- `app/database.py` — engine + `SessionLocal`.
- `scripts/init_db.sql` — DDL PostgreSQL.

**Auth :**
- `app/auth.py` — garde `require_api_key` pour l'API REST.
- `app/routers/dashboard.py:42-65` — `_sign_token`, `_verify_token`, `_require_login` (cookie HMAC).

**Testing:**
- Not detected — aucun dossier `tests/` ni fichier `test_*.py` dans le repo. Pas de pytest dans `requirements.txt`.

## Naming Conventions

**Files (Python):**
- `snake_case.py` pour tous les modules (`voice_webhook.py`, `tool_service.py`, `init_db.sql`).
- Suffixe `_service.py` pour les modules de la couche services (`realtime_service`, `tool_service`, `telnyx_service`, `sms_service`).
- Pas de suffixe `_router` — les routers vivent dans le sous-package `routers/`.

**Files (templates):**
- `snake_case.html` (`restaurant_form.html`, `login.html`).
- Une page = un fichier ; `base.html` pour le layout partagé.

**Directories:**
- `snake_case` au pluriel pour les "couches" (`routers/`, `services/`, `utils/`, `templates/`, `scripts/`, `prompts/`).
- `app/` au singulier pour le package racine.

**Modules Python:**
- Classes : `PascalCase` (`Restaurant`, `MenuItem`, `Settings`, `Base`).
- Fonctions / variables : `snake_case` (`get_db`, `execute_tool_call`, `caller_phone`).
- Helpers privés : préfixe `_` (`_find_restaurant`, `_build_instructions`, `_safe_send`, `_code`, `_spell`, `_sign_token`).
- Constantes module-level : `UPPER_SNAKE_CASE` (`REALTIME_TOOLS`, `SYSTEM_PROMPT`, `COOKIE_NAME`, `SESSION_MAX_AGE`, `TEMPLATES_DIR`, `_CODE_CHARS`).

**Loggers :**
- Hiérarchie `mia.<sous-système>` : `mia`, `mia.voice`, `mia.realtime`, `mia.conv`, `mia.tools`, `mia.telnyx`, `mia.sms`.

**Codes métier :**
- Réservations : préfixe `R` + 4 chars (`R4T2K`).
- Commandes : préfixe `C` + 4 chars (`C7MN9`).

## Where to Add New Code

**Nouvelle route REST (CRUD ou lecture) :**
- Implémentation : créer `app/routers/<domaine>.py` avec un `APIRouter(prefix=..., tags=..., dependencies=[Depends(require_api_key)])`.
- Inclusion : ajouter `app.include_router(<domaine>.router)` dans `app/main.py:33-38`.
- Schémas : ajouter les classes Pydantic dans `app/schemas.py`.

**Nouvelle table / nouveau modèle :**
- ORM : ajouter la classe dans `app/models.py` (suivre le pattern `Restaurant`, avec `relationship` + `back_populates`).
- DDL : ajouter le `CREATE TABLE` et les index dans `scripts/init_db.sql`.
- Schémas Pydantic : ajouter dans `app/schemas.py`.

**Nouveau tool OpenAI (function call) :**
- Définition : ajouter une entrée dans `REALTIME_TOOLS` (`app/services/realtime_service.py:50`).
- Implémentation : ajouter une branche dans `execute_tool_call` (`app/services/tool_service.py:46`) + une fonction privée `_<nom>(...)` retournant `{success, recap_vocal, ...}`.
- Documenter le tool dans le prompt système (`prompts/mia_system_prompt.md`) pour guider MIA.

**Nouvelle intégration externe (HTTP) :**
- Créer `app/services/<vendor>_service.py` avec une fonction publique par action.
- Suivre le pattern `telnyx_service` / `sms_service` : `urllib.request`, timeout explicite, retourne `bool`, logue le corps tronqué sur erreur HTTP.

**Nouvelle page dashboard :**
- Route : ajouter une fonction dans `app/routers/dashboard.py` avec `redirect = _require_login(request)` en première ligne.
- Template : ajouter `dashboard/templates/<page>.html` qui étend `base.html`.
- Lien : mettre à jour la navigation dans `dashboard/templates/base.html`.

**Nouvelle config / variable d'env :**
- Ajouter le champ dans la dataclass `Settings` (`app/config.py:40`) avec un défaut typé via `_s` / `_i` / `_f`.
- Documenter dans `.env.example`.
- Si critique au démarrage, ajouter un warning dans `_check_config` (`app/main.py:42`).

**Nouveau helper pur :**
- Si lié au téléphone : `app/utils/phone.py`.
- Sinon : créer `app/utils/<domaine>.py` (le sous-package existe déjà).

**Nouveau script ops :**
- `scripts/` (SQL, shell, Python one-shot).

## Special Directories

**`.planning/`:**
- Purpose : Artefacts GSD (codebase maps, plans de phase).
- Generated : Oui, par les commandes `/gsd-*`.
- Committed : À décider selon convention projet.

**`app/__pycache__/` et descendants:**
- Purpose : Caches bytecode Python.
- Generated : Oui (automatique).
- Committed : Non (à ajouter à `.gitignore` si pas déjà présent).

**`.env`:**
- Purpose : Configuration locale avec secrets.
- Generated : Manuellement à partir de `.env.example`.
- Committed : Non (jamais — contient `OPENAI_API_KEY`, `TELNYX_API_KEY`, `DASHBOARD_SECRET`, `MIA_API_KEY`).

---

*Structure analysis: 2026-05-13*
