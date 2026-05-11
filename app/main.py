"""Point d'entrée FastAPI de MIA.

Routes :
  /voice/*     → Webhooks Telnyx (appels entrants + WS media-stream)
  /dashboard/* → Admin Jinja2 protégé par mot de passe
  /restaurants /menu /reservations /commandes → API REST (X-API-Key)
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import commandes, dashboard, menu, reservations, restaurants, voice_webhook

logging.basicConfig(format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO)
log = logging.getLogger("mia")

app = FastAPI(title="MIA", version="1.0.0", redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

app.include_router(dashboard.router)
app.include_router(restaurants.router)
app.include_router(reservations.router)
app.include_router(commandes.router)
app.include_router(menu.router)
app.include_router(voice_webhook.router, prefix="/voice")


@app.on_event("startup")
def _check_config():
    if settings.has_default_secrets:
        log.warning("SECURITE : DASHBOARD_PASSWORD/SECRET par défaut — changez-les en .env avant déploiement.")
    if not settings.mia_api_key:
        log.warning("SECURITE : MIA_API_KEY non définie — API REST sans authentification.")
    if not settings.openai_api_key:
        log.warning("OPENAI_API_KEY manquante — les appels échoueront.")
    if not settings.telnyx_api_key:
        log.warning("TELNYX_API_KEY manquante — appels et SMS échoueront.")


@app.get("/")
def root():
    return {"status": "ok", "app": "MIA"}
