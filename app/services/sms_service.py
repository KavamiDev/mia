"""Envoi de SMS via Telnyx Messaging API.

Une seule fonction publique : send_sms(to, body).
Les templates de message (réservation, commande) sont construits côté appelant
dans tool_service.py — pas besoin d'abstractions intermédiaires.
"""
import json
import logging
import urllib.error
import urllib.request

from app.config import settings
from app.utils.phone import to_e164

log = logging.getLogger("mia.sms")


def _mask(phone: str) -> str:
    """Masque un numéro pour les logs (RGPD) : +33692XX***XX."""
    return phone[:4] + "***" + phone[-2:] if len(phone) > 6 else "***"


def send_sms(to: str, body: str) -> bool:
    """Envoie un SMS via Telnyx. Retourne True si envoyé, False sinon.

    Échecs silencieux : un SMS raté ne doit JAMAIS faire échouer la
    création de la réservation/commande sous-jacente. On log et on continue.

    Les numéros sont normalisés en E.164 (+33...) — Telnyx exige ce format.
    """
    api_key = (settings.telnyx_api_key or "").strip()
    from_num = to_e164(settings.telnyx_phone_number)
    to_num = to_e164(to)

    # Garde-fou : sans config ou avec un numéro destinataire invalide, on abandonne.
    if not api_key or not from_num or not to_num:
        log.warning("SMS non envoyé (config ou numéro invalide)")
        return False

    try:
        # Appel HTTP direct via urllib pour éviter une dépendance supplémentaire.
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
        # Erreur Telnyx (4xx/5xx) : on log le corps tronqué pour debug.
        log.warning("Telnyx SMS échec %d : %s", e.code, (e.read().decode()[:200] if e.fp else ""))
        return False
    except Exception as e:
        # Réseau, timeout, etc. : on capture tout pour ne pas crasher l'appel.
        log.exception("Erreur envoi SMS : %s", e)
        return False
