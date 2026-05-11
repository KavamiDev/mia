"""Configuration MIA — chargée depuis les variables d'environnement (.env)."""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _s(k, d=None): v = os.getenv(k, d); return v.strip() if isinstance(v, str) else v
def _f(k, d):
    try: return float(os.getenv(k) or d)
    except ValueError: return d
def _i(k, d):
    try: return int(os.getenv(k) or d)
    except ValueError: return d


@dataclass(frozen=True)
class Settings:
    database_url: str = _s("DATABASE_URL", "postgresql://localhost:5432/mia")
    openai_api_key: str | None = _s("OPENAI_API_KEY")
    telnyx_api_key: str | None = _s("TELNYX_API_KEY")
    telnyx_phone_number: str | None = _s("TELNYX_PHONE_NUMBER")
    mia_api_key: str | None = _s("MIA_API_KEY")
    dashboard_password: str = _s("DASHBOARD_PASSWORD", "mia-admin")
    dashboard_secret: str = _s("DASHBOARD_SECRET", "change-me-in-production")
    backend_url: str = _s("BACKEND_URL", "http://localhost:8000")
    voice_realtime_domain: str | None = _s("VOICE_REALTIME_DOMAIN")
    voice_realtime_voice: str = _s("VOICE_REALTIME_VOICE", "coral")
    vad_threshold: float = _f("VAD_THRESHOLD", 0.8)
    vad_prefix_padding_ms: int = _i("VAD_PREFIX_PADDING_MS", 500)
    vad_silence_duration_ms: int = _i("VAD_SILENCE_DURATION_MS", 1000)

    @property
    def stream_wss_domain(self) -> str | None:
        d = self.voice_realtime_domain or self.backend_url
        return d.rstrip("/").replace("https://", "").replace("http://", "") if d else None

    @property
    def has_default_secrets(self) -> bool:
        return self.dashboard_secret == "change-me-in-production" or self.dashboard_password == "mia-admin"


settings = Settings()
