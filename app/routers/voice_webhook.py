"""
Webhook Telnyx pour les appels entrants.

Flux d'un appel :
  1. Telnyx envoie POST /voice/incoming (event: call.initiated)
  2. On identifie le restaurant via le numéro appelé (incoming_phone_number)
  3. On répond à Telnyx avec l'URL WebSocket pour le stream média
  4. Telnyx ouvre WS /voice/media-stream avec l'audio bidirectionnel
  5. On lance le bridge OpenAI Realtime (realtime_service)

Le numéro de l'appelant et le call_control_id sont passés via
client_state (base64 JSON) pour être disponibles dans le WebSocket.
Le call_control_id est nécessaire pour le transfert vers un humain.
"""
import asyncio
import base64
import json
import logging

from fastapi import APIRouter, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import MenuItem, Restaurant
from app.services.realtime_service import run_realtime_bridge
from app.services.telnyx_service import telnyx_answer
from app.utils.phone import matches_phone, normalize_phone

router = APIRouter(tags=["voice"])
log = logging.getLogger("mia.voice")


# ──────────────────────────────────────
# Identification du restaurant
# ──────────────────────────────────────

def _find_restaurant(db: Session, incoming_number: str | None) -> Restaurant | None:
    """Trouve le restaurant associé au numéro Telnyx appelé.

    Si un seul restaurant est configuré avec un numéro, on le retourne
    même si le numéro ne match pas exactement (mode mono-restaurant).
    """
    all_with_num = list(
        db.query(Restaurant).filter(Restaurant.incoming_phone_number.isnot(None)).all()
    )
    if incoming_number:
        for r in all_with_num:
            if matches_phone(r.incoming_phone_number, incoming_number):
                return r
    if len(all_with_num) == 1:
        return all_with_num[0]
    return None


def _restaurant_to_dict(r: Restaurant) -> dict:
    """Sérialise les champs nécessaires au bridge (évite de passer l'ORM)."""
    return {
        "id": r.id,
        "nom": r.nom,
        "telephone": r.telephone,
        "horaires": r.horaires or "",
        "adresse": r.adresse or "",
        "quota_reservations": r.quota_reservations,
        "quota_commandes": r.quota_commandes,
    }


def _load_menu(db: Session, restaurant_id: int) -> list[dict]:
    """Charge le menu complet d'un restaurant pour l'injecter dans le prompt."""
    items = db.query(MenuItem).filter(MenuItem.restaurant_id == restaurant_id).all()
    return [{"nom_plat": m.nom_plat, "prix": m.prix, "description": m.description or ""} for m in items]


# ──────────────────────────────────────
# Endpoints
# ──────────────────────────────────────

@router.get("/incoming")
async def voice_health_check():
    """Health check pour vérifier que le webhook est joignable."""
    return {"status": "ok"}


@router.post("/incoming")
async def handle_telnyx_webhook(request: Request, db: Session = Depends(get_db)):
    """Reçoit les événements Telnyx (call.initiated, etc.).

    On ne traite que call.initiated : on identifie le restaurant,
    puis on répond à l'appel en ouvrant un stream WebSocket.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    data = body.get("data", {})
    event_type = data.get("event_type", "")
    payload = data.get("payload", {})

    if event_type == "call.initiated":
        to_num = normalize_phone(payload.get("to", ""))
        from_num = payload.get("from", "")
        # Filtrage des appels anonymes/masqués.
        caller_phone = (
            normalize_phone(from_num)
            if from_num and from_num.lower() not in ("anonymous", "restricted", "private")
            else ""
        )
        call_control_id = payload.get("call_control_id", "")

        log.info("Appel entrant : to=%r from=%r", to_num, caller_phone or "anonyme")

        restaurant = _find_restaurant(db, to_num)
        if not restaurant:
            log.warning("Numéro non configuré : %s — appel ignoré", to_num)
            return JSONResponse({"status": "ok"})

        # Encode restaurant_id, caller_phone ET call_control_id dans le client_state.
        # Telnyx renvoie ce blob dans l'événement 'start' du WebSocket,
        # ce qui permet au bridge d'avoir toutes les infos sans requête DB.
        stream_url = f"wss://{settings.stream_wss_domain}/voice/media-stream"
        client_state = base64.b64encode(
            json.dumps({
                "restaurant_id": restaurant.id,
                "caller_phone": caller_phone or "",
                "call_control_id": call_control_id,
            }).encode()
        ).decode()

        ok = telnyx_answer(call_control_id, stream_url, client_state)
        if not ok:
            log.error("Échec answer Telnyx pour call %s", call_control_id)

    return JSONResponse({"status": "ok"})


@router.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """WebSocket bidirectionnel : reçoit l'audio Telnyx, lance le bridge OpenAI."""
    await websocket.accept()

    # Attend le message 'start' de Telnyx qui contient le client_state.
    start_msg, buffered = await _wait_for_start(websocket)
    if not start_msg:
        log.warning("media-stream : pas de message start — fermeture")
        await websocket.close(code=4000)
        return

    rid, caller_phone, call_control_id = _parse_start_params(start_msg)
    if rid is None:
        try:
            rid = int(websocket.query_params.get("restaurant_id", ""))
        except (TypeError, ValueError):
            rid = None
    if rid is None:
        await websocket.close(code=4000)
        return

    # Charge le restaurant et son menu depuis la DB (une seule fois par appel).
    db = SessionLocal()
    try:
        restaurant = db.query(Restaurant).filter(Restaurant.id == rid).first()
        if not restaurant:
            await websocket.close(code=4000)
            return
        restaurant_dict = _restaurant_to_dict(restaurant)
        menu = _load_menu(db, rid)
    except Exception:
        await websocket.close(code=1011)
        return
    finally:
        db.close()

    log.info("media-stream : %s (%d plats, caller=%s)", restaurant_dict["nom"], len(menu), caller_phone or "?")

    try:
        await run_realtime_bridge(
            websocket,
            restaurant_dict,
            menu=menu,
            caller_phone=caller_phone,
            call_control_id=call_control_id,
            initial_messages=buffered,
        )
    except WebSocketDisconnect:
        log.info("media-stream : client déconnecté")
    except Exception as e:
        log.exception("media-stream : %s", e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ──────────────────────────────────────
# Parsing du message 'start' Telnyx
# ──────────────────────────────────────

async def _wait_for_start(websocket: WebSocket) -> tuple[dict | None, list[str]]:
    """Attend le message 'start' de Telnyx (max 5 messages, timeout 5s)."""
    messages: list[str] = []
    try:
        for _ in range(5):
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            messages.append(msg)
            data = json.loads(msg)
            if data.get("event") == "start":
                return data, messages
    except asyncio.TimeoutError:
        pass
    return None, messages


def _parse_start_params(start_msg: dict) -> tuple[int | None, str, str]:
    """Extrait restaurant_id, caller_phone et call_control_id du client_state."""
    client_state = start_msg.get("start", {}).get("client_state")
    if not client_state:
        return None, "", ""
    try:
        decoded = json.loads(base64.b64decode(client_state).decode())
        rid = decoded.get("restaurant_id")
        caller = decoded.get("caller_phone", "") or ""
        cc_id = decoded.get("call_control_id", "") or ""
        if rid is not None:
            return int(rid), caller, cc_id
    except Exception:
        pass
    return None, "", ""
