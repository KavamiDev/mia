"""
Bridge WebSocket bidirectionnel : Telnyx ↔ OpenAI Realtime.

Architecture du flux audio en temps réel :
  ┌─────────┐   PCMU audio   ┌──────────┐   PCMU audio    ┌──────────┐
  │  Client  │ ──────────────→│  MIA     │ ───────────────→│  OpenAI  │
  │ (Telnyx) │ ←──────────────│  (FastAPI)│ ←───────────────│ Realtime │
  └─────────┘                 └──────────┘                  └──────────┘

Le bridge gère trois tâches concurrentes (asyncio.gather) :
  1. receive_from_client  — Reçoit l'audio Telnyx, le forward vers OpenAI
  2. send_to_client       — Reçoit les réponses OpenAI, les forward vers Telnyx
  3. wait_and_send_greeting — Envoie le message d'accueil initial

Gestion des interruptions (barge-in) :
  Quand le client parle pendant que MIA répond, on attend vad_barge_in_delay_ms
  avant de couper la réponse en cours (évite les coupures sur le bruit ambiant).

Filtrage du bruit :
  Les transcriptions trop courtes ou composées uniquement d'hésitations
  ("euh", "hum") sont ignorées pour éviter que MIA ne réagisse au bruit.
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

# ──────────────────────────────────────
# Définition des tools exposés à OpenAI
# ──────────────────────────────────────

REALTIME_TOOLS = [
    {
        "type": "function",
        "name": "create_reservation",
        "description": "Créer une réservation. Appeler UNIQUEMENT après récapitulatif et confirmation du client.",
        "parameters": {
            "type": "object",
            "properties": {
                "personnes": {"type": "integer"},
                "heure": {"type": "string", "description": "Ex: 20h, 20h30"},
                "date": {"type": "string", "description": "Format AAAA-MM-JJ"},
            },
            "required": ["personnes", "heure", "date"],
        },
    },
    {
        "type": "function",
        "name": "create_commande",
        "description": "Créer une commande à emporter. Appeler UNIQUEMENT après récapitulatif et confirmation du client.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "plat": {"type": "string"},
                            "qty": {"type": "integer"},
                        },
                        "required": ["plat", "qty"],
                    },
                },
            },
            "required": ["items"],
        },
    },
    {
        "type": "function",
        "name": "transfer_to_human",
        "description": (
            "Transférer l'appel vers le restaurant (un humain). "
            "Utiliser si le client le demande explicitement OU si tu ne "
            "parviens pas à comprendre/aider après 2-3 tentatives."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "raison": {
                    "type": "string",
                    "description": "Raison du transfert (ex: 'demande client', 'incompréhension répétée', 'question hors périmètre')",
                },
            },
            "required": ["raison"],
        },
    },
]

# Instructions complémentaires injectées après le contexte du restaurant.
CONFIRMATION_INSTRUCTIONS = """

## Informations automatiques (NE PAS DEMANDER)
- Le téléphone du client est celui de l'appelant → NE DEMANDE JAMAIS le numéro.
- Le nom du client n'est PAS nécessaire → NE DEMANDE JAMAIS le nom ni le prénom.
- Un code unique est généré automatiquement (ex: R4T2K, C7MN9) pour identifier la réservation/commande.
- Après confirmation, annonce le code au client et dis qu'il recevra un SMS.

## Interruption par bruit
- Si l'utilisateur t'interrompt sans rien dire de compréhensible : attends ou dis "Je vous écoute."
- Ne répète pas ta dernière phrase."""

# Mots d'hésitation filtrés pour éviter que MIA réagisse au bruit.
HESITATION_WORDS = frozenset({"euh", "ah", "eh", "hum", "hm", "heu", "euhh", "ahh"})


def _is_transcription_failure(transcript: str) -> bool:
    """Détecte les transcriptions invalides (bruit, hésitations, silence)."""
    t = (transcript or "").strip().lower()
    if not t:
        return True
    words = [w for w in t.split() if w]
    if len(words) < settings.vad_min_transcript_words:
        return True
    if all(w in HESITATION_WORDS for w in words):
        return True
    return False


# ──────────────────────────────────────
# Construction du prompt contextuel
# ──────────────────────────────────────

def _build_instructions(restaurant: dict, menu: list[dict], caller_phone: str = "") -> str:
    """Assemble les instructions complètes envoyées à OpenAI.

    Combine le prompt système (personnalité MIA) avec le contexte
    spécifique du restaurant (horaires, menu, quotas, numéro appelant).
    """
    lines = [
        f"Restaurant : {restaurant.get('nom', '')}",
        f"Horaires : {restaurant.get('horaires', 'Non renseignés')}",
        f"Adresse : {restaurant.get('adresse', '')}",
    ]

    quota_r = restaurant.get("quota_reservations")
    quota_c = restaurant.get("quota_commandes")
    if quota_r:
        lines.append(f"Quota réservations : {quota_r} max par jour. Si complet, proposer un autre jour.")
    if quota_c:
        lines.append(f"Quota commandes : {quota_c} max par jour. Si complet, informer le client.")

    if menu:
        lines.append("\nMenu :")
        for item in menu[:30]:
            desc = f" — {item['description']}" if item.get("description") else ""
            lines.append(f"- {item.get('nom_plat', '')} : {item.get('prix', 0)}€{desc}")

    lines.append(f"\nDate du jour : {date.today().isoformat()} (format AAAA-MM-JJ pour réservations).")
    if caller_phone:
        lines.append(f"\nNuméro de l'appelant : {caller_phone}. Ne jamais demander le numéro.")

    return f"{SYSTEM_PROMPT}\n\n## Contexte du restaurant\n" + "\n".join(lines) + CONFIRMATION_INSTRUCTIONS


def _build_greeting(restaurant: dict) -> str:
    """Message d'accueil initial que MIA prononcera."""
    nom = restaurant.get("nom", "le restaurant")
    return f"Salue le client avec : 'Bonjour, bienvenue chez {nom}, MIA à l'appareil.'"


# ──────────────────────────────────────
# Helpers WebSocket
# ──────────────────────────────────────

def _is_open(ws) -> bool:
    """Vérifie si un WebSocket (websockets lib) est encore ouvert."""
    try:
        return getattr(ws.state, "name", str(ws.state)) == "OPEN"
    except Exception:
        return False


async def _safe_send(ws: WebSocket, payload: dict) -> bool:
    """Envoie JSON sur le WebSocket Telnyx sans crasher si déconnecté."""
    try:
        if ws.client_state != WebSocketState.CONNECTED:
            return False
        await ws.send_json(payload)
        return True
    except Exception:
        return False


# ──────────────────────────────────────
# Bridge principal
# ──────────────────────────────────────

async def run_realtime_bridge(
    client_ws: WebSocket,
    restaurant: dict,
    menu: list[dict] | None = None,
    caller_phone: str = "",
    call_control_id: str = "",
    initial_messages: list[str] | None = None,
) -> None:
    """Lance le bridge bidirectionnel Telnyx ↔ OpenAI pour un appel."""
    call_id = str(uuid.uuid4())[:8]
    menu = menu or []
    instructions = _build_instructions(restaurant, menu, caller_phone)
    greeting = _build_greeting(restaurant)

    log.info("[%s] Connexion OpenAI Realtime...", call_id)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    openai_url = "wss://api.openai.com/v1/realtime?model=gpt-4o-mini-realtime-preview"

    restaurant_id = restaurant.get("id")

    async with websockets.connect(
        openai_url,
        additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        ssl=ssl_context,
    ) as openai_ws:
        log.info("[%s] OpenAI connecté", call_id)
        await _initialize_session(openai_ws, instructions)

        # --- État partagé entre les tâches concurrentes ---
        stream_sid = None              # Identifiant du stream Telnyx
        stream_ready = asyncio.Event() # Signale que le stream audio est prêt
        last_assistant_item = None     # Dernier item audio en cours de lecture
        current_response_id = None     # ID de la réponse OpenAI en cours
        latest_media_timestamp = 0     # Timestamp du dernier paquet audio reçu
        mark_queue: list[str] = []     # File de marks pour synchroniser l'audio
        commit_fallback_task = None    # Tâche de commit forcé si VAD silencieux
        interruption_task = None       # Tâche de barge-in différé
        last_ai_utterance = ""         # Dernière phrase prononcée par MIA
        cancel_next_response = False   # Flag : ignorer la prochaine réponse (bruit)
        pending_transfer = False       # Flag : transfert à exécuter après le goodbye de MIA

        # ── Tâche 1 : Telnyx → OpenAI ──

        async def receive_from_client():
            nonlocal stream_sid, latest_media_timestamp
            try:
                async def message_iter():
                    for m in (initial_messages or []):
                        yield m
                    async for msg in client_ws.iter_text():
                        yield msg

                async for message in message_iter():
                    data = json.loads(message)

                    if data.get("event") == "media" and _is_open(openai_ws):
                        media = data.get("media", {})
                        if media.get("track", "inbound") != "inbound":
                            continue
                        try:
                            latest_media_timestamp = int(media.get("timestamp", 0) or 0)
                        except (ValueError, TypeError):
                            latest_media_timestamp = 0
                        # Forward audio PCMU vers OpenAI
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": media["payload"],
                        }))

                    elif data.get("event") == "start":
                        stream_sid = data.get("stream_id") or data.get("start", {}).get("streamSid")
                        log.info("[%s] Stream démarré : %s", call_id, stream_sid)
                        stream_ready.set()

                    elif data.get("event") == "mark" and mark_queue:
                        mark_queue.pop(0)

            except WebSocketDisconnect:
                log.info("[%s] Client déconnecté", call_id)
                if _is_open(openai_ws):
                    await openai_ws.close()

        # ── Tâche 2 : Message d'accueil ──

        async def wait_and_send_greeting():
            try:
                await asyncio.wait_for(stream_ready.wait(), timeout=5.0)
                await _send_initial_greeting(openai_ws, greeting)
            except asyncio.TimeoutError:
                log.warning("[%s] Timeout : pas d'événement start Telnyx", call_id)

        # ── Tâche 3 : OpenAI → Telnyx ──

        async def send_to_client():
            nonlocal stream_sid, last_assistant_item, current_response_id
            nonlocal commit_fallback_task, interruption_task
            nonlocal last_ai_utterance, cancel_next_response, pending_transfer
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    event_type = response.get("type", "")

                    # --- Erreurs OpenAI ---
                    if event_type == "error":
                        err = response.get("error", {})
                        code = err.get("code", "")
                        # Erreurs bénignes : buffer vide ou truncate sur item inexistant.
                        if code in ("item_truncate_invalid_item_id", "input_audio_buffer_commit_empty"):
                            continue
                        log.error("[%s] OpenAI error : %s", call_id, response)
                        return

                    # --- Détection de parole (début) ---
                    # Le client commence à parler : on programme un commit
                    # de sécurité au cas où le speech_stopped ne viendrait pas.
                    if event_type == "input_audio_buffer.speech_started":
                        if commit_fallback_task:
                            commit_fallback_task.cancel()

                        async def _commit_after_silence():
                            try:
                                await asyncio.sleep(2.5)
                                await openai_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            except asyncio.CancelledError:
                                pass
                        commit_fallback_task = asyncio.create_task(_commit_after_silence())

                    # --- Détection de parole (fin) ---
                    if event_type == "input_audio_buffer.speech_stopped":
                        if commit_fallback_task:
                            commit_fallback_task.cancel()
                            commit_fallback_task = None
                        if interruption_task:
                            interruption_task.cancel()
                            interruption_task = None

                    if event_type == "input_audio_buffer.committed":
                        if commit_fallback_task:
                            commit_fallback_task.cancel()
                            commit_fallback_task = None

                    # --- Nouvelle réponse OpenAI ---
                    if event_type == "response.created":
                        rid = response.get("response", {}).get("id")
                        if cancel_next_response:
                            # Bruit détecté : on annule cette réponse et demande
                            # à MIA de dire simplement "Je vous écoute."
                            cancel_next_response = False
                            try:
                                await openai_ws.send(json.dumps({"type": "response.cancel", "response_id": rid}))
                            except Exception:
                                pass
                            ctx = f" Tu viens de dire: «{last_ai_utterance[:80]}»" if last_ai_utterance else ""
                            await openai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message", "role": "system",
                                    "content": [{"type": "input_text", "text": f"Bruit ou hésitation détecté.{ctx} Ne répète pas. Dis juste : «Je vous écoute.»"}],
                                },
                            }))
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                        else:
                            current_response_id = rid
                            last_ai_utterance = ""

                    # --- Audio sortant (MIA parle) → Forward vers Telnyx ---
                    if event_type == "response.output_audio.delta" and response.get("delta"):
                        if stream_sid:
                            audio_payload = base64.b64encode(base64.b64decode(response["delta"])).decode()
                            ok = await _safe_send(client_ws, {
                                "event": "media", "streamSid": stream_sid,
                                "media": {"payload": audio_payload},
                            })
                            if not ok:
                                return

                    # --- Transcription de ce que MIA dit (pour contexte d'interruption) ---
                    if event_type == "response.output_audio_transcript.delta":
                        delta = response.get("delta", "")
                        if delta:
                            last_ai_utterance = (last_ai_utterance + delta).strip()

                    if event_type == "response.output_audio_transcript.done":
                        transcript = response.get("transcript", "")
                        if transcript:
                            last_ai_utterance = str(transcript).strip()

                    # --- Réponse terminée → Exécution des function calls ---
                    if event_type == "response.done":
                        last_assistant_item = None
                        current_response_id = None
                        if interruption_task:
                            interruption_task.cancel()
                            interruption_task = None

                        output_items = response.get("response", {}).get("output", [])
                        has_function_calls = False

                        for item in output_items:
                            if item.get("type") == "function_call" and restaurant_id:
                                has_function_calls = True
                                name = item.get("name")
                                call_id_fc = item.get("call_id")
                                try:
                                    args = json.loads(item.get("arguments", "{}"))
                                except json.JSONDecodeError:
                                    args = {}

                                # Exécution synchrone dans un thread (DB + SMS).
                                result = await asyncio.to_thread(
                                    execute_tool_call,
                                    restaurant_id, name, args,
                                    menu=menu,
                                    quota_reservations=restaurant.get("quota_reservations"),
                                    quota_commandes=restaurant.get("quota_commandes"),
                                    restaurant_phone=restaurant.get("telephone"),
                                    caller_phone=caller_phone or None,
                                    restaurant_name=restaurant.get("nom", ""),
                                )

                                # Si le tool demande un transfert, on le note
                                # mais on attend que MIA finisse de parler.
                                if result.get("transfer"):
                                    pending_transfer = True

                                # Renvoie le résultat à OpenAI → MIA le lit au client.
                                await openai_ws.send(json.dumps({
                                    "type": "conversation.item.create",
                                    "item": {"type": "function_call_output", "call_id": call_id_fc, "output": json.dumps(result)},
                                }))
                                await openai_ws.send(json.dumps({"type": "response.create"}))

                        # Transfert : MIA vient de prononcer le goodbye (pas de
                        # function_call dans cette réponse). On attend que l'audio
                        # finisse de jouer, puis on transfère via Telnyx.
                        if pending_transfer and not has_function_calls:
                            await asyncio.sleep(3)
                            if call_control_id and restaurant.get("telephone"):
                                await asyncio.to_thread(
                                    telnyx_transfer,
                                    call_control_id,
                                    restaurant["telephone"],
                                )
                            log.info("[%s] Bridge terminé après transfert", call_id)
                            return

                    # --- MIA a été coupée par le client ---
                    if event_type == "conversation.item.truncated":
                        ctx = f" Tu disais: «{last_ai_utterance[:80]}»" if last_ai_utterance else ""
                        await openai_ws.send(json.dumps({
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message", "role": "system",
                                "content": [{"type": "input_text", "text": f"Coupée par un bruit.{ctx} Ne recommence pas. Dis «Je vous écoute»."}],
                            },
                        }))
                        await openai_ws.send(json.dumps({"type": "response.create"}))

                    # --- Nouveau message utilisateur → Filtrage du bruit ---
                    if event_type == "conversation.item.created":
                        item = response.get("item", {})
                        if item.get("type") == "message" and item.get("role") == "user":
                            transcript = ""
                            for c in item.get("content", []):
                                if c.get("transcript"):
                                    transcript = c["transcript"].strip()
                                    break
                                if c.get("text"):
                                    transcript = c["text"].strip()
                                    break
                            # Si transcription invalide (bruit/hésitation),
                            # on flag pour annuler la réponse qui va suivre.
                            if _is_transcription_failure(transcript) or len(transcript) < settings.vad_min_transcript_chars:
                                cancel_next_response = True
                                if current_response_id:
                                    try:
                                        await openai_ws.send(json.dumps({"type": "response.cancel", "response_id": current_response_id}))
                                        current_response_id = None
                                    except Exception:
                                        pass

                    # --- Synchronisation audio (marks) ---
                    if response.get("item_id") and response["item_id"] != last_assistant_item:
                        last_assistant_item = response["item_id"]
                        if stream_sid:
                            ok = await _safe_send(client_ws, {"event": "mark", "streamSid": stream_sid, "mark": {"name": "responsePart"}})
                            if ok:
                                mark_queue.append("responsePart")

                    # --- Gestion du barge-in (interruption différée) ---
                    # On attend vad_barge_in_delay_ms avant de couper MIA,
                    # pour ne pas réagir au bruit ambiant instantanément.
                    if event_type == "input_audio_buffer.speech_started" and last_assistant_item:
                        if interruption_task:
                            interruption_task.cancel()
                        resp_id = current_response_id

                        async def _delayed_interrupt():
                            nonlocal last_assistant_item, current_response_id
                            try:
                                await asyncio.sleep(settings.vad_barge_in_delay_ms / 1000.0)
                                if resp_id:
                                    try:
                                        await openai_ws.send(json.dumps({"type": "response.cancel", "response_id": resp_id}))
                                    except Exception:
                                        pass
                                if stream_sid:
                                    await _safe_send(client_ws, {"event": "clear", "streamSid": stream_sid})
                                mark_queue.clear()
                                last_assistant_item = None
                                current_response_id = None
                            except asyncio.CancelledError:
                                pass
                        interruption_task = asyncio.create_task(_delayed_interrupt())

            except asyncio.CancelledError:
                pass
            except Exception as e:
                log.exception("[%s] Erreur bridge : %s", call_id, e)
                raise

        # Lancement des 3 tâches concurrentes.
        try:
            await asyncio.gather(wait_and_send_greeting(), receive_from_client(), send_to_client())
        except asyncio.CancelledError:
            pass


# ──────────────────────────────────────
# Initialisation session OpenAI
# ──────────────────────────────────────

async def _initialize_session(openai_ws, instructions: str) -> None:
    """Configure la session OpenAI Realtime (voix, VAD, tools)."""
    session_config = {
        "type": "realtime",
        "model": "gpt-4o-mini-realtime-preview",
        "output_modalities": ["audio"],
        "instructions": instructions,
        "audio": {
            "input": {
                "format": {"type": "audio/pcmu"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": settings.vad_threshold,
                    "prefix_padding_ms": settings.vad_prefix_padding_ms,
                    "silence_duration_ms": settings.vad_silence_duration_ms,
                    "create_response": True,
                },
            },
            "output": {
                "format": {"type": "audio/pcmu"},
                "voice": settings.voice_realtime_voice,
            },
        },
        "tools": REALTIME_TOOLS,
        "tool_choice": "auto",
    }
    await openai_ws.send(json.dumps({"type": "session.update", "session": session_config}))


async def _send_initial_greeting(openai_ws, greeting: str) -> None:
    """Déclenche le message d'accueil dès que le stream audio est prêt."""
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": greeting}],
        },
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))
