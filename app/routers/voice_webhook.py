"""Webhooks Telnyx : POST /voice/incoming (appel entrant) + WS /voice/media-stream.

Flux complet d'un appel :

  1. Telnyx reçoit un appel sur un numéro configuré
  2. Telnyx envoie POST /voice/incoming avec event_type=call.initiated
  3. On identifie le restaurant via le numéro appelé (incoming_phone_number)
  4. On répond à Telnyx avec l'URL WSS du stream + un client_state encodé
  5. Telnyx ouvre la WebSocket /voice/media-stream et y rejoue le client_state
  6. On décode le state, charge restaurant/menu en DB, lance le bridge OpenAI
"""
import asyncio
import base64
import hmac
import json
import logging
import time
from hashlib import sha256

from fastapi import APIRouter, Depends, Request, WebSocket
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal, get_db
from app.models import MenuItem, Restaurant
from app.services.realtime_service import run_realtime_bridge
from app.services.telnyx_service import telnyx_answer, verify_telnyx_signature
from app.utils.phone import matches_phone, normalize_phone

router = APIRouter(tags=["voice"])
log = logging.getLogger("mia.voice")

# Durée de validité d'un client_state signé : un appel doit s'établir vite.
_CLIENT_STATE_TTL_SECONDS = 120


def _sign_client_state(payload: dict) -> str:
    """Sérialise + signe le client_state avec HMAC-SHA256.

    Format final : base64(JSON{...payload, ts, sig})
    Telnyx exige que le client_state soit un base64 valide (pas de séparateur),
    donc on intègre la signature DANS le JSON avant d'encoder.
    """
    secret = (settings.dashboard_secret or "").encode()
    body = {**payload, "ts": int(time.time())}
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(secret, body_json.encode(), sha256).hexdigest()[:32]
    body["sig"] = sig
    return base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()


def _verify_client_state(token: str) -> dict | None:
    """Vérifie signature + expiration. Retourne le payload décodé ou None si invalide."""
    if not token:
        return None
    try:
        data = json.loads(base64.b64decode(token).decode())
        sig_received = data.pop("sig", "")
        if not sig_received:
            return None
        # Reconstruit le body sans 'sig' pour recalculer la signature attendue.
        body_json = json.dumps(data, separators=(",", ":"), sort_keys=True)
        secret = (settings.dashboard_secret or "").encode()
        expected = hmac.new(secret, body_json.encode(), sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig_received, expected):
            return None
        if int(time.time()) - int(data.get("ts", 0)) > _CLIENT_STATE_TTL_SECONDS:
            return None
        return data
    except Exception:
        return None


def _find_restaurant(db: Session, to_num: str | None) -> Restaurant | None:
    """Trouve le restaurant associé au numéro Telnyx appelé.

    Mode multi-restaurant : match exact sur incoming_phone_number.
    Fallback mono-restaurant : s'il n'y a qu'un seul restaurant configuré,
    on l'utilise même si le numéro ne correspond pas exactement (utile en
    dev / setup initial où le numéro Telnyx n'est pas encore lié).
    """
    candidates = db.query(Restaurant).filter(Restaurant.incoming_phone_number.isnot(None)).all()
    if to_num:
        for r in candidates:
            if matches_phone(r.incoming_phone_number, to_num):
                return r
    return candidates[0] if len(candidates) == 1 else None


@router.get("/incoming")
async def voice_health():
    """Health check : permet de vérifier depuis Telnyx que le webhook est joignable."""
    return {"status": "ok"}


@router.post("/incoming")
async def handle_telnyx_webhook(request: Request, db: Session = Depends(get_db)):
    """Webhook principal : reçoit call.initiated et déclenche le décrochage.

    Telnyx peut envoyer d'autres events (call.hangup, call.answered, etc.)
    qu'on ignore — on n'agit que sur l'initiation.

    Sécurité : vérifie la signature Ed25519 Telnyx pour bloquer les webhooks
    forgés (sinon un attaquant peut consommer notre clé API + générer des coûts).
    """
    raw_body = await request.body()
    if not verify_telnyx_signature(request.headers, raw_body):
        log.warning("Webhook Telnyx rejeté : signature invalide")
        return JSONResponse({"error": "Invalid signature"}, status_code=401)

    try:
        body = json.loads(raw_body)
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    data = body.get("data", {})
    if data.get("event_type") != "call.initiated":
        return JSONResponse({"status": "ok"})

    payload = data.get("payload", {})
    to_num = normalize_phone(payload.get("to", ""))
    from_num = payload.get("from", "")

    # Filtre les appels masqués/anonymes — pas de SMS de confirmation possible
    # dans ce cas, mais on accepte quand même l'appel (chaîne caller_phone vide).
    caller = (normalize_phone(from_num)
              if from_num and from_num.lower() not in ("anonymous", "restricted", "private")
              else "")
    call_id = payload.get("call_control_id", "")
    log.info("Appel entrant : to=%r from=%r", to_num, caller or "anonyme")

    restaurant = _find_restaurant(db, to_num)
    if not restaurant:
        # Numéro non configuré : on retourne 200 OK pour que Telnyx ne retry pas.
        log.warning("Numéro non configuré : %s", to_num)
        return JSONResponse({"status": "ok"})

    # client_state SIGNÉ : HMAC-SHA256(dashboard_secret) avec horodatage.
    # Empêche un attaquant d'ouvrir /voice/media-stream en forgeant un restaurant_id.
    client_state = _sign_client_state({
        "restaurant_id": restaurant.id,
        "caller_phone": caller,
        "call_control_id": call_id,
    })
    stream_url = f"wss://{settings.stream_wss_domain}/voice/media-stream"

    # telnyx_answer fait un HTTP synchrone — déporté en thread pour ne pas
    # bloquer la boucle asyncio qui sert d'autres appels en parallèle.
    if not await asyncio.to_thread(telnyx_answer, call_id, stream_url, client_state):
        log.error("Échec answer Telnyx pour call %s", call_id)
    return JSONResponse({"status": "ok"})


@router.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """WebSocket audio bidirectionnel Telnyx ↔ bridge OpenAI."""
    await websocket.accept()

    # ─── Étape 1 : attendre le message 'start' qui contient le client_state ───
    # Telnyx envoie d'abord 'connected' puis 'start'. On bufferise les messages
    # vus avant 'start' pour les rejouer dans le bridge (pas de perte audio).
    buffered: list[str] = []
    start_msg = None
    try:
        # Max 5 messages avant 'start' (en pratique 1-2) ; timeout 5s par message.
        for _ in range(5):
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            buffered.append(msg)
            if json.loads(msg).get("event") == "start":
                start_msg = json.loads(msg)
                break
    except asyncio.TimeoutError:
        pass

    if not start_msg:
        await websocket.close(code=4000)  # 4000 = abnormal closure côté applicatif
        return

    # ─── Étape 2 : vérifier signature + décoder le client_state ───
    # Si la signature HMAC est invalide ou expirée (>120s), on refuse la
    # connexion : c'est probablement un attaquant qui tente d'ouvrir le WS
    # avec un restaurant_id forgé.
    state = _verify_client_state(start_msg.get("start", {}).get("client_state", ""))
    if not state:
        log.warning("media-stream : client_state invalide ou expiré — fermeture")
        await websocket.close(code=4001)
        return
    try:
        rid = int(state["restaurant_id"])
        caller_phone = state.get("caller_phone", "") or ""
        call_control_id = state.get("call_control_id", "") or ""
    except (KeyError, ValueError):
        await websocket.close(code=4000)
        return

    # ─── Étape 3 : charger le restaurant et son menu (1 round-trip DB) ───
    # On sérialise en dict simple pour ne pas trimballer l'ORM dans le bridge
    # (qui tourne dans des tâches asyncio).
    db = SessionLocal()
    try:
        r = db.query(Restaurant).filter(Restaurant.id == rid).first()
        if not r:
            await websocket.close(code=4000)
            return
        restaurant = {"id": r.id, "nom": r.nom, "telephone": r.telephone,
                      "horaires": r.horaires or "", "adresse": r.adresse or "",
                      "quota_reservations": r.quota_reservations,
                      "quota_commandes": r.quota_commandes,
                      # Toggles SMS — défaut True pour rétro-compat
                      "sms_to_client": r.sms_to_client if r.sms_to_client is not None else True,
                      "sms_to_restaurant": r.sms_to_restaurant if r.sms_to_restaurant is not None else True}
        menu = [{"nom_plat": m.nom_plat, "prix": m.prix, "description": m.description or ""}
                for m in db.query(MenuItem).filter(MenuItem.restaurant_id == rid).all()]
    finally:
        db.close()

    # ─── Étape 4 : lancer le bridge avec un timeout global ───
    # Coupe-circuit anti-coût : si un appel reste ouvert >5 min, on tue la
    # session. OpenAI Realtime coûte ~0.30$/min input audio.
    # Note : avec le fix asyncio.wait(FIRST_COMPLETED) dans run_realtime_bridge
    # + détection event 'stop' Telnyx, ce timeout n'est qu'un GARDE-FOU.
    # En cas normal, le bridge se termine en <5s après que le client raccroche.
    try:
        await asyncio.wait_for(
            run_realtime_bridge(websocket, restaurant, menu=menu, caller_phone=caller_phone,
                                call_control_id=call_control_id, initial_messages=buffered),
            timeout=300,
        )
    except asyncio.TimeoutError:
        log.warning("media-stream : appel >5min, coupé par timeout global")
    except Exception as e:
        log.exception("media-stream : %s", e)
    finally:
        # Fermeture défensive : si le bridge s'est terminé proprement, la socket
        # est déjà fermée et ce close() lèvera silencieusement.
        try:
            await websocket.close()
        except Exception:
            pass
