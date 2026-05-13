# External Integrations

**Analysis Date:** 2026-05-13

## APIs & External Services

**Voix / IA temps réel :**
- OpenAI Realtime API - Conversation audio bidirectionnelle (entrée + sortie µ-law 8 kHz, function calling)
  - SDK/Client : aucun (le SDK `openai==1.12.0` est listé mais inutilisé) — connexion directe via `websockets` (`app/services/realtime_service.py:169-173`)
  - Endpoint : `wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview`
  - Modèle : `gpt-4o-realtime-preview` (le README mentionne `gpt-4o-mini-realtime` mais le code utilise bien `gpt-4o-realtime-preview`)
  - Auth : header `Authorization: Bearer ${OPENAI_API_KEY}`
  - TLS : `ssl.create_default_context(cafile=certifi.where())`
  - Config session : `audio.input.format=audio/pcmu`, `audio.output.format=audio/pcmu`, voix configurable (défaut `coral`), `turn_detection.type=server_vad`
  - Tools (function calling) exposés : `create_reservation`, `create_commande`, `transfer_to_human` (`app/services/realtime_service.py:50-65`)

**Téléphonie :**
- Telnyx Call Control v2 - Décrochage et transfert d'appels
  - SDK/Client : aucun, appels REST via `urllib.request` (`app/services/telnyx_service.py`)
  - `POST https://api.telnyx.com/v2/calls/{call_control_id}/actions/answer` — décroche et ouvre le stream bidirectionnel (codec PCMU, mode RTP)
  - `POST https://api.telnyx.com/v2/calls/{call_control_id}/actions/transfer` — transfère vers un autre numéro
  - Auth : header `Authorization: Bearer ${TELNYX_API_KEY}`
  - Timeout : 10 s par requête

**SMS :**
- Telnyx Messaging v2 - Envoi de SMS de confirmation (restaurateur + client)
  - SDK/Client : aucun, appel REST via `urllib.request` (`app/services/sms_service.py:42-48`)
  - `POST https://api.telnyx.com/v2/messages`
  - Auth : header `Authorization: Bearer ${TELNYX_API_KEY}`
  - Numéros normalisés en E.164 via `app/utils/phone.py:to_e164` (logique FR : 0X… → +33X…)
  - Échecs silencieux : un SMS raté ne fait jamais échouer la réservation/commande
  - PII : numéros masqués dans les logs (`_mask` dans `sms_service.py:18-20`, format `+339…***XX`)

## Data Storage

**Databases:**
- PostgreSQL (version non figée, >= 9.4 requis pour JSONB)
  - Connexion : `DATABASE_URL` (DSN SQLAlchemy)
  - Client : SQLAlchemy 2.0 (`DeclarativeBase`) + psycopg2-binary
  - Pool : `pool_pre_ping=True`, `pool_recycle=300` (`app/database.py:16`)
  - Schéma versionné dans `scripts/init_db.sql` (4 tables : `restaurants`, `reservations`, `commandes`, `menu`)
  - Pas de migration tool (pas d'Alembic) — schéma appliqué manuellement via `psql -f scripts/init_db.sql`
  - Types : `SERIAL`, `VARCHAR`, `TEXT`, `INTEGER`, `FLOAT`, `TIMESTAMPTZ`, `JSONB` (pour `commandes.items`)

**File Storage:**
- Aucun stockage objet (S3, GCS, etc.)
- Système de fichiers local utilisé uniquement pour lire le prompt système : `prompts/mia_system_prompt.md` (chargé une fois au démarrage dans `realtime_service.py:42`) et les templates Jinja2 (`dashboard/templates/`)

**Caching:**
- Aucun cache externe (Redis, Memcached, etc.)
- Le prompt système est gardé en mémoire process via lecture unique au chargement de `realtime_service.py`
- L'objet `settings` (dataclass `frozen=True`) est lu une fois au chargement du module `app/config.py`

## Authentication & Identity

**Auth Provider:**
- Aucun (pas d'OAuth, OIDC, Auth0, etc.) — solution interne minimaliste
- **API REST** (`/restaurants`, `/menu`, `/reservations`, `/commandes`) : clé statique `MIA_API_KEY` en header `X-API-Key`
  - Implémentation : `app/auth.py:require_api_key` (dépendance FastAPI)
  - Comparaison timing-safe via `hmac.compare_digest`
  - Si `MIA_API_KEY` non définie en env → garde désactivé (un warning est loggé au startup, cf. `app/main.py:52`)
- **Dashboard admin** (`/dashboard/*`) : mot de passe unique + cookie de session signé HMAC
  - Implémentation : `app/routers/dashboard.py:42-78`
  - Token = `f"{timestamp}:{hmac_sha256(dashboard_secret, f'mia:{ts}')[:32]}"`
  - Cookie : `mia_session`, `httponly=True`, `samesite=lax`, `path=/dashboard`, durée 7 jours
  - Comparaison timing-safe sur le mot de passe et le token
- **Webhooks Telnyx** (`/voice/incoming`, WS `/voice/media-stream`) : aucune authentification — exposés publiquement (aucune vérification de signature Telnyx)

## Monitoring & Observability

**Error Tracking:**
- Aucun (pas de Sentry, Rollbar, Datadog, etc.)

**Logs:**
- `logging.basicConfig` standard Python (`app/main.py:21`), niveau `INFO`
- Loggers nommés : `mia` (app), `mia.realtime`, `mia.conv` (conversations utilisateur/MIA), `mia.telnyx`, `mia.sms`, `mia.tools`, `mia.voice`
- Corrélation des appels via un ID court généré par appel : `cid = str(uuid.uuid4())[:8]` (`realtime_service.py:157`)
- Conversations loggées en clair via `mia.conv` (préfixes 👤 / 🤖 / 🔧) — attention RGPD
- Aucune rotation, ni envoi vers un agrégateur externe

## CI/CD & Deployment

**Hosting:**
- VPS Hetzner (par le contexte) ; aucune config d'infra-as-code dans le repo
- Reverse proxy Nginx + TLS Let's Encrypt en frontal (la terminaison TLS est externe au process Python)
- Health check : `GET /` retourne `{"status": "ok", "app": "MIA"}` (`app/main.py:60-62`) et `GET /voice/incoming` retourne `{"status": "ok"}` (`voice_webhook.py:48-51`)

**CI Pipeline:**
- Aucun (pas de `.github/workflows/`, `.gitlab-ci.yml`, `circleci/`, etc.)

## Environment Configuration

**Required env vars (production) :**
- `DATABASE_URL`, `OPENAI_API_KEY`, `TELNYX_API_KEY`, `TELNYX_PHONE_NUMBER`, `MIA_API_KEY`, `DASHBOARD_PASSWORD`, `DASHBOARD_SECRET`, `BACKEND_URL`
- Optionnels : `VOICE_REALTIME_DOMAIN`, `VOICE_REALTIME_VOICE`, `VAD_THRESHOLD`, `VAD_PREFIX_PADDING_MS`, `VAD_SILENCE_DURATION_MS`

**Secrets location:**
- Fichier `.env` à la racine (présence notée, contenu non lu — listé dans `.gitignore`)
- `.env.example` versionné comme template (sans valeurs réelles)
- Pas de vault externe (Vault, AWS Secrets Manager, etc.) — secrets injectés directement comme variables d'environnement en production
- Warnings au démarrage si les défauts sont conservés (`app/main.py:_check_config`)

## Webhooks & Callbacks

**Incoming (vers MIA) :**
- `GET /voice/incoming` — health check appelable depuis Telnyx (`app/routers/voice_webhook.py:48-51`)
- `POST /voice/incoming` — webhook principal Telnyx, traite `event_type == "call.initiated"` (les autres events sont ignorés) (`app/routers/voice_webhook.py:54-99`)
- `WS /voice/media-stream` — WebSocket bidirectionnel ouvert par Telnyx pour streamer l'audio PCMU (`app/routers/voice_webhook.py:102-168`)
  - Un `client_state` base64(JSON) encode `restaurant_id`, `caller_phone`, `call_control_id` et est transmis par Telnyx dans le message `start` du WS
  - Le `stream_url` envoyé à Telnyx est construit depuis `settings.stream_wss_domain` → `wss://<domaine>/voice/media-stream`

**Outgoing (depuis MIA) :**
- Vers Telnyx Call Control : `POST .../calls/{id}/actions/answer` et `.../actions/transfer`
- Vers Telnyx Messaging : `POST /v2/messages` (SMS au restaurateur et au client)
- Vers OpenAI : connexion WebSocket persistante pour la durée de l'appel (`wss://api.openai.com/v1/realtime`)

**Routage multi-restaurant :**
- Le restaurant cible est identifié par `incoming_phone_number` (numéro Telnyx appelé, UNIQUE en DB)
- Fallback mono-restaurant : s'il n'y a qu'un seul restaurant configuré, il est utilisé même si le numéro ne matche pas exactement (`voice_webhook.py:32-45`)

---

*Integration audit: 2026-05-13*
