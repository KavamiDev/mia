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

    # --- OVH SMS (fallback pour +262 La Réunion / Mayotte) ---
    # Telnyx ne livre pas de SMS vers les DOM Océan Indien. On bascule sur
    # OVH SMS pour ces numéros. Si non configuré, les SMS +262 échouent
    # silencieusement (log warning, pas de crash).
    # Setup OVH : https://eu.api.ovh.com/createToken/ (droits POST /sms/*/jobs)
    ovh_application_key: str | None = _s("OVH_APPLICATION_KEY")
    ovh_application_secret: str | None = _s("OVH_APPLICATION_SECRET")
    ovh_consumer_key: str | None = _s("OVH_CONSUMER_KEY")
    ovh_sms_account: str | None = _s("OVH_SMS_ACCOUNT")        # ex: "sms-cs12345-1"
    ovh_sms_sender: str = _s("OVH_SMS_SENDER", "MIA")          # ≤ 11 chars alphanumériques

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
    # VAD = Voice Activity Detection. Contrôle quand OpenAI considère
    # que l'utilisateur a fini de parler et déclenche une réponse.
    #
    # Calibration latence (goal : <1.5 s perçue) :
    #   silence_duration_ms + inference OpenAI (~400ms) + réseau (~150ms) = latence totale
    #   À 700ms → ~1.25s perçue. Acceptable pour téléphone.
    #   À 1500ms → ~2.05s, perçu comme "long" par l'appelant.
    # Si MIA coupe trop tôt en cours de phrase, monter à 900-1100ms.
    vad_threshold: float = _f("VAD_THRESHOLD", 0.6)              # 0..1, sensibilité (compromis téléphonique/bruit)
    vad_prefix_padding_ms: int = _i("VAD_PREFIX_PADDING_MS", 300) # Court → meilleure réactivité
    vad_silence_duration_ms: int = _i("VAD_SILENCE_DURATION_MS", 700)   # Silence min — calibré pour ~1.25s latence

    # --- Debug audio (optionnel) ---
    # Si défini, dump les 5 premières secondes d'audio entrant de chaque appel
    # dans ce dossier sous forme de .wav µ-law (lisible par QuickTime/VLC).
    # Permet de valider MANUELLEMENT que le bridge décode bien l'audio Telnyx.
    # ⚠ Laisser vide en prod (RGPD : ces fichiers contiennent la voix du client).
    audio_debug_dir: str | None = _s("AUDIO_DEBUG_DIR")

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
