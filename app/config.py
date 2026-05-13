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
    vad_threshold: float = _f("VAD_THRESHOLD", 0.5)              # 0..1, sensibilité (bas = capte mieux la parole faible/téléphonique)
    vad_prefix_padding_ms: int = _i("VAD_PREFIX_PADDING_MS", 500) # Audio pré-speech
    vad_silence_duration_ms: int = _i("VAD_SILENCE_DURATION_MS", 1000)  # Silence min

    @property
    def stream_wss_domain(self) -> str | None:
        """Extrait le domaine pur (sans scheme) pour construire wss://<domaine>/voice/media-stream."""
        d = self.voice_realtime_domain or self.backend_url
        return d.rstrip("/").replace("https://", "").replace("http://", "") if d else None

    @property
    def has_default_secrets(self) -> bool:
        """Détecte les secrets non changés : alerte au startup pour bloquer le déploiement."""
        return self.dashboard_secret == "change-me-in-production" or self.dashboard_password == "mia-admin"


# Singleton global utilisé partout dans l'app via `from app.config import settings`.
settings = Settings()
