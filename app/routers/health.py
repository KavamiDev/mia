"""Endpoint santé détaillé /health.

Renvoie un JSON avec l'état de chaque dépendance critique :
  - DB : connexion + ping SELECT 1
  - Vars critiques : OPENAI_API_KEY, TELNYX_API_KEY présents
  - Optionnel : OVH_* présents, SENTRY_DSN configuré

Pas d'auth requise — destiné aux uptime monitors (UptimeRobot, healthchecks.io).
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(db: Session = Depends(get_db)):
    """Retourne 200 si tout va bien, 503 si une dépendance critique est KO.

    Le format est lisible humainement (JSON structuré) pour debug rapide :
        {
          "status": "ok" | "degraded" | "down",
          "checks": { "db": "ok", "openai_key": "ok", ... }
        }
    """
    checks: dict[str, str] = {}

    # 1. DB ping
    try:
        db.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as e:
        checks["db"] = f"down: {type(e).__name__}"

    # 2. Variables critiques (présence — on ne révèle pas la valeur)
    checks["openai_key"] = "ok" if settings.openai_api_key else "missing"
    checks["telnyx_key"] = "ok" if settings.telnyx_api_key else "missing"
    checks["telnyx_phone"] = "ok" if settings.telnyx_phone_number else "missing"

    # 3. Secrets dashboard (en prod : doivent être custom)
    if settings.looks_like_production:
        checks["dashboard_secrets"] = (
            "default!" if settings.has_default_secrets else "ok"
        )
        checks["telnyx_pubkey"] = "ok" if settings.telnyx_public_key else "missing"
        checks["sentry"] = "ok" if settings.sentry_dsn else "missing"

    # 4. OVH SMS (optionnel — info uniquement)
    ovh_keys = [settings.ovh_application_key, settings.ovh_application_secret,
                settings.ovh_consumer_key, settings.ovh_sms_account]
    if all(ovh_keys):
        checks["ovh_sms"] = "ok"
    elif any(ovh_keys):
        checks["ovh_sms"] = "partial"
    else:
        checks["ovh_sms"] = "not_configured"

    # Status global : down si DB ou vars critiques KO, degraded si secret par défaut.
    critical_keys = ["db", "openai_key", "telnyx_key"]
    has_critical_failure = any(not checks[k].startswith("ok") for k in critical_keys)
    has_default_secret = checks.get("dashboard_secrets") == "default!"

    if has_critical_failure:
        status = "down"
        http_code = 503
    elif has_default_secret:
        status = "degraded"
        http_code = 200
    else:
        status = "ok"
        http_code = 200

    return JSONResponse(content={"status": status, "checks": checks}, status_code=http_code)
