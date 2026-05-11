"""Webhooks Telnyx : POST /voice/incoming (appel entrant) + WS /voice/media-stream.

Flux : Telnyx envoie call.initiated → on identifie le restaurant via le numéro
appelé → on répond avec l'URL WSS → Telnyx ouvre le stream → bridge OpenAI.
"""
import asyncio
import base64
import json
import logging

from fastapi import APIRouter, Depends, Request, WebSocket
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


def _find_restaurant(db: Session, to_num: str | None) -> Restaurant | None:
    """Trouve le restaurant via son numéro Telnyx. Fallback mono-restaurant."""
    candidates = db.query(Restaurant).filter(Restaurant.incoming_phone_number.isnot(None)).all()
    if to_num:
        for r in candidates:
            if matches_phone(r.incoming_phone_number, to_num):
                return r
    return candidates[0] if len(candidates) == 1 else None


@router.get("/incoming")
async def voice_health():
    return {"status": "ok"}


@router.post("/incoming")
async def handle_telnyx_webhook(request: Request, db: Session = Depends(get_db)):
    """Reçoit call.initiated et déclenche l'ouverture du stream."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    data = body.get("data", {})
    if data.get("event_type") != "call.initiated":
        return JSONResponse({"status": "ok"})

    payload = data.get("payload", {})
    to_num = normalize_phone(payload.get("to", ""))
    from_num = payload.get("from", "")
    caller = (normalize_phone(from_num)
              if from_num and from_num.lower() not in ("anonymous", "restricted", "private")
              else "")
    call_id = payload.get("call_control_id", "")
    log.info("Appel entrant : to=%r from=%r", to_num, caller or "anonyme")

    restaurant = _find_restaurant(db, to_num)
    if not restaurant:
        log.warning("Numéro non configuré : %s", to_num)
        return JSONResponse({"status": "ok"})

    # Encode l'état dans client_state (Telnyx le retransmet dans le WS).
    client_state = base64.b64encode(json.dumps({
        "restaurant_id": restaurant.id, "caller_phone": caller, "call_control_id": call_id,
    }).encode()).decode()
    stream_url = f"wss://{settings.stream_wss_domain}/voice/media-stream"

    if not telnyx_answer(call_id, stream_url, client_state):
        log.error("Échec answer Telnyx pour call %s", call_id)
    return JSONResponse({"status": "ok"})


@router.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """WebSocket audio bidirectionnel Telnyx ↔ bridge OpenAI."""
    await websocket.accept()

    # Attend le message 'start' contenant le client_state (timeout 5s).
    buffered: list[str] = []
    start_msg = None
    try:
        for _ in range(5):
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            buffered.append(msg)
            if json.loads(msg).get("event") == "start":
                start_msg = json.loads(msg)
                break
    except asyncio.TimeoutError:
        pass

    if not start_msg:
        await websocket.close(code=4000)
        return

    # Décode client_state pour récupérer restaurant_id, caller_phone, call_control_id.
    try:
        state = json.loads(base64.b64decode(start_msg["start"]["client_state"]).decode())
        rid = int(state["restaurant_id"])
        caller_phone = state.get("caller_phone", "") or ""
        call_control_id = state.get("call_control_id", "") or ""
    except Exception:
        await websocket.close(code=4000)
        return

    # Charge restaurant + menu (une seule fois par appel).
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == rid).first()
        if not r:
            await websocket.close(code=4000)
            return
        restaurant = {"id": r.id, "nom": r.nom, "telephone": r.telephone,
                      "horaires": r.horaires or "", "adresse": r.adresse or "",
                      "quota_reservations": r.quota_reservations,
                      "quota_commandes": r.quota_commandes}
        menu = [{"nom_plat": m.nom_plat, "prix": m.prix, "description": m.description or ""}
                for m in db.query(MenuItem).filter(MenuItem.restaurant_id == rid).all()]
    finally:
        db.close()

    try:
        await run_realtime_bridge(websocket, restaurant, menu=menu, caller_phone=caller_phone,
                                  call_control_id=call_control_id, initial_messages=buffered)
    except Exception as e:
        log.exception("media-stream : %s", e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
