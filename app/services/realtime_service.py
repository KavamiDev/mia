"""Bridge WebSocket bidirectionnel : Telnyx ↔ OpenAI Realtime.

Trois tâches asyncio concurrentes :
  1. receive_from_client  — audio Telnyx → OpenAI
  2. send_to_client       — réponses OpenAI → Telnyx + exécution des tools
  3. send_greeting        — message d'accueil dès que le stream est prêt
"""
import asyncio
import base64
import json
import logging
import ssl
import uuid
from datetime import date
from pathlib import Path

import certifi
import websockets
from fastapi import WebSocket
from fastapi.websockets import WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.config import settings
from app.services.telnyx_service import telnyx_transfer
from app.services.tool_service import execute_tool_call

log = logging.getLogger("mia.realtime")
SYSTEM_PROMPT = (Path(__file__).parent.parent.parent / "prompts" / "mia_system_prompt.md").read_text(encoding="utf-8")

REALTIME_TOOLS = [
    {"type": "function", "name": "create_reservation",
     "description": "Créer une réservation. Appeler UNIQUEMENT après récapitulatif et confirmation.",
     "parameters": {"type": "object", "required": ["personnes", "heure", "date"], "properties": {
         "personnes": {"type": "integer"},
         "heure": {"type": "string", "description": "Ex: 20h, 20h30"},
         "date": {"type": "string", "description": "Format AAAA-MM-JJ"}}}},
    {"type": "function", "name": "create_commande",
     "description": "Créer une commande à emporter. Appeler UNIQUEMENT après récapitulatif et confirmation.",
     "parameters": {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
         "type": "object", "required": ["plat", "qty"],
         "properties": {"plat": {"type": "string"}, "qty": {"type": "integer"}}}}}}},
    {"type": "function", "name": "transfer_to_human",
     "description": "Transférer l'appel vers le restaurant (humain). Utiliser si demandé ou après 2-3 échecs.",
     "parameters": {"type": "object", "required": ["raison"], "properties": {"raison": {"type": "string"}}}},
]


def _build_instructions(restaurant: dict, menu: list[dict], caller_phone: str) -> str:
    """Assemble prompt système + contexte du restaurant."""
    lines = [f"Restaurant : {restaurant.get('nom', '')}",
             f"Horaires : {restaurant.get('horaires', 'Non renseignés')}",
             f"Adresse : {restaurant.get('adresse', '')}"]
    if restaurant.get("quota_reservations"):
        lines.append(f"Quota réservations : {restaurant['quota_reservations']} max/jour.")
    if restaurant.get("quota_commandes"):
        lines.append(f"Quota commandes : {restaurant['quota_commandes']} max/jour.")
    if menu:
        lines.append("\nMenu :")
        for m in menu[:30]:
            desc = f" — {m['description']}" if m.get("description") else ""
            lines.append(f"- {m.get('nom_plat', '')} : {m.get('prix', 0)}€{desc}")
    lines.append(f"\nDate du jour : {date.today().isoformat()} (AAAA-MM-JJ).")
    if caller_phone:
        lines.append(f"Numéro appelant : {caller_phone}. Ne jamais demander le numéro.")
    return f"{SYSTEM_PROMPT}\n\n## Contexte restaurant\n" + "\n".join(lines)


async def _safe_send(ws: WebSocket, payload: dict) -> bool:
    """Envoie JSON sur Telnyx ; False si la socket est fermée."""
    try:
        if ws.client_state != WebSocketState.CONNECTED:
            return False
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def _init_session(openai_ws, instructions: str) -> None:
    """Configure la session OpenAI (audio PCMU, VAD serveur, tools)."""
    await openai_ws.send(json.dumps({"type": "session.update", "session": {
        "type": "realtime", "model": "gpt-4o-mini-realtime-preview",
        "output_modalities": ["audio"], "instructions": instructions,
        "audio": {
            "input": {"format": {"type": "audio/pcmu"}, "turn_detection": {
                "type": "server_vad", "threshold": settings.vad_threshold,
                "prefix_padding_ms": settings.vad_prefix_padding_ms,
                "silence_duration_ms": settings.vad_silence_duration_ms,
                "create_response": True}},
            "output": {"format": {"type": "audio/pcmu"}, "voice": settings.voice_realtime_voice}},
        "tools": REALTIME_TOOLS, "tool_choice": "auto"}}))


async def run_realtime_bridge(client_ws: WebSocket, restaurant: dict, *, menu=None,
                              caller_phone="", call_control_id="", initial_messages=None) -> None:
    """Lance le bridge bidirectionnel pour un appel."""
    cid = str(uuid.uuid4())[:8]
    menu = menu or []
    instructions = _build_instructions(restaurant, menu, caller_phone)
    greeting = f"Salue le client : 'Bonjour, bienvenue chez {restaurant.get('nom', 'le restaurant')}, MIA à l'appareil.'"

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    log.info("[%s] Connexion OpenAI Realtime...", cid)

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview",
        additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        ssl=ssl_ctx,
    ) as openai_ws:
        await _init_session(openai_ws, instructions)
        log.info("[%s] OpenAI connecté — %s (%d plats)", cid, restaurant.get("nom"), len(menu))

        stream_sid = None
        stream_ready = asyncio.Event()
        last_item = None
        current_response_id = None
        pending_transfer = False

        async def receive_from_client():
            """Telnyx → OpenAI : forward de l'audio entrant."""
            nonlocal stream_sid
            try:
                async def messages():
                    for m in initial_messages or []:
                        yield m
                    async for msg in client_ws.iter_text():
                        yield msg

                async for msg in messages():
                    data = json.loads(msg)
                    ev = data.get("event")
                    if ev == "media":
                        media = data.get("media", {})
                        if media.get("track", "inbound") == "inbound":
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append", "audio": media["payload"]}))
                    elif ev == "start":
                        stream_sid = data.get("stream_id") or data.get("start", {}).get("streamSid")
                        log.info("[%s] Stream démarré : %s", cid, stream_sid)
                        stream_ready.set()
            except WebSocketDisconnect:
                log.info("[%s] Client déconnecté", cid)

        async def send_greeting():
            """Envoie le message d'accueil dès que le stream est prêt."""
            try:
                await asyncio.wait_for(stream_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("[%s] Pas d'événement start Telnyx", cid)
                return
            await openai_ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": greeting}]}}))
            await openai_ws.send(json.dumps({"type": "response.create"}))

        async def send_to_client():
            """OpenAI → Telnyx : forward audio, gère interruptions et tools."""
            nonlocal last_item, current_response_id, pending_transfer
            async for msg in openai_ws:
                resp = json.loads(msg)
                t = resp.get("type", "")

                if t == "error":
                    code = resp.get("error", {}).get("code", "")
                    if code in ("item_truncate_invalid_item_id", "input_audio_buffer_commit_empty"):
                        continue
                    log.error("[%s] OpenAI error : %s", cid, resp)
                    return

                # Audio sortant → Telnyx
                if t == "response.output_audio.delta" and resp.get("delta") and stream_sid:
                    if not await _safe_send(client_ws, {"event": "media", "streamSid": stream_sid,
                                                        "media": {"payload": resp["delta"]}}):
                        return

                # ID de réponse en cours
                elif t == "response.created":
                    current_response_id = resp.get("response", {}).get("id")

                # Suivi de l'item audio pour le barge-in
                elif resp.get("item_id") and resp["item_id"] != last_item:
                    last_item = resp["item_id"]

                # Barge-in : le client parle, on coupe MIA immédiatement
                elif t == "input_audio_buffer.speech_started" and last_item:
                    if current_response_id:
                        await openai_ws.send(json.dumps({"type": "response.cancel",
                                                         "response_id": current_response_id}))
                    if stream_sid:
                        await _safe_send(client_ws, {"event": "clear", "streamSid": stream_sid})
                    last_item = None
                    current_response_id = None

                # Fin de réponse : exécution éventuelle des function calls
                elif t == "response.done":
                    last_item = None
                    current_response_id = None
                    output = resp.get("response", {}).get("output", [])
                    had_calls = False
                    for item in output:
                        if item.get("type") != "function_call":
                            continue
                        had_calls = True
                        try:
                            args = json.loads(item.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}
                        result = await asyncio.to_thread(
                            execute_tool_call, restaurant["id"], item.get("name"), args,
                            menu=menu, quota_reservations=restaurant.get("quota_reservations"),
                            quota_commandes=restaurant.get("quota_commandes"),
                            restaurant_phone=restaurant.get("telephone"),
                            caller_phone=caller_phone or None,
                            restaurant_name=restaurant.get("nom", ""))
                        if result.get("transfer"):
                            pending_transfer = True
                        await openai_ws.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": item.get("call_id"),
                            "output": json.dumps(result)}}))
                        await openai_ws.send(json.dumps({"type": "response.create"}))

                    # Transfert : MIA a fini son goodbye, on transfère
                    if pending_transfer and not had_calls:
                        await asyncio.sleep(3)
                        if call_control_id and restaurant.get("telephone"):
                            await asyncio.to_thread(telnyx_transfer, call_control_id, restaurant["telephone"])
                        log.info("[%s] Transfert effectué", cid)
                        return

        try:
            await asyncio.gather(send_greeting(), receive_from_client(), send_to_client())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("[%s] Erreur bridge : %s", cid, e)
