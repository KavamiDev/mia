<!-- refreshed: 2026-05-13 -->
# Architecture

**Analysis Date:** 2026-05-13

## System Overview

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                        Acteurs externes                                  │
│   ┌─────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────┐ │
│   │  Appelant   │   │   Telnyx     │   │   OpenAI     │   │ Admin    │ │
│   │ (téléphone) │   │ Voice + SMS  │   │  Realtime    │   │(navigateur)│ │
│   └──────┬──────┘   └──────┬───────┘   └──────┬───────┘   └─────┬────┘ │
└──────────┼─────────────────┼──────────────────┼─────────────────┼──────┘
           │  audio PSTN     │  HTTPS + WSS     │ WSS audio       │ HTTPS
           ▼                 ▼                  ▼                 ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       FastAPI app (`app/main.py`)                        │
│  CORS middleware + 6 routers inclus dans cet ordre :                    │
│  dashboard → restaurants → reservations → commandes → menu → voice      │
├──────────────────┬──────────────────────┬──────────────────────────────┤
│  Voice layer     │  REST API layer      │  Dashboard layer             │
│ `routers/        │ `routers/            │ `routers/dashboard.py`        │
│  voice_webhook`  │  restaurants.py`     │  Jinja2 + cookies HMAC        │
│                  │  `reservations.py`   │  (auth password)              │
│ POST /incoming   │  `commandes.py`      │                              │
│ WS /media-stream │  `menu.py`           │ /dashboard/restaurants ...    │
│ (auth: aucune,   │  (auth: X-API-Key    │ (auth: cookie signé)         │
│  webhook public) │   header)            │                              │
└────────┬─────────┴───────────┬──────────┴─────────────┬────────────────┘
         │                     │                          │
         ▼                     │                          │
┌─────────────────────────┐    │                          │
│  Services layer         │    │                          │
│  `app/services/`        │    │                          │
│  - realtime_service     │    │                          │
│    (bridge WS asyncio)  │    │                          │
│  - tool_service         │◄───┘                          │
│    (function calls)     │                               │
│  - telnyx_service       │                               │
│    (answer / transfer)  │                               │
│  - sms_service          │                               │
└──────┬──────────────┬───┘                               │
       │              │                                    │
       │              ▼                                    ▼
       │   ┌──────────────────────────────────────────────────────┐
       │   │  Modèles & schémas SQLAlchemy / Pydantic              │
       │   │  `app/models.py`  `app/schemas.py`                    │
       │   │  `app/database.py` (engine + SessionLocal)            │
       │   └──────────────────────────┬───────────────────────────┘
       │                              │
       ▼                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  PostgreSQL                                                          │
│  Tables : restaurants, reservations, commandes, menu                 │
│  Schéma : `scripts/init_db.sql`                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| App factory | Instancie FastAPI, monte CORS, inclut tous les routers, log warnings sécurité au startup | `app/main.py` |
| Settings | Charge `.env` via dataclass `Settings` figée, expose un singleton `settings` global | `app/config.py` |
| Database engine | Engine SQLAlchemy + `SessionLocal` + dépendance `get_db()` | `app/database.py` |
| ORM models | Tables `restaurants`, `reservations`, `commandes`, `menu` (JSONB items) | `app/models.py` |
| Pydantic schemas | Validation REST `*Create` / `*Response` avec `from_attributes=True` | `app/schemas.py` |
| API auth | Garde `require_api_key` (X-API-Key, comparaison timing-safe) | `app/auth.py` |
| Voice webhook | Reçoit `call.initiated` Telnyx, identifie le restaurant, encode `client_state`, déclenche `telnyx_answer` | `app/routers/voice_webhook.py` |
| Media-stream WS | Accepte le WS Telnyx, décode `client_state`, charge restaurant + menu, délègue au bridge | `app/routers/voice_webhook.py` |
| Realtime bridge | Trois tâches asyncio : Telnyx→OpenAI, greeting, OpenAI→Telnyx (+ tools, barge-in) | `app/services/realtime_service.py` |
| Tool executor | Implémente `create_reservation`, `create_commande`, `transfer_to_human` (quotas, DB, SMS, recap vocal) | `app/services/tool_service.py` |
| Telnyx client | `telnyx_answer` + `telnyx_transfer` via `urllib.request` | `app/services/telnyx_service.py` |
| SMS client | `send_sms` via Telnyx Messaging API, normalisation E.164 | `app/services/sms_service.py` |
| Phone utils | `normalize_phone`, `to_e164`, `matches_phone` | `app/utils/phone.py` |
| REST routers | CRUD `restaurants`, lecture `reservations`/`commandes`, bulk `menu` | `app/routers/{restaurants,reservations,commandes,menu}.py` |
| Dashboard router | Login HMAC + pages Jinja2 (restaurants, menu, reservations, commandes) | `app/routers/dashboard.py` |
| Templates | Vues Jinja2 admin | `dashboard/templates/*.html` |
| System prompt | Personnalité MIA (FR), règles métier, instructions tools | `prompts/mia_system_prompt.md` |

## Pattern Overview

**Overall:** Monolithe FastAPI structuré en couches (routers → services → models), avec un *bridge* WebSocket asynchrone dédié pour le pipeline audio temps réel.

**Key Characteristics:**
- Trois surfaces HTTP coexistent dans une seule app : webhooks Telnyx (`/voice/*`, publics), API REST (`/restaurants`, `/menu`, ... protégée par `X-API-Key`), dashboard admin (`/dashboard/*` protégé par cookie HMAC).
- Le coeur métier (création résa/commande, transfert) vit dans `app/services/tool_service.py` et est invoqué uniquement par OpenAI Realtime via des *function calls* — il n'y a pas d'API REST permettant de créer une résa "à la main".
- Couplage faible OpenAI ↔ FastAPI : seules les définitions de tools (`REALTIME_TOOLS`) et le prompt système (`prompts/mia_system_prompt.md`) décrivent le contrat avec le modèle.
- Le `client_state` Telnyx (base64 JSON) sert de mécanisme de continuité entre le webhook HTTP et la WebSocket — évite un second lookup DB et passe `restaurant_id`, `caller_phone`, `call_control_id`.
- Pas d'async ORM : SQLAlchemy synchrone, et les appels DB / HTTP du bridge sont déportés via `asyncio.to_thread` pour ne pas bloquer la boucle asyncio qui sert l'audio.

## Layers

**Routers (HTTP/WebSocket surface):**
- Purpose : Définir les endpoints, déserialiser, déléguer aux services
- Location : `app/routers/`
- Contains : `voice_webhook.py` (Telnyx), `dashboard.py` (Jinja2), `restaurants.py`, `reservations.py`, `commandes.py`, `menu.py`
- Depends on : `app/services/*`, `app/models.py`, `app/schemas.py`, `app/database.get_db`
- Used by : `app/main.py` via `include_router`

**Services (logique métier et intégrations externes):**
- Purpose : Bridge audio, exécution des tools, clients HTTP Telnyx/SMS
- Location : `app/services/`
- Contains : `realtime_service.py`, `tool_service.py`, `telnyx_service.py`, `sms_service.py`
- Depends on : `app/models.py`, `app/database.SessionLocal`, `app/config.settings`, `prompts/mia_system_prompt.md`
- Used by : routers (`voice_webhook`), et `realtime_service` qui appelle `tool_service` puis `telnyx_service`

**Modèles & persistance:**
- Purpose : Représentation tables PostgreSQL + session SQLAlchemy
- Location : `app/models.py`, `app/database.py`, `app/schemas.py`
- Contains : `Restaurant`, `Reservation`, `Commande`, `MenuItem`, `Base`, `SessionLocal`, schémas Pydantic
- Depends on : `app/config.settings.database_url`
- Used by : tous les routers et `tool_service`

**Cross-cutting (config, auth, utils):**
- Purpose : Configuration `.env`, garde API, normalisation téléphone
- Location : `app/config.py`, `app/auth.py`, `app/utils/phone.py`
- Used by : partout

## Data Flow

### Primary Request Path — Appel téléphonique entrant

1. Telnyx POST `/voice/incoming` avec `data.event_type=call.initiated` (`app/routers/voice_webhook.py:54`).
2. `_find_restaurant` matche `payload.to` contre `restaurants.incoming_phone_number` (fallback mono-restaurant) (`voice_webhook.py:32`).
3. Encodage `client_state` = base64(JSON `{restaurant_id, caller_phone, call_control_id}`) (`voice_webhook.py:91`).
4. `telnyx_answer(call_control_id, wss://…/voice/media-stream, client_state)` (`app/services/telnyx_service.py:21`).
5. Telnyx ouvre le WebSocket → `handle_media_stream` accepte, bufferise jusqu'à `start`, décode `client_state` (`voice_webhook.py:102`).
6. Chargement restaurant + menu en un round-trip, sérialisé en dicts (`voice_webhook.py:141`).
7. `run_realtime_bridge` ouvre une WSS `wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview`, envoie `session.update` (PCMU in/out, `server_vad`, tools) (`app/services/realtime_service.py:144`, `:114`).
8. Trois coroutines lancées via `asyncio.gather` :
   - `send_greeting()` : attend `stream_ready`, pousse un item user virtuel + `response.create` (`realtime_service.py:227`).
   - `receive_from_client()` : pour chaque `media` Telnyx, strip 12 bytes RTP header, forward µ-law en `input_audio_buffer.append` à OpenAI (`realtime_service.py:187`).
   - `send_to_client()` : lit les events OpenAI, forward `response.output_audio.delta` en `media` Telnyx, gère barge-in (`speech_started` → `response.cancel` + Telnyx `clear`), exécute `function_call` via `asyncio.to_thread(execute_tool_call, …)` (`realtime_service.py:245`).
9. Si un tool retourne `transfer: True`, attendre fin du dernier audio (3 s), puis `telnyx_transfer(call_control_id, restaurant.telephone)` (`realtime_service.py:353`, `telnyx_service.py:52`).

### Secondary Flow — Création de réservation/commande (via tool)

1. OpenAI émet `response.done` avec `function_call` (`create_reservation` ou `create_commande`).
2. `execute_tool_call` dispatche vers `_create_reservation` / `_create_commande` (`app/services/tool_service.py:46`).
3. Vérification quota journalier (count SQL sur `Reservation.date` ou `func.date(Commande.created_at)`).
4. Génération `code = _code("R"|"C")` (4 caractères `secrets.choice` dans alphabet sans ambiguïté).
5. Insert SQLAlchemy + `commit()`.
6. `send_sms(restaurant.telephone, …)` puis `send_sms(caller_phone, …)` — échecs silencieux.
7. Retour `{success, code, recap_vocal}` → `realtime_service` renvoie `function_call_output` + `response.create` → MIA lit le récap au client.

### Tertiary Flow — Dashboard admin

1. `GET /dashboard/login` → form Jinja2 (`dashboard/templates/login.html`).
2. `POST /dashboard/login` : `hmac.compare_digest(password, settings.dashboard_password)` → cookie signé `mia_session=ts:HMAC256(secret, "mia:ts")[:32]`, `httponly`, `samesite=lax`, path `/dashboard` (`app/routers/dashboard.py:92`).
3. Chaque route appelle `_require_login(request)` → redirige si cookie invalide ou expiré (>7 j).
4. CRUD restaurants + menu (forms), lecture seule pour réservations/commandes (limitées à 100 lignes).

**State Management:**
- Aucun état serveur partagé entre requêtes hors PostgreSQL.
- Pendant un appel : état local au bridge (`stream_sid`, `last_item`, `current_response_id`, `pending_transfer`, `stream_ready: asyncio.Event`) capturé dans des `nonlocal`.
- `Settings` figée chargée une fois au démarrage du module `app.config`.

## Key Abstractions

**`Settings` (frozen dataclass):**
- Purpose: Configuration centralisée, lecture .env unique au chargement
- Examples: `app/config.py:40`
- Pattern: Singleton module-level (`from app.config import settings`), propriétés calculées (`stream_wss_domain`, `has_default_secrets`)

**`client_state` (base64 JSON Telnyx):**
- Purpose: Passer le contexte de l'appel du webhook HTTP au WebSocket sans seconde requête DB
- Examples: encodé `app/routers/voice_webhook.py:91`, décodé `voice_webhook.py:129`
- Pattern: Token opaque transporté par Telnyx, jamais persisté

**Realtime bridge (3 coroutines + état nonlocal):**
- Purpose: Couplage bidirectionnel WS Telnyx ↔ WS OpenAI sur la durée d'un appel
- Examples: `app/services/realtime_service.py:144`
- Pattern: `asyncio.gather` sur tâches concurrentes, `asyncio.Event` pour synchronisation, `asyncio.to_thread` pour blocs synchrones (DB, HTTP)

**Tool dispatcher:**
- Purpose: Point d'entrée unique entre OpenAI function calls et logique métier
- Examples: `app/services/tool_service.py:46` (`execute_tool_call`)
- Pattern: Dispatcher par nom de fonction, retourne dict `{success, recap_vocal, transfer?}`

**Code court (`_code(prefix)`):**
- Purpose: Identifiant lisible à l'oral (sans 0/O, 1/I/L, 5/S, 8/B) communiqué au client et au restaurateur
- Examples: `app/services/tool_service.py:30`
- Pattern: 1 lettre + 4 caractères `secrets.choice` (cryptographiquement sûr)

**Dashboard session token:**
- Purpose: Session signée HMAC-SHA256, sans état serveur
- Examples: `app/routers/dashboard.py:42` (`_sign_token`)
- Pattern: `"timestamp:hex_sig"` dans cookie, vérifié à chaque requête via `hmac.compare_digest`

## Entry Points

**FastAPI ASGI app:**
- Location: `app/main.py:24` (`app = FastAPI(...)`)
- Triggers: `uvicorn app.main:app --reload`
- Responsibilities: Construit l'app, applique CORS, inclut les routers, exécute `_check_config` au startup

**Health check:**
- Location: `app/main.py:60` (`GET /`)
- Triggers: Render / fly.io / curl
- Responsibilities: Renvoie `{"status": "ok", "app": "MIA"}`

**Telnyx voice webhook:**
- Location: `app/routers/voice_webhook.py:54` (`POST /voice/incoming`)
- Triggers: Appel téléphonique sur un numéro Telnyx configuré
- Responsibilities: Identifier le restaurant, demander à Telnyx d'ouvrir le stream

**Telnyx media stream:**
- Location: `app/routers/voice_webhook.py:102` (`WS /voice/media-stream`)
- Triggers: Connexion WebSocket ouverte par Telnyx après `telnyx_answer`
- Responsibilities: Lancer le bridge OpenAI Realtime pour la durée de l'appel

**Dashboard:**
- Location: `app/routers/dashboard.py` (préfixe `/dashboard`)
- Triggers: Navigateur admin
- Responsibilities: Gestion restaurants/menus, consultation résas/commandes

**API REST:**
- Location: `app/routers/{restaurants,reservations,commandes,menu}.py`
- Triggers: Scripts externes, curl, intégrations
- Responsibilities: CRUD restaurants + menu, lecture résas/commandes

## Architectural Constraints

- **Threading :** boucle asyncio unique servie par uvicorn ; tout I/O bloquant (SQLAlchemy synchrone, `urllib.request` SMS/Telnyx, exécution des tools) est wrappé dans `asyncio.to_thread` côté `realtime_service.py:329`, `:357`. Le bridge ne doit jamais `await` directement un appel bloquant.
- **Global state :** `app.config.settings` (singleton figé) et `app.database.engine` / `SessionLocal` (singletons module-level). Le `SYSTEM_PROMPT` est lu une fois au chargement de `realtime_service.py:42`.
- **Circular imports :** aucun détecté. La dépendance est strictement routers → services → models.
- **Pas d'ORM async :** chaque tool ouvre/ferme sa propre `SessionLocal()` (cf. `tool_service._create_reservation`) plutôt que d'utiliser la session FastAPI `Depends(get_db)` (car invoqué hors contexte requête, dans un thread).
- **`client_state` non signé :** la confiance dans Telnyx est implicite ; un attaquant ouvrant directement `/voice/media-stream` avec un `client_state` forgé pourrait charger n'importe quel `restaurant_id` (pas de signature HMAC sur ce token).
- **Pas de transcription d'entrée configurée côté OpenAI :** délibéré (cf. commentaire `realtime_service.py:124`) — le modèle audio natif est plus robuste que whisper sur du 8 kHz téléphonique.

## Anti-Patterns

### Ouvrir une session DB sans la fermer dans un tool

**What happens:** Du code récent pourrait être tenté d'instancier `SessionLocal()` sans `try/finally`.
**Why it's wrong:** Les tools s'exécutent dans un `asyncio.to_thread` ; toute fuite de session bloque le pool pendant la durée de l'appel.
**Do this instead:** Suivre le pattern `app/services/tool_service.py:75-93` : `db = SessionLocal(); try: ... finally: db.close()`.

### Appeler directement un client HTTP depuis la boucle asyncio du bridge

**What happens:** Faire `urllib.request.urlopen(...)` ou `db.commit()` directement dans `send_to_client()`.
**Why it's wrong:** Bloque la boucle asyncio qui sert l'audio, provoque cracks et latence sur la voix.
**Do this instead:** Wrapper via `await asyncio.to_thread(fn, *args)` comme fait pour `execute_tool_call` (`realtime_service.py:329`) et `telnyx_transfer` (`realtime_service.py:357`).

### Demander le numéro de téléphone à l'appelant

**What happens:** Ajouter dans le prompt ou un tool une question "quel est votre numéro ?".
**Why it's wrong:** Le `caller_phone` est déjà connu via Telnyx caller ID et injecté dans `_build_instructions` (`realtime_service.py:92`). Le redemander dégrade l'UX.
**Do this instead:** Toujours utiliser `caller_phone` du `client_state` ; respecter la consigne "Ne jamais demander le numéro" injectée dans le prompt.

### Renvoyer le payload Telnyx brut à OpenAI

**What happens:** Forwarder `media["payload"]` (base64) tel quel sans dépayloter le header RTP.
**Why it's wrong:** Telnyx en mode `rtp` ajoute 12 bytes de header avant le µ-law brut ; OpenAI attend du µ-law pur → hallucinations audio.
**Do this instead:** Strip `rtp_pkt[12:]` avant de re-encoder en base64 (`realtime_service.py:211-215`).

### Faire échouer une réservation si le SMS ne part pas

**What happens:** Lever une exception en cas d'erreur Telnyx Messaging.
**Why it's wrong:** La résa est déjà committée et le client a entendu MIA la confirmer. Échec SMS ≠ échec métier.
**Do this instead:** Pattern "échec silencieux" de `app/services/sms_service.py:50-58` : logger et retourner `False`.

## Error Handling

**Strategy:** Best-effort téléphonique — toute erreur secondaire est loguée mais ne casse pas l'appel.

**Patterns:**
- Webhooks Telnyx renvoient toujours `200 OK` (même sur numéro inconnu) pour éviter les retries Telnyx (`voice_webhook.py:86`).
- Le bridge ignore les erreurs OpenAI bénignes (`item_truncate_invalid_item_id`, `input_audio_buffer_commit_empty`) au lieu de fermer la session (`realtime_service.py:257`).
- SMS et `telnyx_transfer` retournent `bool` ; les erreurs HTTP sont loguées avec un extrait du corps (300 chars).
- Le WS Telnyx est fermé avec code `4000` (abnormal applicatif) si `client_state` invalide ou restaurant introuvable.
- `_safe_send` vérifie `WebSocketState.CONNECTED` avant chaque envoi pour éviter les crashes sur déconnexion (`realtime_service.py:98`).

## Cross-Cutting Concerns

**Logging:**
- `logging.basicConfig` configuré dans `app/main.py:21`.
- Loggers nommés par sous-système : `mia`, `mia.voice`, `mia.realtime`, `mia.conv` (contenu conversationnel), `mia.tools`, `mia.telnyx`, `mia.sms`.
- ID d'appel court `cid = uuid4().hex[:8]` injecté dans tous les logs du bridge pour corrélation.
- Numéros de téléphone masqués dans `mia.sms` via `_mask(phone)` (RGPD).

**Validation:**
- Entrées REST : Pydantic via `app/schemas.py` (`Field(..., min_length=1)`, `gt=0`, etc.).
- Entrées tools (depuis OpenAI) : revérifiées dans `tool_service` (`max(int(personnes), 1)`, etc.) — on ne fait pas confiance au JSON du modèle.
- Numéros : `to_e164` et `matches_phone` (`app/utils/phone.py`) en sortie SMS / lookup restaurant.

**Authentication:**
- REST : `X-API-Key` via `Depends(require_api_key)` (`app/auth.py:20`), comparaison timing-safe, désactivé si `MIA_API_KEY` vide.
- Dashboard : cookie HMAC-SHA256 signé, expiration 7 jours, `httponly` + `samesite=lax`, path `/dashboard`.
- Webhook Telnyx : aucune authentification (à durcir — pas de vérif signature webhook côté Telnyx).
- OpenAI : `Authorization: Bearer {settings.openai_api_key}` envoyé à la connexion WSS.

---

*Architecture analysis: 2026-05-13*
