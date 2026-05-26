"""Envoi de SMS multi-provider.

Routage automatique selon le pays du destinataire :
  - +262 (La Réunion / Mayotte) → Brevo (Telnyx ne couvre pas la zone)
  - Tous les autres             → Telnyx

Pourquoi Brevo (et plus OVH) : OVH refuse les souscriptions au service SMS
depuis les DOM. Brevo (ex-Sendinblue, FR) accepte tout client et livre vers
+262. API simple : 1 seule clé (header `api-key`).

Tous les échecs sont SILENCIEUX (retour False, log warning) : un SMS qui
ne part pas ne doit jamais faire échouer la réservation/commande sous-jacente.
"""
import json
import logging
import urllib.error
import urllib.request

from app.config import settings
from app.utils.phone import is_reunion_mayotte, to_e164

log = logging.getLogger("mia.sms")

BREVO_SMS_ENDPOINT = "https://api.brevo.com/v3/transactionalSMS/sms"


def _mask(phone: str) -> str:
    """Masque un numéro pour les logs (RGPD) : +33612***78."""
    return phone[:5] + "***" + phone[-2:] if len(phone) > 7 else "***"


def send_sms(to: str, body: str) -> bool:
    """Point d'entrée unique pour envoyer un SMS.

    Returns:
        True si l'envoi a réussi, False sinon (config manquante, numéro
        invalide, erreur HTTP, timeout). Les échecs ne lèvent jamais.
    """
    to_num = to_e164(to)
    if not to_num:
        log.warning("SMS non envoyé : numéro invalide (%r)", to)
        return False

    # Routage : +262 → Brevo (Telnyx ne couvre pas), sinon Telnyx.
    if is_reunion_mayotte(to_num):
        return _send_via_brevo(to_num, body)
    return _send_via_telnyx(to_num, body)


# ─────────────────────────────────────────────────────
# Provider 1 : Telnyx (numéros métropole + international)
# ─────────────────────────────────────────────────────


def _send_via_telnyx(to_e164_num: str, body: str) -> bool:
    """Envoie via l'API Telnyx Messaging.

    Le numéro expéditeur (settings.telnyx_phone_number) doit être un numéro
    Telnyx provisionné pour la messagerie sortante.
    """
    api_key = (settings.telnyx_api_key or "").strip()
    from_num = to_e164(settings.telnyx_phone_number)

    if not api_key or not from_num:
        log.warning("SMS Telnyx non envoyé : config manquante (api_key/from_num)")
        return False

    try:
        req = urllib.request.Request(
            "https://api.telnyx.com/v2/messages",
            data=json.dumps({"from": from_num, "to": to_e164_num, "text": body}).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        log.info("SMS Telnyx envoyé à %s (id=%s)", _mask(to_e164_num),
                 data.get("data", {}).get("id", "?"))
        return True
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()[:200] if e.fp else ""
        log.warning("Telnyx SMS échec %d : %s", e.code, body_err)
        return False
    except Exception as e:
        log.exception("Erreur Telnyx SMS : %s", e)
        return False


# ─────────────────────────────────────────────────────
# Provider 2 : Brevo (fallback pour +262)
# ─────────────────────────────────────────────────────


def _send_via_brevo(to_e164_num: str, body: str) -> bool:
    """Envoie via l'API Brevo Transactional SMS.

    Auth : 1 seul header `api-key` (clé v3 commençant par `xkeysib-...`).
    Le sender (settings.brevo_sender) doit être ≤ 11 caractères alphanumériques
    ET avoir été validé dans le dashboard Brevo (sinon Brevo rejette avec 400).

    Doc : https://developers.brevo.com/reference/sendtransacsms
    """
    api_key = (settings.brevo_api_key or "").strip()
    sender = (settings.brevo_sender or "MIA").strip()

    if not api_key:
        log.warning("SMS Brevo non envoyé à %s : BREVO_API_KEY non configurée.",
                    _mask(to_e164_num))
        return False

    payload = json.dumps({
        "type": "transactional",     # vs "marketing" — exempte du STOP obligatoire
        "unicodeEnabled": False,     # GSM-7 — 160 chars max, plus économique
        "sender": sender,
        "recipient": to_e164_num,
        "content": body,
        "tag": "mia",                # tag pour reporting dans le dashboard Brevo
    })

    try:
        req = urllib.request.Request(
            BREVO_SMS_ENDPOINT,
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "api-key": api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())

        # Brevo renvoie { reference, messageId, smsCount, usedCredits, remainingCredits }
        log.info(
            "SMS Brevo envoyé à %s (id=%s, credits restants=%s)",
            _mask(to_e164_num), data.get("messageId"), data.get("remainingCredits"),
        )
        return True
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()[:300] if e.fp else ""
        log.warning("Brevo SMS échec %d : %s", e.code, body_err)
        return False
    except Exception as e:
        log.exception("Erreur Brevo SMS : %s", e)
        return False
