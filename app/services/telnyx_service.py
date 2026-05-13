"""
Client Telnyx — appels API pour contrôler les appels téléphoniques.

Centralise les interactions avec l'API Telnyx Call Control :
  - answer   : décrocher un appel et ouvrir un stream WebSocket
  - transfer : transférer l'appel vers un autre numéro (le restaurateur)
  - verify_telnyx_signature : valide la signature Ed25519 d'un webhook entrant

Le transfert est utilisé quand MIA ne peut pas aider le client
ou quand celui-ci demande explicitement à parler à un humain.
"""
import base64
import json
import logging
import time
import urllib.error
import urllib.request

from app.config import settings

log = logging.getLogger("mia.telnyx")


def verify_telnyx_signature(headers, raw_body: bytes) -> bool:
    """Vérifie la signature Ed25519 d'un webhook Telnyx.

    Format Telnyx : signature = Ed25519(timestamp + "|" + body), encodée base64.
    Tolérance horloge : 5 minutes.

    Retourne True si :
    - TELNYX_PUBLIC_KEY n'est pas configurée (dev mode, on log un warning)
    - OU la signature est valide ET le timestamp est récent

    Sinon False → on rejette le webhook.
    """
    pub_key_b64 = (settings.telnyx_public_key or "").strip()
    if not pub_key_b64:
        # Dev / setup initial : on accepte mais on alerte. EN PROD ce return
        # doit être False — c'est pour ça qu'on log un warning.
        log.warning("TELNYX_PUBLIC_KEY non définie : webhook accepté sans vérif")
        return True

    sig = headers.get("telnyx-signature-ed25519") or headers.get("Telnyx-Signature-Ed25519", "")
    ts = headers.get("telnyx-timestamp") or headers.get("Telnyx-Timestamp", "")
    if not sig or not ts:
        log.warning("Webhook Telnyx sans header signature/timestamp")
        return False

    # Anti-replay : refuse les webhooks > 5 min d'âge
    try:
        if abs(int(time.time()) - int(ts)) > 300:
            log.warning("Webhook Telnyx hors fenêtre temporelle")
            return False
    except (TypeError, ValueError):
        return False

    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError
    except ImportError:
        log.error("PyNaCl non installé — impossible de vérifier la signature Telnyx")
        return False

    try:
        verify_key = VerifyKey(base64.b64decode(pub_key_b64))
        message = ts.encode() + b"|" + raw_body
        verify_key.verify(message, base64.b64decode(sig))
        return True
    except BadSignatureError:
        log.warning("Signature Telnyx invalide")
        return False
    except Exception as e:
        log.exception("Erreur vérif signature Telnyx : %s", e)
        return False


def telnyx_answer(call_control_id: str, stream_url: str, client_state: str) -> bool:
    """Décroche l'appel et ouvre un stream audio bidirectionnel (PCMU)."""
    api_key = (settings.telnyx_api_key or "").strip()
    if not api_key:
        log.warning("TELNYX_API_KEY manquant")
        return False
    try:
        payload = json.dumps({
            "stream_url": stream_url,
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
            "client_state": client_state,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/answer",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300] if e.fp else ""
        log.warning("Telnyx answer échec %d : %s", e.code, body)
        return False
    except Exception as e:
        log.exception("Telnyx answer : %s", e)
        return False


def telnyx_transfer(call_control_id: str, to_number: str) -> bool:
    """Transfère l'appel en cours vers un autre numéro (ex: le restaurateur).

    Telnyx connecte alors l'appelant directement avec le destinataire.
    Le stream média WebSocket se ferme automatiquement après le transfert.
    """
    if not call_control_id:
        log.warning("Transfert impossible : call_control_id manquant")
        return False
    if not to_number:
        log.warning("Transfert impossible : numéro destination manquant")
        return False

    api_key = (settings.telnyx_api_key or "").strip()
    if not api_key:
        log.warning("TELNYX_API_KEY manquant")
        return False

    try:
        payload = json.dumps({"to": to_number}).encode()
        req = urllib.request.Request(
            f"https://api.telnyx.com/v2/calls/{call_control_id}/actions/transfer",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        log.info("Appel transféré vers %s (call=%s)", to_number, call_control_id[:12])
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300] if e.fp else ""
        log.warning("Telnyx transfer échec %d : %s", e.code, body)
        return False
    except Exception as e:
        log.exception("Telnyx transfer : %s", e)
        return False
