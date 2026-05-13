# Technology Stack

**Analysis Date:** 2026-05-13

## Languages

**Primary:**
- Python 3.11+ - Backend FastAPI complet (`app/`, `scripts/`)
- SQL (PostgreSQL dialect) - Schéma DB dans `scripts/init_db.sql`

**Secondary:**
- HTML / Jinja2 - Templates du dashboard admin (`dashboard/templates/*.html`)
- Markdown - Prompt système OpenAI (`prompts/mia_system_prompt.md`)

## Runtime

**Environment:**
- Python 3.11+ (le commentaire en tête de `requirements.txt` impose `Python 3.11+`)
- Serveur ASGI : `uvicorn[standard] 0.27.0` lancé via `uvicorn app.main:app`
- En production : VPS Hetzner, derrière Nginx + Let's Encrypt (terminaison TLS)
- PostgreSQL (version non figée — utilise JSONB et `TIMESTAMPTZ`, donc >= 9.4)

**Package Manager:**
- pip + `requirements.txt` (versions épinglées)
- Lockfile : aucun lockfile dédié (pas de `Pipfile.lock`, `poetry.lock` ou `uv.lock`)
- Pas d'outil de gestion d'environnement Python explicite (pas de `pyproject.toml`, `setup.py`, `.python-version`)

## Frameworks

**Core:**
- FastAPI 0.109.0 - API HTTP, WebSockets, dépendances FastAPI (`Depends`, `Form`, `Request`)
- Starlette (transitive via FastAPI) - WebSockets bas-niveau (`starlette.websockets.WebSocketState`)
- SQLAlchemy 2.0.25 - ORM (style 2.0 `DeclarativeBase`), session sync via `sessionmaker`
- Jinja2 3.1.3 - Templates HTML du dashboard (`fastapi.templating.Jinja2Templates`)

**Testing:**
- Aucun framework de test installé (pas de pytest dans `requirements.txt`, pas de `tests/`, pas de `conftest.py`). `.gitignore` mentionne `.pytest_cache/` mais aucun test n'est présent dans le repo.

**Build/Dev:**
- uvicorn[standard] 0.27.0 - Inclut `watchfiles` pour `--reload`
- Aucun linter / formatter configuré (pas de `.ruff.toml`, `.flake8`, `pyproject.toml`, `mypy.ini`, `.pre-commit-config.yaml`)

## Key Dependencies

**Critical:**
- openai 1.12.0 - Officiellement présent dans `requirements.txt`, mais le code n'utilise pas le SDK : la connexion à OpenAI Realtime se fait via `websockets` direct (`app/services/realtime_service.py:169`)
- websockets >= 12.0 - Client WebSocket vers `wss://api.openai.com/v1/realtime` (modèle `gpt-4o-realtime-preview`)
- certifi >= 2024.0.0 - Bundle CA Mozilla utilisé pour le contexte SSL du WebSocket OpenAI (`realtime_service.py:166`)
- sqlalchemy 2.0.25 + psycopg2-binary 2.9.9 - Accès PostgreSQL synchrone via `app/database.py`
- pydantic 2.5.3 - Schémas Pydantic dans `app/schemas.py` (validation des payloads API)

**Infrastructure:**
- python-dotenv 1.0.0 - Chargement optionnel du `.env` (`app/config.py:14-18`, import protégé par try/except)
- python-multipart 0.0.9 - Support `Form(...)` dans le dashboard (`app/routers/dashboard.py`)
- jinja2 3.1.3 - Rendu des templates `dashboard/templates/*.html`

**Notable absences:**
- Aucun client HTTP de haut niveau (`httpx`, `requests`) : tous les appels Telnyx (Call Control + Messaging) passent par `urllib.request` standard (`app/services/telnyx_service.py`, `app/services/sms_service.py`)
- Aucun SDK Telnyx officiel : implémentation REST manuelle
- Aucun outil de migration DB (pas d'Alembic) — le schéma se gère via `scripts/init_db.sql`

## Configuration

**Environment:**
- Toutes les configs sont lues depuis des variables d'environnement via `os.getenv` dans `app/config.py`
- Singleton `settings: Settings` (dataclass `frozen=True`) lu **une seule fois** au chargement du module — tout changement nécessite un redémarrage du process
- `.env` chargé optionnellement par `python-dotenv` au démarrage si présent
- Helpers de parsing typé : `_s` (string strip), `_f` (float), `_i` (int) — défaut typé si var absente

**Variables requises (cf. `.env.example`) :**
- `DATABASE_URL` — DSN PostgreSQL (`postgresql://user:pass@host:5432/mia`)
- `OPENAI_API_KEY` — clé API OpenAI (header `Authorization: Bearer ...`)
- `TELNYX_API_KEY` — clé API Telnyx (Call Control + Messaging)
- `TELNYX_PHONE_NUMBER` — numéro Telnyx expéditeur SMS (E.164)
- `MIA_API_KEY` — clé partagée pour protéger l'API REST (header `X-API-Key`) ; si vide, API ouverte
- `DASHBOARD_PASSWORD` — mot de passe d'accès au dashboard (défaut `mia-admin`)
- `DASHBOARD_SECRET` — secret HMAC-SHA256 pour signer le cookie de session (défaut `change-me-in-production`)
- `BACKEND_URL` — URL publique du backend (pour construire l'URL WSS du stream Telnyx)
- `VOICE_REALTIME_DOMAIN` (optionnel) — override du domaine pour l'URL WSS (utile derrière reverse proxy/tunnel)
- `VOICE_REALTIME_VOICE` (optionnel, défaut `coral`) — voix OpenAI Realtime
- `VAD_THRESHOLD` / `VAD_PREFIX_PADDING_MS` / `VAD_SILENCE_DURATION_MS` — paramètres VAD serveur OpenAI

**Build:**
- Aucun pipeline de build : c'est du Python pur, déployé tel quel
- Pas de `Dockerfile`, `docker-compose.yml`, ou `Procfile` dans le repo
- Pas de configuration CI/CD versionnée (pas de `.github/workflows/`, `.gitlab-ci.yml`)

## Platform Requirements

**Development:**
- Python 3.11+ avec pip
- Instance PostgreSQL accessible (locale ou distante)
- Comptes OpenAI (API Realtime) et Telnyx (numéro + clé API)
- Un tunnel public (ngrok ou équivalent) si on veut tester les webhooks Telnyx en local
- Lancement : `uvicorn app.main:app --reload`

**Production:**
- VPS Hetzner (selon le contexte projet) avec Nginx en reverse proxy + Let's Encrypt pour TLS
- Le WebSocket `/voice/media-stream` doit être joignable en `wss://` depuis Telnyx (Nginx doit relayer `Upgrade: websocket`)
- PostgreSQL : `pool_pre_ping=True` et `pool_recycle=300` (5 min) configurés dans `app/database.py:16` pour survivre aux connexions coupées
- Logging configuré en `INFO` via `logging.basicConfig` dans `app/main.py:21` — pas de rotation, ni d'agrégateur externe

---

*Stack analysis: 2026-05-13*
