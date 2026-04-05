"""
Configuration MIA — chargée depuis les variables d'environnement (.env).

Les valeurs sensibles (clés API, mots de passe) DOIVENT être définies
dans .env en production (voir .env.example). Ne jamais déployer avec
les valeurs par défaut.
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Paramètres chargés depuis .env avec fallback sur les défauts."""

    # --- Base de données ---
    database_url: str = "postgresql://localhost:5432/mia"

    # --- APIs externes ---
    openai_api_key: str | None = None
    telnyx_api_key: str | None = None
    telnyx_phone_number: str | None = None

    # --- Sécurité API ---
    # Protège /restaurants, /menu, /reservations, /commandes via header X-API-Key.
    # Vide = pas de protection (acceptable en dev local uniquement).
    mia_api_key: str | None = None

    # --- Sécurité Dashboard ---
    # Mot de passe unique pour accéder au dashboard admin (/dashboard).
    dashboard_password: str = "mia-admin"
    # Secret HMAC-SHA256 pour signer les cookies de session.
    # DOIT être >= 32 caractères aléatoires en production.
    dashboard_secret: str = "change-me-in-production"

    # --- Réseau ---
    backend_url: str = "http://localhost:8000"
    # Si défini, utilisé à la place de backend_url pour le WebSocket média.
    voice_realtime_domain: str | None = None

    # --- Voix OpenAI Realtime ---
    voice_realtime_voice: str = "coral"

    # --- VAD (Voice Activity Detection) ---
    # Contrôle la sensibilité de détection parole/silence côté OpenAI.
    vad_threshold: float = 0.8
    vad_prefix_padding_ms: int = 500
    vad_silence_duration_ms: int = 1000
    # Délai avant de couper la réponse MIA quand le client interrompt.
    vad_barge_in_delay_ms: int = 800
    # Seuils minimaux pour considérer qu'une transcription est valide
    # (filtre le bruit ambiant, les hésitations, etc.).
    vad_min_transcript_chars: int = 3
    vad_min_transcript_words: int = 2

    @property
    def stream_wss_domain(self) -> str | None:
        """Domaine WSS pour le stream média Telnyx → notre serveur."""
        d = self.voice_realtime_domain or self.backend_url
        if d:
            return d.rstrip("/").replace("https://", "").replace("http://", "")
        return None

    @property
    def has_default_secrets(self) -> bool:
        """True si les secrets dashboard n'ont pas été changés — dangereux en prod."""
        return (
            self.dashboard_secret == "change-me-in-production"
            or self.dashboard_password == "mia-admin"
        )

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
