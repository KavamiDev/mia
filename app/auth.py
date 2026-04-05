"""
Authentification API par clé (header X-API-Key).

Protège les routes de gestion (/restaurants, /menu, /reservations, /commandes).
Les webhooks Telnyx (/voice/*) NE passent PAS par ce garde.

Sécurité :
  - Comparaison timing-safe (hmac.compare_digest) pour éviter les
    attaques par analyse temporelle.
  - Si MIA_API_KEY n'est pas définie dans .env, la protection est
    désactivée (dev local uniquement).
"""
import hmac

from fastapi import HTTPException, Request

from app.config import settings


async def require_api_key(request: Request) -> None:
    """Dépendance FastAPI : rejette 401 si la clé API est absente ou invalide."""
    expected = (settings.mia_api_key or "").strip()
    if not expected:
        return
    got = request.headers.get("X-API-Key", "").strip()
    if not got or not hmac.compare_digest(got.encode(), expected.encode()):
        raise HTTPException(
            status_code=401,
            detail="Clé API invalide ou absente (header X-API-Key)",
        )
