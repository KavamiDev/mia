"""Point d'entrée FastAPI de MIA.

Organisation des routes :
  /voice/*     → Webhooks Telnyx (appels entrants + WS media-stream)
  /dashboard/* → Admin Jinja2 (multi-tenant : email + password)
  /restaurants /menu /reservations /commandes → API REST (protégée X-API-Key)
  /health      → Endpoint santé détaillé (DB, secrets, providers)

Au démarrage on :
  1. Crée les tables manquantes (Base.metadata.create_all — idempotent)
  2. Crée un superadmin auto si la table users est vide
  3. Init Sentry conditionnellement (SENTRY_DSN env var)
  4. Vérifie/durcit les configs de sécurité
"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.routers import (billing, commandes, dashboard, health, menu,
                         reservations, restaurants, voice_webhook)

logging.basicConfig(format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", level=logging.INFO)
log = logging.getLogger("mia")


# ──────────────────────────────────────
# Sentry (observability) — init AVANT la création de l'app FastAPI
# ──────────────────────────────────────
# Si SENTRY_DSN défini, on capture les exceptions + métriques. Sinon no-op.
if settings.sentry_dsn:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
            traces_sample_rate=0.1,            # 10 % des transactions tracées (coût raisonnable)
            send_default_pii=False,            # RGPD : pas d'IP/email dans Sentry
            environment="production" if settings.looks_like_production else "dev",
            release="mia@1.0.0",
        )
        log.info("Sentry initialisé (env=%s)",
                 "production" if settings.looks_like_production else "dev")
    except ImportError:
        log.warning("SENTRY_DSN défini mais sentry-sdk non installé. "
                    "pip install sentry-sdk[fastapi]")


app = FastAPI(title="MIA", version="1.0.0", redoc_url=None)

# CORS : ouvert pour l'API REST (consommée par scripts externes).
# allow_credentials=False car on utilise X-API-Key (pas de cookies),
# donc allow_origins="*" reste sûr. Le dashboard est servi en same-origin.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False,
                   allow_methods=["*"], allow_headers=["*"])

# Ordre des routers : dashboard d'abord (UI), puis API REST, puis webhooks.
app.include_router(dashboard.router)
app.include_router(billing.router)
app.include_router(restaurants.router)
app.include_router(reservations.router)
app.include_router(commandes.router)
app.include_router(menu.router)
app.include_router(voice_webhook.router, prefix="/voice")
app.include_router(health.router)


def _bootstrap_admin_user() -> None:
    """Crée un superadmin si la table users est vide.

    Email : admin@mia.local
    Password : settings.dashboard_password (DASHBOARD_PASSWORD env)

    Permet de se connecter immédiatement après le 1er déploiement sans avoir
    à insérer manuellement une ligne en SQL.
    """
    from app.models import User
    from app.utils.auth_hash import hash_password

    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return  # déjà initialisé
        admin = User(
            email="admin@mia.local",
            password_hash=hash_password(settings.dashboard_password),
            restaurant_id=None,
            is_admin=True,
        )
        db.add(admin)
        db.commit()
        log.info("Superadmin créé : admin@mia.local (password = DASHBOARD_PASSWORD)")
    except Exception as e:
        db.rollback()
        log.warning("Bootstrap superadmin échoué : %s", e)
    finally:
        db.close()


@app.on_event("startup")
def _check_config():
    """Vérifie les configurations au démarrage + bootstrap initial.

    En production (BACKEND_URL https public), un secret par défaut bloque
    le démarrage — fail-fast plutôt que de tourner avec une faille béante.
    En dev/staging, on log un warning.

    Crée aussi les tables manquantes (idempotent : ne touche que les nouvelles).
    """
    # Import models pour que Base.metadata.tables contienne tout avant create_all.
    from app import models  # noqa: F401
    try:
        Base.metadata.create_all(engine)
    except Exception as e:
        log.warning("Création tables auto échouée (ignorable si DB déjà OK) : %s", e)

    # Bootstrap superadmin si table users vide
    try:
        _bootstrap_admin_user()
    except Exception as e:
        log.warning("Bootstrap admin échoué : %s", e)

    is_prod = settings.looks_like_production

    if settings.has_default_secrets:
        msg = "SECURITE : DASHBOARD_PASSWORD/SECRET par défaut détectés."
        if is_prod:
            raise RuntimeError(
                f"{msg} Refus de démarrer en production. "
                "Définis DASHBOARD_PASSWORD et DASHBOARD_SECRET dans .env."
            )
        log.warning("%s Changez-les avant déploiement.", msg)

    if not settings.mia_api_key:
        msg = "SECURITE : MIA_API_KEY non définie — API REST sans authentification."
        if is_prod:
            raise RuntimeError(f"{msg} Refus de démarrer en production.")
        log.warning(msg)

    if is_prod and not settings.telnyx_public_key:
        log.warning(
            "SECURITE : TELNYX_PUBLIC_KEY non définie en prod — les webhooks "
            "Telnyx sont acceptés sans vérification de signature."
        )
    if not settings.openai_api_key:
        log.warning("OPENAI_API_KEY manquante — les appels échoueront.")
    if not settings.telnyx_api_key:
        log.warning("TELNYX_API_KEY manquante — appels et SMS échoueront.")
    if is_prod and not settings.sentry_dsn:
        log.warning("Observability : SENTRY_DSN non défini en prod — pas d'alerting auto.")


@app.get("/")
def root():
    """Endpoint racine pour les health checks externes (Render, fly.io, etc.)."""
    return {"status": "ok", "app": "MIA"}
