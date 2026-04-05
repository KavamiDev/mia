"""
Client Telnyx — appels API pour contrôler les appels téléphoniques.

Centralise les interactions avec l'API Telnyx Call Control :
  - answer   : décrocher un appel et ouvrir un stream WebSocket
  - transfer : transférer l'appel vers un autre numéro (le restaurateur)

Le transfert est utilisé quand MIA ne peut pas aider le client
ou quand celui-ci demande explicitement à parler à un humain.
"""
import json
import logging
import urllib.error
import urllib.request

from app.config import settings

log = logging.getLogger("mia.telnyx")


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
