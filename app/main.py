"""
Point d'entrée FastAPI de MIA — assistante téléphonique pour restaurants.

Architecture des routes :
  /voice/*          → Webhooks Telnyx (appels entrants, WebSocket média)
  /dashboard/*      → Interface admin protégée par mot de passe (Jinja2 + Tailwind)
  /restaurants, /menu, /reservations, /commandes
                    → API REST protégée par X-API-Key
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import commandes, dashboard, menu, reservations, restaurants, voice_webhook

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("mia")

app = FastAPI(
    title="MIA - Assistante téléphonique restaurant",
    version="1.0.0",
    redoc_url=None,
)

# CORS : autorise les appels cross-origin pour l'API REST.
# allow_credentials=False car l'API utilise X-API-Key (pas de cookies).
# Le dashboard est servi en same-origin, donc non affecté par CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routers ---
app.include_router(dashboard.router)
app.include_router(restaurants.router)
app.include_router(reservations.router)
app.include_router(commandes.router)
app.include_router(menu.router)
app.include_router(voice_webhook.router, prefix="/voice")


@app.on_event("startup")
def _check_configuration():
    """Vérifie la configuration au démarrage et alerte sur les risques."""
    if settings.has_default_secrets:
        log.warning(
            "SECURITE : DASHBOARD_PASSWORD et/ou DASHBOARD_SECRET utilisent "
            "les valeurs par défaut. Changez-les dans .env avant tout déploiement."
        )
    if not settings.mia_api_key:
        log.warning(
            "SECURITE : MIA_API_KEY non définie — les routes de gestion "
            "sont accessibles sans authentification."
        )
    if not settings.openai_api_key:
        log.warning("OPENAI_API_KEY non configurée — les appels vocaux échoueront.")
    if not settings.telnyx_api_key:
        log.warning("TELNYX_API_KEY non configurée — les appels et SMS échoueront.")


@app.get("/")
def root():
    return {"status": "ok", "app": "MIA"}


@app.get("/health")
def health():
    return {"status": "healthy"}
