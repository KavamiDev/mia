"""Point d'entrée FastAPI de MIA.

Organisation des routes :
  /voice/*     → Webhooks Telnyx (appels entrants + WS media-stream)
  /dashboard/* → Admin Jinja2 protégé par mot de passe (cookie HMAC)
  /restaurants /menu /reservations /commandes → API REST (protégée X-API-Key)

Au démarrage, on log des warnings de sécurité si :
  - les secrets dashboard sont restés sur les valeurs par défaut,
  - la clé API REST n'est pas définie (API ouverte),
  - les clés OpenAI/Telnyx manquent (l'appel échouera silencieusement).
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import commandes, dashboard, menu, reservations, restaurants, voice_webhook

logging.basicConfig(format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO)
log = logging.getLogger("mia")

app = FastAPI(title="MIA", version="1.0.0", redoc_url=None)

# CORS : ouvert pour l'API REST (consommée par scripts externes).
# allow_credentials=False car on utilise X-API-Key (pas de cookies),
# donc allow_origins="*" reste sûr. Le dashboard est servi en same-origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

# Ordre des routers : dashboard d'abord (UI), puis API REST, puis webhooks.
app.include_router(dashboard.router)
app.include_router(restaurants.router)
app.include_router(reservations.router)
app.include_router(commandes.router)
app.include_router(menu.router)
app.include_router(voice_webhook.router, prefix="/voice")


@app.on_event("startup")
def _check_config():
    """Alerte sur les configurations dangereuses au démarrage.

    Ces warnings ne BLOQUENT PAS le démarrage — c'est volontaire pour
    permettre un setup progressif (dev → staging → prod). Mais ils
    apparaissent dans les logs et doivent être traités avant prod.
    """
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
    """Endpoint racine pour les health checks externes (Render, fly.io, etc.)."""
    return {"status": "ok", "app": "MIA"}
