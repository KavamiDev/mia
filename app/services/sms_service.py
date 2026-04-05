"""
Envoi de SMS via Telnyx Messaging API.

Deux types de notifications :
  1. SMS au restaurateur  — détails complets (code, items, téléphone client)
  2. SMS au client        — confirmation avec code et récapitulatif

Les numéros sont convertis en E.164 avant envoi (requis par Telnyx).
"""
import json
import logging
import urllib.error
import urllib.request

from app.config import settings
from app.utils.phone import to_e164

log = logging.getLogger("mia.sms")


def _mask_phone(phone: str) -> str:
    """Masque partiellement un numéro pour les logs (RGPD)."""
    if len(phone) > 6:
        return phone[:4] + "***" + phone[-2:]
    return "***"


def send_sms(to: str, body: str) -> bool:
    """Envoie un SMS via Telnyx. Retourne True si envoyé avec succès."""
    api_key = (settings.telnyx_api_key or "").strip()
    from_num = to_e164(settings.telnyx_phone_number) or str(settings.telnyx_phone_number or "").replace(" ", "")
    if not api_key or not from_num:
        log.warning("Telnyx non configuré, SMS non envoyé")
        return False

    to_num = to_e164(to)
    if not to_num:
        log.warning("SMS non envoyé : numéro destinataire invalide (to=%r)", _mask_phone(to or ""))
        return False

    try:
        payload = json.dumps({"from": from_num, "to": to_num, "text": body}).encode()
        req = urllib.request.Request(
            "https://api.telnyx.com/v2/messages",
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        log.info("SMS envoyé à %s (id=%s)", _mask_phone(to_num), data.get("data", {}).get("id", "?"))
        return True
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()[:200] if e.fp else ""
        log.warning("Telnyx SMS échec %d : %s", e.code, body_err)
        return False
    except Exception as e:
        log.exception("Erreur envoi SMS : %s", e)
        return False


# ──────────────────────────────────────
# Notifications restaurateur
# ──────────────────────────────────────

def notify_restaurant_reservation(
    restaurant_phone: str,
    code: str,
    personnes: int,
    date: str,
    heure: str,
    telephone: str,
) -> bool:
    """SMS au restaurateur avec les détails de la réservation."""
    body = (
        f"Réservation {code}\n"
        f"{personnes} personnes\n"
        f"{heure} le {date}\n"
        f"Tel : {telephone}"
    )
    return send_sms(restaurant_phone, body)


def notify_restaurant_commande(
    restaurant_phone: str,
    code: str,
    items_recap: str,
    prix_total: float,
    telephone: str,
) -> bool:
    """SMS au restaurateur avec les détails de la commande."""
    body = (
        f"Commande {code}\n"
        f"{items_recap}\n"
        f"Total : {prix_total:.2f}€\n"
        f"Tel : {telephone}"
    )
    return send_sms(restaurant_phone, body)


# ──────────────────────────────────────
# Confirmations client
# ──────────────────────────────────────

def notify_client_reservation(
    client_phone: str,
    code: str,
    personnes: int,
    date: str,
    heure: str,
    restaurant_name: str = "",
) -> bool:
    """SMS de confirmation au client après réservation."""
    resto = f" chez {restaurant_name}" if restaurant_name else ""
    body = (
        f"Réservation {code} confirmée{resto}\n"
        f"{personnes} pers. le {date} à {heure}\n"
        f"À bientôt !"
    )
    return send_sms(client_phone, body)


def notify_client_commande(
    client_phone: str,
    code: str,
    items_recap: str,
    prix_total: float,
    restaurant_name: str = "",
) -> bool:
    """SMS de confirmation au client après commande."""
    resto = f" chez {restaurant_name}" if restaurant_name else ""
    body = (
        f"Commande {code} confirmée{resto}\n"
        f"{items_recap}\n"
        f"Total : {prix_total:.2f}€\n"
        f"À récupérer au restaurant !"
    )
    return send_sms(client_phone, body)
