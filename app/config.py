"""Configuration MIA — chargée depuis les variables d'environnement (.env).

On utilise une dataclass figée (frozen=True) au lieu de pydantic-settings :
moins de magie, moins de dépendances, et l'interface reste identique
(`from app.config import settings` puis `settings.openai_api_key`).

Les valeurs sont lues UNE SEULE FOIS au chargement du module, donc tout
changement d'env nécessite un redémarrage du process.
"""
import os
from dataclasses import dataclass

# Charge le .env s'il existe (optionnel : en prod on injecte les vars directement).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# Helpers de parsing : retournent un défaut typé si la var est absente/invalide.
def _s(k, d=None):
    """String : strip les espaces, retourne default si absent."""
    v = os.getenv(k, d)
    return v.strip() if isinstance(v, str) else v


def _f(k, d):
    """Float : retourne d si la conversion échoue."""
    try: return float(os.getenv(k) or d)
    except ValueError: return d


def _i(k, d):
    """Int : retourne d si la conversion échoue."""
    try: return int(os.getenv(k) or d)
    except ValueError: return d


@dataclass(frozen=True)
class Settings:
    # --- Base de données et APIs externes ---
    database_url: str = _s("DATABASE_URL", "postgresql://localhost:5432/mia")
    openai_api_key: str | None = _s("OPENAI_API_KEY")
    telnyx_api_key: str | None = _s("TELNYX_API_KEY")
    telnyx_phone_number: str | None = _s("TELNYX_PHONE_NUMBER")  # Expéditeur SMS
    # Clé publique Telnyx pour vérifier la signature Ed25519 des webhooks.
    # À récupérer sur https://portal.telnyx.com (Account > Public Key).
    telnyx_public_key: str | None = _s("TELNYX_PUBLIC_KEY")

    # --- Brevo SMS (fallback pour +262 La Réunion / Mayotte) ---
    # Telnyx ne livre pas de SMS vers les DOM Océan Indien. On bascule sur
    # Brevo (ex-Sendinblue) qui accepte les clients depuis tout pays et
    # livre vers +262. API simple : 1 seule clé. Si non configurée, les
    # SMS +262 échouent silencieusement (log warning, pas de crash).
    # Setup : https://app.brevo.com/settings/keys/api → générer une clé v3.
    brevo_api_key: str | None = _s("BREVO_API_KEY")
    brevo_sender: str = _s("BREVO_SENDER", "MIA")  # ≤ 11 chars alphanum, validé côté Brevo

    # --- Sécurité ---
    # Protège l'API REST /restaurants, /menu, etc. via header X-API-Key.
    # Si vide : aucune protection (acceptable en dev local seulement).
    mia_api_key: str | None = _s("MIA_API_KEY")
    # Mot de passe d'accès au dashboard admin.
    dashboard_password: str = _s("DASHBOARD_PASSWORD", "mia-admin")
    # Secret HMAC-SHA256 pour signer les cookies de session du dashboard.
    # DOIT être >= 32 caractères aléatoires en production.
    dashboard_secret: str = _s("DASHBOARD_SECRET", "change-me-in-production")

    # --- Réseau ---
    backend_url: str = _s("BACKEND_URL", "http://localhost:8000")
    # Si défini, utilisé à la place de backend_url pour construire l'URL WSS
    # du stream Telnyx (utile derrière un reverse proxy / tunnel).
    voice_realtime_domain: str | None = _s("VOICE_REALTIME_DOMAIN")

    # --- Voix OpenAI Realtime ---
    voice_realtime_voice: str = _s("VOICE_REALTIME_VOICE", "coral")
    # Modèle Realtime. OpenAI renomme régulièrement, d'où la variable :
    #   - "gpt-realtime"       → alias vers la dernière version stable (recommandé)
    #   - "gpt-realtime-mini"  → ~50 % moins cher, qualité légèrement inférieure
    #   - "gpt-realtime-2"     → version explicite (immuable)
    # Si OpenAI sort un nouveau modèle, on ajuste l'env sans toucher au code.
    openai_realtime_model: str = _s("OPENAI_REALTIME_MODEL", "gpt-realtime")

    # Modèle de transcription (whisper-like). À aligner avec le modèle Realtime :
    #   - "gpt-4o-transcribe"     → bon défaut, compatible avec gpt-realtime
    #   - "gpt-realtime-whisper"  → spécifique gpt-realtime, à tester si gpt-4o-transcribe hallucine
    #   - "whisper-1"             → ancien, très stable, bonne base de fallback
    openai_transcription_model: str = _s("OPENAI_TRANSCRIPTION_MODEL", "gpt-4o-transcribe")
    # VAD = Voice Activity Detection. Contrôle quand OpenAI considère
    # que l'utilisateur a fini de parler et déclenche une réponse.
    #
    # Calibration latence (goal : <1.5 s perçue) :
    #   silence_duration_ms + inference OpenAI (~400ms) + réseau (~150ms) = latence totale
    #   À 900ms → ~1.45s perçue. Compromis : capture les mots courts (« oui »)
    #   sans trop allonger la latence.
    #
    # threshold=0.5 (au lieu de 0.6) : plus sensible aux sons faibles
    #   (« hummm », « oui » courts, voix Réunion compressée GSM).
    #   À 0.6, on observait des « oui » non détectés après le récap → résa bloquée.
    #
    # silence_duration_ms=900 (au lieu de 700) : laisse le temps aux mots
    #   isolés comme « oui » d'être pleinement bufferisés avant déclenchement.
    vad_threshold: float = _f("VAD_THRESHOLD", 0.5)              # 0..1, sensibilité (compromis téléphonique/bruit)
    vad_prefix_padding_ms: int = _i("VAD_PREFIX_PADDING_MS", 400) # Capture mieux le DÉBUT du mot
    vad_silence_duration_ms: int = _i("VAD_SILENCE_DURATION_MS", 900)   # Silence min — laisse les "oui" courts être captés

    # VAD adaptatif : quand MIA vient de poser une question de validation
    # (« Je valide ? »), la réponse attendue est courte (« oui ») — pas besoin
    # d'attendre 900ms de silence. On abaisse temporairement le VAD à cette
    # valeur pour ce tour-là, puis on restaure 900ms au tour suivant.
    # Gain : ~400ms de latence sur le tour le plus critique de l'appel.
    # Mettre 0 pour désactiver (kill-switch sans redéploiement).
    vad_confirmation_silence_ms: int = _i("VAD_CONFIRMATION_SILENCE_MS", 500)

    # --- AGC (Auto-Gain Control) sur l'audio entrant ---
    # Au lieu d'un gain fixe (qui sature les voix fortes et sous-amplifie les
    # voix faibles), on ajuste dynamiquement le gain pour chaque chunk afin
    # d'atteindre target_rms. S'adapte automatiquement aux différentes voix
    # et téléphones — pas de calibration manuelle nécessaire.
    audio_target_rms: int = _i("AUDIO_TARGET_RMS", 6000)   # RMS cible (parole confortable)
    audio_max_gain: float = _f("AUDIO_MAX_GAIN", 20.0)     # cap pour éviter la saturation
    # Mode gain fixe LEGACY : si > 0, on bypass l'AGC et on applique ce gain.
    # Utile pour debug / A/B. Mettre 0 pour activer l'AGC normale.
    audio_input_gain: float = _f("AUDIO_INPUT_GAIN", 0.0)

    # --- Connexion OpenAI Realtime ---
    # Nombre total de tentatives de connexion WSS au début d'un appel.
    # Backoff exponentiel : 0.5s, 1s, 2s... Un blip réseau transitoire ne doit
    # pas faire perdre l'appel (le client entend juste 1-2s de silence en plus).
    openai_connect_attempts: int = _i("OPENAI_CONNECT_ATTEMPTS", 3)

    # --- Pool PostgreSQL ---
    # pool_size : connexions persistantes. max_overflow : connexions
    # supplémentaires temporaires en pic. pool_timeout : attente max d'une
    # connexion libre avant erreur (fail-fast plutôt que bloquer un appel).
    # Ignoré sous SQLite (tests).
    db_pool_size: int = _i("DB_POOL_SIZE", 10)
    db_max_overflow: int = _i("DB_MAX_OVERFLOW", 20)
    db_pool_timeout: int = _i("DB_POOL_TIMEOUT", 10)

    # --- Cache contexte d'appel (restaurant + menu) ---
    # Évite les SELECT restaurant/menu à chaque décrochage. TTL court : une
    # modification de menu est visible au plus tard après ce délai (et les
    # mutations API/dashboard invalident immédiatement). 0 = désactivé.
    restaurant_cache_ttl_seconds: int = _i("RESTAURANT_CACHE_TTL_SECONDS", 60)

    # --- Protection webhook Telnyx ---
    # Limite de requêtes POST /voice/incoming par IP source et par minute.
    # Protège la vérification Ed25519 + les lookups DB d'un flood. Telnyx
    # n'envoie qu'une poignée d'events par appel : 300/min/IP est très large.
    # 0 = désactivé.
    webhook_rate_limit_per_minute: int = _i("WEBHOOK_RATE_LIMIT_PER_MINUTE", 300)

    # --- Debug audio (optionnel) ---
    # Si défini, dump les 5 premières secondes d'audio entrant de chaque appel
    # dans ce dossier sous forme de .wav µ-law (lisible par QuickTime/VLC).
    # Permet de valider MANUELLEMENT que le bridge décode bien l'audio Telnyx.
    # ⚠ Laisser vide en prod (RGPD : ces fichiers contiennent la voix du client).
    audio_debug_dir: str | None = _s("AUDIO_DEBUG_DIR")

    # --- Observability (Sentry) ---
    # DSN Sentry pour capturer les exceptions en production. Format :
    # https://abc123@o12345.ingest.sentry.io/67890
    # Si vide, Sentry est désactivé (no-op).
    sentry_dsn: str | None = _s("SENTRY_DSN")

    # --- Billing ---
    # Coût estimé par minute d'appel (OpenAI Realtime + Telnyx voice + abonnement).
    # 0.40 € est une estimation conservatrice — ajuster selon les vrais coûts mesurés.
    billing_eur_per_minute: float = _f("BILLING_EUR_PER_MINUTE", 0.40)
    # Seuil d'alerte (log + Sentry) quand un restaurant dépasse N €/mois.
    billing_alert_eur: float = _f("BILLING_ALERT_EUR", 100.0)

    # --- RGPD ---
    # Durée de rétention des transcripts d'appel en jours. Au-delà, purge automatique
    # via scripts/purge_old_calls.py (à exécuter en cron quotidien).
    # 30 jours = recommandation CNIL pour des transcripts vocaux.
    rgpd_call_log_retention_days: int = _i("RGPD_CALL_LOG_RETENTION_DAYS", 30)

    @property
    def stream_wss_domain(self) -> str | None:
        """Extrait le domaine pur (sans scheme) pour construire wss://<domaine>/voice/media-stream."""
        d = self.voice_realtime_domain or self.backend_url
        return d.rstrip("/").replace("https://", "").replace("http://", "") if d else None

    @property
    def has_default_secrets(self) -> bool:
        """Détecte les secrets non changés : alerte au startup pour bloquer le déploiement."""
        return self.dashboard_secret == "change-me-in-production" or self.dashboard_password == "mia-admin"

    @property
    def looks_like_production(self) -> bool:
        """Heuristique : si BACKEND_URL est public (https + pas localhost/ngrok), on est en prod."""
        url = (self.backend_url or "").lower()
        return ("localhost" not in url and "127.0.0.1" not in url
                and "ngrok" not in url and url.startswith("https://"))


# Singleton global utilisé partout dans l'app via `from app.config import settings`.
settings = Settings()
