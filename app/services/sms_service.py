"""Envoi de SMS via Telnyx Messaging API."""
import json
import logging
import urllib.error
import urllib.request

from app.config import settings
from app.utils.phone import to_e164

log = logging.getLogger("mia.sms")


def _mask(phone: str) -> str:
    return phone[:4] + "***" + phone[-2:] if len(phone) > 6 else "***"


def send_sms(to: str, body: str) -> bool:
    """Envoie un SMS via Telnyx. Retourne True si envoyé avec succès."""
    api_key = (settings.telnyx_api_key or "").strip()
    from_num = to_e164(settings.telnyx_phone_number)
    to_num = to_e164(to)
    if not api_key or not from_num or not to_num:
        log.warning("SMS non envoyé (config ou numéro invalide)")
        return False
    try:
        req = urllib.request.Request(
            "https://api.telnyx.com/v2/messages",
            data=json.dumps({"from": from_num, "to": to_num, "text": body}).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        log.info("SMS envoyé à %s (id=%s)", _mask(to_num), data.get("data", {}).get("id", "?"))
        return True
    except urllib.error.HTTPError as e:
        log.warning("Telnyx SMS échec %d : %s", e.code, (e.read().decode()[:200] if e.fp else ""))
        return False
    except Exception as e:
        log.exception("Erreur envoi SMS : %s", e)
        return False
