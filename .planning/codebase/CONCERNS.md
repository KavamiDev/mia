# Codebase Concerns

**Analysis Date:** 2026-05-13

> Audit transverse du code MIA (FastAPI + OpenAI Realtime + Telnyx + PostgreSQL).
> Le code est volontairement « lite » (~1300 LOC, post-refactor `28a9f32`), ce qui
> en fait un atout — mais plusieurs angles morts mettent en risque la robustesse
> téléphonique, la sécurité dashboard et la maintenabilité long terme.

---

## Tech Debt

### Absence totale de tests automatisés

- Issue : Aucun fichier `test_*.py`, `*_test.py` ou `conftest.py` dans le dépôt.
  Pas de framework de test installé (pas de `pytest` dans `requirements.txt`).
  Tous les fixes audio récents (`48120d0`, `390b462`, `63596e1`) ont été validés
  manuellement par appel téléphonique en prod.
- Fichiers : `requirements.txt` (pas de `pytest`), absence dans `app/`
- Impact : Chaque modification du bridge audio peut casser silencieusement le
  flux PCMU. Cycle de feedback = appel réel → écoute → redéploiement.
  Régressions probables (la séquence wrap → chunk → revert RTP sortant en 3
  commits le démontre).
- Fix approach : Ajouter `pytest` + `pytest-asyncio`. Prioriser :
  1. Tests unitaires sur `app/utils/phone.py` (pur, trivial à couvrir)
  2. Tests sur `app/services/tool_service.py` avec SQLite en mémoire
  3. Tests d'intégration sur le bridge via `WebSocket` mocké
     (faux client Telnyx + faux serveur OpenAI Realtime)

### Pas de système de migrations DB

- Issue : Schéma défini deux fois — `app/models.py` (SQLAlchemy) et
  `scripts/init_db.sql` (DDL manuel). Aucun outil (Alembic, sqitch) pour
  versionner les changements. Tout `ALTER TABLE` est manuel sur la prod.
- Fichiers : `scripts/init_db.sql`, `app/models.py`
- Impact : Drift garanti entre dev et prod dès qu'un champ est ajouté.
  Risque d'oubli de colonne en prod → 500 silencieux.
  Pas d'historique des changements DB.
- Fix approach : Introduire Alembic (`alembic init`), générer la baseline
  depuis les modèles actuels, supprimer `init_db.sql` ou le marquer
  « bootstrap initial uniquement ».

### Déploiement manuel et non reproductible

- Issue : « git pull + systemctl restart » selon le contexte fourni. Pas de
  pipeline CI, pas de healthcheck post-deploy, pas de rollback automatisé,
  pas de Dockerfile / image versionnée.
- Fichiers : Absence de `.github/workflows/`, `Dockerfile`, `docker-compose.yml`
- Impact : Drift de version Python possible entre dev/prod (pas de `.python-version`
  ni de pin de Python dans `requirements.txt`). Un push cassé interrompt les
  appels en cours (le `systemctl restart` tue les WebSockets actifs).
- Fix approach :
  1. Dockerfile + image pinnée Python 3.11
  2. `systemd` reload avec `ExecReload` + `KillSignal=SIGTERM` pour drainer
     les WS actives avant restart
  3. GitHub Actions minimal : lint (`ruff`) + test sur PR

### Types `String` pour des données structurées

- Issue : `Reservation.date: String(10)` et `Reservation.heure: String(10)`
  stockent date et heure en texte (`"2026-05-13"`, `"20h30"`).
- Fichiers : `app/models.py:48`, `app/models.py:51`, `scripts/init_db.sql:27`, `:29`
- Impact : Impossible de filtrer/trier proprement (`ORDER BY date DESC` est
  trié lexicographiquement — fonctionne par chance avec ISO8601 mais cassera
  dès qu'un format non-ISO se faufile). Aucune validation côté DB.
  La requête `func.date(Commande.created_at) == date.today()` dans
  `tool_service.py:130` mélange date serveur et date applicative — sources
  d'incohérence en cas de fuseaux.
- Fix approach : Migrer `date` en `DATE` et `heure` en `TIME`. Si format
  oral libre (« 20h », « 20h30 ») doit être conservé pour le récap vocal,
  ajouter une colonne `heure_normalisee: TIME` à côté.

### Pas de modèle Pydantic pour `Commande.items` (JSONB libre)

- Issue : `items: Any` côté schéma (`schemas.py:75`) et `JSONB` côté DB.
  Aucune validation de structure (`plat`, `qty`, `prix_unitaire`).
- Fichiers : `app/schemas.py:75`, `app/services/tool_service.py:137-143`
- Impact : Une modification du format côté `tool_service` (ex: ajout d'un
  champ `note`) ne sera détectée qu'à la lecture cassée du dashboard ou API.
- Fix approach : Définir `CommandeItem(BaseModel)` avec `plat: str`, `qty: int`,
  `prix_unitaire: float` et l'utiliser dans `schemas.py`.

### Bulk insert menu sans rollback global

- Issue : `app/routers/menu.py:35-47` (`create_menu_bulk`) ajoute item par item
  puis fait un seul `commit()`. Si un item provoque une erreur applicative
  *après* la boucle, les précédents sont perdus ; mais surtout, aucune
  validation d'unicité du nom — un même `nom_plat` peut être inséré N fois.
- Fichiers : `app/routers/menu.py:35-47`, `app/models.py:73-82`
- Impact : Menus dupliqués → `tool_service._create_commande` matche le premier
  trouvé via le dict `prices` (les doublons écrasent silencieusement).
- Fix approach : Contrainte `UNIQUE(restaurant_id, nom_plat)` en DB +
  `IntegrityError` géré → 409.

---

## Known Bugs

### Race condition sur le greeting si `start` arrive avant la tâche

- Issue : `send_greeting()` (`realtime_service.py:227`) attend `stream_ready`
  avec `timeout=5.0`. Mais `receive_from_client()` rejoue d'abord les
  messages bufférisés (dont `start`) AVANT de basculer sur `iter_text()`.
  Comme `asyncio.gather` lance les 3 coroutines en parallèle, la tâche
  `send_greeting` peut être schedulée après que `receive_from_client` ait
  déjà consommé et set l'event — fonctionne, mais l'ordre n'est garanti
  que parce que `await asyncio.wait_for` réordonne implicitement.
- Fichiers : `app/services/realtime_service.py:191-220`, `app/services/realtime_service.py:227-240`
- Trigger : Réseau lent côté Telnyx faisant arriver `start` en même temps
  que les premiers paquets `media`.
- Workaround : Aucun nécessaire en pratique, mais le code est fragile à
  toute modif de l'ordre des tâches.

### `last_item` et tracking barge-in capricieux

- Issue : `realtime_service.py:291-293` met à jour `last_item` sur n'importe
  quel event OpenAI portant un `item_id`, y compris des events qui ne
  correspondent pas à de l'audio en cours de lecture (ex: transcript de
  l'utilisateur). Le barge-in (`speech_started`) ne se déclenche alors
  qu'à condition d'avoir vu un `item_id` — qui peut ne pas être celui
  de la réponse audio active.
- Fichiers : `app/services/realtime_service.py:290-305`
- Trigger : Utilisateur qui parle pendant que MIA prépare une réponse
  (avant que `response.output_audio.delta` ne soit émis).
- Workaround : `current_response_id` est la vraie source de vérité —
  scoper le barge-in à `if current_response_id` plutôt que `if last_item`.

### Quota commandes sur `created_at` vs quota réservations sur `date`

- Issue : `tool_service.py:79-81` compte les résa par `Reservation.date == date_req`
  (date demandée par le client). `tool_service.py:128-131` compte les commandes
  par `func.date(Commande.created_at) == date.today()` (date serveur).
  Incohérence sémantique entre les deux quotas.
- Fichiers : `app/services/tool_service.py:79-81`, `app/services/tool_service.py:128-131`
- Trigger : Toute commande à emporter passée après minuit serveur — le quota
  redevient 0 même si la « journée commerciale » du restaurant n'est pas finie.
- Workaround : Aucun en l'état. Définir la sémantique (commande = date du jour
  serveur ou date logique du service ?) puis aligner.

### Numéros internationaux silencieusement cassés dans `to_e164`

- Issue : `app/utils/phone.py:18-39` ne gère que la France. Un numéro
  belge `+32...` sera retourné tel quel (OK), mais un numéro local
  `032xxx` sera incorrectement préfixé `+33` (FR) → SMS perdu.
- Fichiers : `app/utils/phone.py:31-38`
- Trigger : Restaurant en outre-mer / DOM-TOM (974/972/971/262) qui reçoit
  un appel d'un client en métropole sans préfixe.
- Workaround : `default_country` n'est jamais surchargé. Stocker le pays
  du restaurant et le passer à `to_e164`.

### `client_state` Telnyx en clair, base64 non signé

- Issue : `voice_webhook.py:91-93` encode `{restaurant_id, caller_phone,
  call_control_id}` en base64 et fait confiance à Telnyx pour le renvoyer
  intact dans le message `start`. Aucun MAC/HMAC pour vérifier l'intégrité.
- Fichiers : `app/routers/voice_webhook.py:91-93`, `app/routers/voice_webhook.py:128-136`
- Trigger : Si l'endpoint `/voice/media-stream` est exposé publiquement (ce
  qu'il EST pour que Telnyx s'y connecte), un attaquant peut s'y connecter
  avec un `client_state` forgé pointant sur un `restaurant_id` arbitraire.
- Workaround : Signer le `client_state` avec HMAC-SHA256 (réutiliser
  `dashboard_secret` ou un nouveau secret) et vérifier la signature dans
  `handle_media_stream`.

---

## Security Considerations

### Dashboard : pas de rate-limit, pas de lockout

- Risk : Brute-force du mot de passe dashboard (`DASHBOARD_PASSWORD`).
  La comparaison est timing-safe (`hmac.compare_digest` en `dashboard.py:95`)
  mais aucun délai/quota n'est imposé — un attaquant peut tester des
  millions de mots de passe.
- Fichiers : `app/routers/dashboard.py:92-107`
- Current mitigation : `hmac.compare_digest` (anti-timing), pas de
  user enumeration (un seul compte).
- Recommendations :
  1. Délai constant (`asyncio.sleep(0.5)`) après échec
  2. Rate-limit par IP via `slowapi` ou Nginx
  3. À terme : passer à des comptes utilisateurs + bcrypt

### Secrets fragiles : valeurs par défaut acceptées au boot

- Risk : `Settings.dashboard_password` fallback `"mia-admin"`,
  `dashboard_secret` fallback `"change-me-in-production"`. Un déploiement
  oublieux part avec ces valeurs et `main._check_config` ne fait que
  logger un WARNING (pas un crash).
- Fichiers : `app/config.py:53-56`, `app/main.py:42-56`
- Current mitigation : Warning au startup (`main.py:50`).
- Recommendations :
  1. `raise RuntimeError` au boot si `has_default_secrets` ET environnement
     ressemble à de la prod (ex: `BACKEND_URL` ne contient pas `localhost`/`ngrok`)
  2. `mia_api_key` vide = API ouverte sans auth — au minimum, exiger une clé
     si `BACKEND_URL` est en `https://`.

### Cookie dashboard sans `secure=True`

- Risk : `dashboard.py:98-105` set le cookie sans `secure=True`. En clair
  sur du HTTP, capturable en MITM.
- Fichiers : `app/routers/dashboard.py:98-105`
- Current mitigation : `httponly=True`, `samesite="lax"`.
- Recommendations : Ajouter `secure=True` (à minima conditionnel selon que
  `BACKEND_URL` commence par `https`).

### CORS `allow_origins=["*"]` sur l'API REST

- Risk : API REST avec X-API-Key + CORS wildcard = OK aujourd'hui (pas de
  cookies, donc `allow_credentials=False` empêche les attaques par session).
  Mais si demain un endpoint cookie-authentifié est ajouté à `app`, la
  surface s'ouvre.
- Fichiers : `app/main.py:29-30`
- Current mitigation : `allow_credentials=False` (documenté inline).
- Recommendations : Lister explicitement les origines autorisées, ou au
  minimum ajouter un commentaire bloquant tout futur passage à `credentials=True`.

### Pas de validation d'origine sur le webhook Telnyx

- Risk : N'importe qui peut POST `/voice/incoming` avec un payload Telnyx
  forgé et déclencher des appels `telnyx_answer` (qui consomme l'API key
  Telnyx et peut générer des coûts).
- Fichiers : `app/routers/voice_webhook.py:54-99`
- Current mitigation : Aucune (Telnyx supporte la signature webhook
  Ed25519, non implémentée ici).
- Recommendations : Vérifier le header `telnyx-signature-ed25519` +
  `telnyx-timestamp` avec la clé publique Telnyx avant de traiter.

### Logs : transcriptions intégrales en INFO

- Risk : `realtime_service.py:264-274` log les transcriptions complètes
  des conversations (client + MIA) au niveau INFO. Si les logs sont
  centralisés ou conservés longtemps, c'est de la donnée personnelle
  (RGPD : contenu des appels, noms, allergies éventuelles…).
- Fichiers : `app/services/realtime_service.py:264-274`
- Current mitigation : SMS masqués (`sms_service._mask`), mais pas la voix.
- Recommendations : Niveau DEBUG pour le contenu intégral, INFO pour les
  métadonnées seules. Politique de rétention courte sur les logs.

### `_spell()` épelle un code dans une URL/SMS sans masquage

- Risk : Faible — code aléatoire 4 chars cryptographiquement sûr (`secrets`).
  À noter : 4 chars dans un alphabet 27 = ~530k combinaisons. Avec quotas
  journaliers, collision possible (pas d'unicité forcée en DB sur `code`).
- Fichiers : `app/services/tool_service.py:30-37`, `app/models.py:46` (pas
  d'`unique=True` sur `code`)
- Recommendations : `UNIQUE(restaurant_id, code)` ou retry on collision.

---

## Performance Bottlenecks

### Bridge audio : `asyncio.to_thread` pour le SQL des tools bloque potentiellement

- Problem : `realtime_service.py:329` exécute le tool via `asyncio.to_thread`.
  C'est correct pour ne pas bloquer la boucle, mais le pool de threads par
  défaut (`min(32, os.cpu_count()+4)`) peut saturer si plusieurs appels
  simultanés font des tools en même temps avec une DB lente.
- Fichiers : `app/services/realtime_service.py:329-335`
- Cause : SQL synchrone (`SessionLocal()`) + appel HTTP synchrone vers
  Telnyx SMS dans le même thread (`tool_service.py:97-103`).
- Improvement path :
  1. Augmenter le pool : `loop.set_default_executor(ThreadPoolExecutor(max_workers=64))`
  2. À terme, async DB (`asyncpg` ou `sqlalchemy[asyncio]`) + `httpx.AsyncClient`
     pour les appels Telnyx.

### Pool DB sous-dimensionné par défaut

- Problem : `database.py:16` n'override ni `pool_size` ni `max_overflow`.
  Default SQLAlchemy = 5 + 10 = 15. Au-delà, requêtes en attente.
- Fichiers : `app/database.py:16`
- Cause : `create_engine(database_url, pool_pre_ping=True, pool_recycle=300)`
- Improvement path : `pool_size=10, max_overflow=20` + monitoring du pool.

### Chargement complet du menu à chaque appel

- Problem : `voice_webhook.py:151-152` charge tous les `MenuItem` du
  restaurant à chaque connexion WS, puis seuls les 30 premiers sont
  envoyés à OpenAI (`realtime_service.py:86`).
- Fichiers : `app/routers/voice_webhook.py:151-152`, `app/services/realtime_service.py:84-88`
- Cause : Pas de cache, pas de `LIMIT 30` côté DB.
- Improvement path : Cache LRU process-wide (TTL 60s) ou `LIMIT 30 ORDER BY id`.

### `urllib.request.urlopen` synchrone bloque le thread

- Problem : `telnyx_service.telnyx_answer` (timeout 10s) et
  `telnyx_transfer` (timeout 10s) sont synchrones. `telnyx_answer` est
  appelé directement dans `handle_telnyx_webhook` (async) — la boucle
  est bloquée jusqu'à 10s en cas de latence Telnyx.
- Fichiers : `app/routers/voice_webhook.py:97`, `app/services/telnyx_service.py:40`, `:78`
- Cause : `urllib` choisi pour éviter d'ajouter `httpx` (`sms_service.py:41`
  documente ce choix).
- Improvement path : `await asyncio.to_thread(telnyx_answer, ...)` à minima.
  Idéalement migrer vers `httpx.AsyncClient`.

### Quota check non atomique (race condition)

- Problem : `tool_service.py:79-87` fait `count() >= quota` puis `add/commit`.
  Deux appels parallèles peuvent dépasser le quota.
- Fichiers : `app/services/tool_service.py:79-87`, `:128-147`
- Cause : Pas de transaction `SERIALIZABLE` ni de `SELECT ... FOR UPDATE`.
- Improvement path : Contrainte `CHECK` impossible (count global) — utiliser
  un advisory lock PostgreSQL par `(restaurant_id, date)` autour de la
  séquence count+insert.

---

## Fragile Areas

### Bridge audio (3 tâches asyncio.gather)

- Files : `app/services/realtime_service.py:144-368`
- Why fragile :
  - 3 coroutines partagent 5 variables `nonlocal` (`stream_sid`, `stream_ready`,
    `last_item`, `current_response_id`, `pending_transfer`).
  - Si une tâche lève, `asyncio.gather` annule les autres mais l'audio
    bufferisé côté Telnyx peut continuer à jouer 1-2 secondes.
  - L'historique git (`48120d0` → `63596e1` → `390b462`) montre 3 itérations
    sur le format RTP en 24h — fix audio = trial and error.
- Safe modification : Toujours tester en condition d'appel réel après
  modification. Ne pas toucher à `receive_from_client` sans vérifier le
  parsing RTP (`raw_ulaw = rtp_pkt[12:]`).
- Test coverage : 0%. Aucune simulation Telnyx ni mock OpenAI Realtime.

### Singleton `Settings` figé au chargement du module

- Files : `app/config.py:85`
- Why fragile : Les valeurs sont lues à l'import, jamais relues. Tout
  changement de `.env` nécessite un restart complet (qui tue les appels
  WS en cours). `_check_config` (`main.py:42`) ne vérifie qu'au startup.
- Safe modification : Si le déploiement reload `.env`, ajouter un
  endpoint `/admin/reload-config` protégé qui recrée le singleton.
- Test coverage : 0%.

### Prompt système chargé une seule fois au module load

- Files : `app/services/realtime_service.py:42`
- Why fragile : `SYSTEM_PROMPT = Path(...).read_text(...)` au top du
  module. Toute modification du prompt nécessite restart. Si le fichier
  est absent au boot, ImportError → crash silencieux du service.
- Safe modification : Wrap dans try/except + fallback minimal, ou
  recharger à chaque appel (acceptable car 1 lecture / appel = négligeable).

### URL WSS dérivée d'une string `BACKEND_URL`

- Files : `app/config.py:73-76`, `app/routers/voice_webhook.py:94`
- Why fragile : `stream_wss_domain` fait du `replace("https://", "")`. Une
  URL avec un port (`https://host:8443`) ou un path (`https://host/api`)
  produira une `wss://host:8443/voice/media-stream` peut-être valide,
  peut-être non.
- Safe modification : Utiliser `urllib.parse.urlparse` proprement.

### Fallback mono-restaurant silencieux

- Files : `app/routers/voice_webhook.py:40-45`
- Why fragile : Si un seul restaurant est configuré, n'importe quel appel
  entrant lui sera routé même si le numéro `to_num` ne correspond pas.
  Pratique en dev, dangereux en multi-tenant si on passe en prod avec
  un seul restaurant temporairement (ex: pendant onboarding d'un nouveau).
- Safe modification : Ajouter un flag `settings.allow_mono_restaurant_fallback`
  désactivé par défaut.

---

## Scaling Limits

### Process unique, pas de worker

- Current capacity : 1 process uvicorn (cf. README `uvicorn app.main:app --reload`).
  En prod, probablement aussi 1 worker via `systemctl` (à confirmer dans le
  fichier `.service` non versionné).
- Limit : Tous les appels passent par la même boucle asyncio + même pool
  DB (15 conns). Estimation : ~20-30 appels concurrents max avant
  saturation (audio 8kHz µ-law = 8 KB/s par direction, négligeable
  bande passante ; mais le pool de threads pour les tools devient
  goulot).
- Scaling path :
  1. Uvicorn `--workers N` pose problème (les WS sont par-worker, OK,
     mais le `SessionLocal` doit être thread-safe — il l'est)
  2. Multi-instance derrière load balancer (Telnyx routera vers une
     instance, la WS suivra l'IP) — nécessite un LB sticky.

### Codes réservation/commande : entropie limitée

- Current capacity : 4 chars × 27 symboles = ~531k codes par préfixe.
- Limit : À ~5 % d'occupation (~26k codes), collision probable (paradoxe
  des anniversaires). Pas de contrainte d'unicité DB.
- Scaling path : Passer à 5 chars (~14M) ou ajouter unique constraint +
  retry.

### Logs in-process, pas centralisés

- Current capacity : `logging.basicConfig` → stderr, capturé par systemd.
- Limit : Aucune corrélation cross-call sur plusieurs instances, pas de
  recherche/agrégation.
- Scaling path : `structlog` ou JSON logs + ship vers Loki/Datadog.

---

## Dependencies at Risk

### `openai==1.12.0` pinné

- Risk : Version figée de février 2024. L'API Realtime a beaucoup évolué
  (renommages, nouveaux events). `gpt-4o-realtime-preview` dans le code
  est un modèle preview qui peut être déprécié.
- Impact : Si OpenAI déprécie `gpt-4o-realtime-preview`, l'app meurt
  silencieusement (erreur sur `session.update`).
- Migration plan : Suivre les release notes OpenAI Realtime ; passer à
  `gpt-realtime` (GA) quand stable.

### `gpt-4o-mini-realtime` mentionné dans README mais `gpt-4o-realtime-preview` utilisé

- Risk : Incohérence README vs code. `realtime_service.py:130` et `:170`
  utilisent `gpt-4o-realtime-preview`. Le README annonce `gpt-4o-mini-realtime`.
- Files : `README.md:32`, `app/services/realtime_service.py:130`, `:170`
- Impact : Confusion sur les coûts (mini est ~4x moins cher) et capacités.
- Migration plan : Soit aligner le code sur mini (test qualité d'écoute FR
  téléphonique), soit corriger le README.

### `psycopg2-binary` en prod (non recommandé officiellement)

- Risk : `psycopg2-binary` est documenté comme dev/CI seulement par les
  mainteneurs (problèmes SSL avec OpenSSL système).
- Files : `requirements.txt:10`
- Impact : Risque sur reconnections SSL longues / certificats.
- Migration plan : Passer à `psycopg[binary]` (v3) ou `psycopg2` non-binary
  compilé contre OpenSSL système.

### `python-dotenv` chargé silencieusement (try/except ImportError)

- Risk : `config.py:14-18` swallow l'ImportError. Si `python-dotenv`
  manque en prod, le `.env` n'est pas chargé et tous les fallbacks
  (mot de passe par défaut, etc.) s'activent silencieusement.
- Files : `app/config.py:14-18`
- Migration plan : Logger un WARNING si import échoue ET un `.env` existe
  sur disque.

---

## Missing Critical Features

### Pas de monitoring / observabilité

- Problem : Aucune métrique exposée (Prometheus, OpenTelemetry).
  Aucun tracing distribué entre Telnyx webhook → bridge → OpenAI → DB.
- Blocks : Diagnostic difficile en cas d'appel raté en prod.
  Pas de SLO mesurable (taux de réussite des réservations, latence
  greeting, etc.).

### Pas de gestion des erreurs OpenAI au-delà de 2 codes

- Problem : `realtime_service.py:257` ignore seulement deux codes
  d'erreur. Tout autre `type=error` → `return` qui tue la session
  (et donc l'appel) sans tentative de récupération.
- Blocks : Robustesse appel — un blip OpenAI (rate-limit, brief
  network) raccroche brutalement.

### Pas de reconnexion WebSocket OpenAI

- Problem : Si la WS OpenAI tombe pendant un appel, `websockets.connect`
  n'a pas de retry. L'appel est perdu.
- Blocks : SLA téléphonique. Idéalement, fallback vers transfert humain
  automatique sur erreur OpenAI prolongée.

### Pas de timeout global d'appel

- Problem : Un appel peut théoriquement durer des heures (boucle infinie
  `async for msg in openai_ws`). Pas de circuit-breaker côté serveur.
- Blocks : Coûts OpenAI Realtime non plafonnés ($0.06/min input audio).
  Un appel oublié = $$$.
- Fix : `asyncio.wait_for(asyncio.gather(...), timeout=600)` (10 min max).

### Pas de désactivation d'un restaurant

- Problem : Pas de champ `is_active` sur `Restaurant`. Pour suspendre un
  restaurant, il faut supprimer son `incoming_phone_number` (cascade UI).
- Files : `app/models.py:19-37`
- Blocks : Onboarding/offboarding propre, mode maintenance.

---

## Test Coverage Gaps

> **Tout** est non testé. Liste priorisée par ratio risque/effort.

### `app/utils/phone.py` — 0% — Priorité HAUTE (effort minimal)

- What's not tested : `normalize_phone`, `to_e164`, `matches_phone`.
- Risk : Numéros mal formatés → SMS perdus → clients non confirmés.
- Files : `app/utils/phone.py`
- Effort : 10 lignes par fonction, pas de mock.

### `app/services/tool_service.py` — 0% — Priorité HAUTE

- What's not tested : `_code` (unicité statistique), `_create_reservation`
  (quota), `_create_commande` (calcul total, plat inconnu).
- Risk : Bug dans le calcul de prix = SMS envoyé avec mauvais montant au
  client. Quota mal vérifié = restaurant débordé.
- Files : `app/services/tool_service.py`
- Effort : Moyen — nécessite SQLite/Postgres test fixture + mock `send_sms`.

### `app/routers/dashboard.py` auth — 0% — Priorité HAUTE

- What's not tested : `_sign_token`, `_verify_token`, expiration, signature
  altérée.
- Risk : Bypass d'authentification dashboard (= accès complet aux données
  de tous les restaurants).
- Files : `app/routers/dashboard.py:42-65`
- Effort : Faible (fonctions pures).

### `app/services/realtime_service.py` bridge — 0% — Priorité MOYENNE

- What's not tested : Parsing RTP, barge-in, exécution function call,
  pending_transfer, déconnexion WS client/OpenAI.
- Risk : Régression silencieuse à chaque commit (cf. série de fix RTP).
- Files : `app/services/realtime_service.py`
- Effort : Élevé — mock complet WS Telnyx + WS OpenAI Realtime.

### `app/routers/voice_webhook.py` — 0% — Priorité MOYENNE

- What's not tested : `_find_restaurant` (matching, fallback mono),
  `handle_media_stream` (buffer initial, client_state corrompu).
- Risk : Routage erroné multi-tenant, crash si `start` malformé.
- Files : `app/routers/voice_webhook.py`
- Effort : Moyen — FastAPI TestClient + WS de test.

### Intégration end-to-end — 0% — Priorité BASSE (effort très élevé)

- What's not tested : Webhook Telnyx mocké → answer → WS → bridge →
  function_call → SMS → transcript.
- Risk : Régressions sur le chemin nominal.
- Effort : Très élevé. Faisable avec `pytest-asyncio` + fakes complets,
  mais investissement initial conséquent.

---

*Concerns audit: 2026-05-13*
