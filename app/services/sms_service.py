"""Envoi de SMS multi-provider.

Routage automatique selon le pays du destinataire :
  - +262 (La Réunion / Mayotte) → OVH SMS
  - Tous les autres             → Telnyx

Pourquoi : Telnyx ne livre pas de SMS vers la zone +262 (DOM Océan Indien).
OVH SMS prend le relais avec une API simple (HMAC-SHA1 + JSON).

Tous les échecs sont SILENCIEUX (retour False, log warning) : un SMS qui
ne part pas ne doit jamais faire échouer la réservation/commande sous-jacente.
"""
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request

from app.config import settings
from app.utils.phone import is_reunion_mayotte, to_e164

log = logging.getLogger("mia.sms")

OVH_API_BASE = "https://eu.api.ovh.com/1.0"


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

    # Routage : +262 → OVH (Telnyx ne couvre pas), sinon Telnyx.
    if is_reunion_mayotte(to_num):
        return _send_via_ovh(to_num, body)
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
        # Erreur 4xx/5xx : on log le corps tronqué pour debug.
        body_err = e.read().decode()[:200] if e.fp else ""
        log.warning("Telnyx SMS échec %d : %s", e.code, body_err)
        return False
    except Exception as e:
        # Réseau, timeout, etc. — capture tout pour ne pas crasher l'appel.
        log.exception("Erreur Telnyx SMS : %s", e)
        return False


# ─────────────────────────────────────────────────────
# Provider 2 : OVH SMS (fallback pour +262)
# ─────────────────────────────────────────────────────


def _ovh_sign(method: str, url: str, body: str, timestamp: str,
              app_secret: str, consumer_key: str) -> str:
    """Calcule la signature OVH conforme à leur spec.

    Format : "$1$" + sha1_hex(<app_secret>+<consumer_key>+<method>+<url>+<body>+<ts>)

    Le séparateur est '+' (caractère plus, pas concaténation). L'URL inclut
    le scheme et le path complet. Le timestamp est un epoch unix en secondes.
    """
    msg = f"{app_secret}+{consumer_key}+{method}+{url}+{body}+{timestamp}"
    return "$1$" + hashlib.sha1(msg.encode("utf-8")).hexdigest()


def _send_via_ovh(to_e164_num: str, body: str) -> bool:
    """Envoie via l'API OVH SMS.

    Auth OVH = 4 valeurs :
      application_key    : identifie l'app cliente
      application_secret : signe les requêtes
      consumer_key       : représente l'utilisateur OVH ayant donné consentement
      sms_account        : ex "sms-cs12345-1" (visible dans le manager OVH)

    Le sender (settings.ovh_sms_sender, défaut "MIA") doit être ≤ 11 caractères
    alphanumériques ET avoir été validé dans le manager OVH avant utilisation.
    """
    app_key = (settings.ovh_application_key or "").strip()
    app_secret = (settings.ovh_application_secret or "").strip()
    consumer_key = (settings.ovh_consumer_key or "").strip()
    sms_account = (settings.ovh_sms_account or "").strip()
    sender = (settings.ovh_sms_sender or "MIA").strip()

    if not all([app_key, app_secret, consumer_key, sms_account]):
        log.warning("SMS OVH non envoyé à %s : config OVH incomplète. "
                    "Définir OVH_APPLICATION_KEY/SECRET/CONSUMER_KEY/SMS_ACCOUNT.",
                    _mask(to_e164_num))
        return False

    url = f"{OVH_API_BASE}/sms/{sms_account}/jobs"
    payload = json.dumps({
        "message": body,
        "receivers": [to_e164_num],
        "sender": sender,
        "noStopClause": True,        # SMS transactionnel (pas marketing)
        "priority": "high",
        "validityPeriod": 2880,      # 48h — au-delà OVH abandonne
        "charset": "UTF-8",
        "coding": "7bit",
    })
    timestamp = str(int(time.time()))
    signature = _ovh_sign("POST", url, payload, timestamp, app_secret, consumer_key)

    try:
        req = urllib.request.Request(
            url,
            data=payload.encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Ovh-Application": app_key,
                "X-Ovh-Consumer": consumer_key,
                "X-Ovh-Signature": signature,
                "X-Ovh-Timestamp": timestamp,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())

        # OVH renvoie { ids: [...], validReceivers: [...], invalidReceivers: [...] }
        # Un job créé ne garantit pas la livraison, juste l'acceptation par OVH.
        valid = data.get("validReceivers", [])
        invalid = data.get("invalidReceivers", [])
        if to_e164_num in invalid or not valid:
            log.warning("SMS OVH rejeté à %s : invalides=%s", _mask(to_e164_num), invalid)
            return False
        log.info("SMS OVH envoyé à %s (ids=%s)", _mask(to_e164_num), data.get("ids"))
        return True
    except urllib.error.HTTPError as e:
        body_err = e.read().decode()[:300] if e.fp else ""
        log.warning("OVH SMS échec %d : %s", e.code, body_err)
        return False
    except Exception as e:
        log.exception("Erreur OVH SMS : %s", e)
        return False
