"""Bridge WebSocket bidirectionnel : Telnyx ↔ OpenAI Realtime.

Architecture du flux audio en temps réel :

  ┌────────┐  PCMU audio  ┌──────────┐  PCMU audio  ┌──────────┐
  │ Client │ ───────────→ │   MIA    │ ───────────→ │  OpenAI  │
  │(Telnyx)│ ←─────────── │ (FastAPI)│ ←─────────── │ Realtime │
  └────────┘              └──────────┘              └──────────┘

Le bridge orchestre 3 tâches asyncio concurrentes (asyncio.gather) :

  1. receive_from_client  — Audio Telnyx → OpenAI (forward de chaque paquet)
  2. send_to_client       — Réponses OpenAI → Telnyx (+ exécution des tools)
  3. send_greeting        — Envoie le message d'accueil dès que stream prêt

Barge-in : quand le client parle pendant que MIA répond, on coupe MIA
immédiatement (response.cancel + clear du buffer audio Telnyx).
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
conv = logging.getLogger("mia.conv")  # Logger dédié au contenu de la conversation

# Chargé une seule fois au démarrage (le prompt système est statique).
SYSTEM_PROMPT = (Path(__file__).parent.parent.parent / "prompts" / "mia_system_prompt.md").read_text(encoding="utf-8")


# ──────────────────────────────────────
# Tools exposés à OpenAI (function calling)
# ──────────────────────────────────────
# OpenAI décide quand appeler ces fonctions selon la conversation.
# L'implémentation réelle est dans tool_service.execute_tool_call.
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
    """Assemble le prompt complet : personnalité MIA + contexte du restaurant.

    Le contexte (horaires, menu, quotas, numéro appelant) est injecté à
    chaque appel pour que MIA réponde de manière personnalisée selon le
    restaurant appelé.
    """
    lines = [f"Restaurant : {restaurant.get('nom', '')}",
             f"Horaires : {restaurant.get('horaires', 'Non renseignés')}",
             f"Adresse : {restaurant.get('adresse', '')}"]
    if restaurant.get("quota_reservations"):
        lines.append(f"Quota réservations : {restaurant['quota_reservations']} max/jour.")
    if restaurant.get("quota_commandes"):
        lines.append(f"Quota commandes : {restaurant['quota_commandes']} max/jour.")

    # Menu limité aux 30 premiers plats pour ne pas saturer le contexte.
    if menu:
        lines.append("\nMenu :")
        for m in menu[:30]:
            desc = f" — {m['description']}" if m.get("description") else ""
            lines.append(f"- {m.get('nom_plat', '')} : {m.get('prix', 0)}€{desc}")

    # Date du jour injectée pour que MIA puisse interpréter "demain", "ce soir", etc.
    lines.append(f"\nDate du jour : {date.today().isoformat()} (AAAA-MM-JJ).")
    if caller_phone:
        # Le numéro est connu (Telnyx caller ID) — MIA ne doit JAMAIS le demander.
        lines.append(f"Numéro appelant : {caller_phone}. Ne jamais demander le numéro.")
    return f"{SYSTEM_PROMPT}\n\n## Contexte restaurant\n" + "\n".join(lines)


async def _safe_send(ws: WebSocket, payload: dict) -> bool:
    """Envoie un JSON sur le WebSocket Telnyx sans crasher si déconnecté.

    Pendant un appel, la socket peut se fermer à tout moment (raccroché,
    perte réseau). On vérifie l'état avant d'envoyer + try/except pour
    couvrir les cas de race condition.
    """
    try:
        if ws.client_state != WebSocketState.CONNECTED:
            return False
        await ws.send_json(payload)
        return True
    except Exception:
        return False


async def _init_session(openai_ws, instructions: str) -> None:
    """Configure la session OpenAI : audio PCMU, VAD serveur, tools.

    PCMU (G.711 µ-law) est le codec téléphonique standard utilisé par
    Telnyx — on évite ainsi un transcodage côté serveur.

    `server_vad` : c'est OpenAI qui détecte la fin de la parole côté
    utilisateur, déclenche automatiquement une réponse (create_response=True).
    Plus simple et fiable que de gérer la détection nous-mêmes.

    Pas de `transcription` configurée : sur l'audio téléphonique 8 kHz, les
    transcripteurs (whisper, gpt-4o-transcribe) hallucinent et polluent le
    contexte de conversation que le modèle voit. Le modèle gpt-4o-realtime
    a sa propre compréhension audio native, plus robuste sans ces transcriptions.
    """
    await openai_ws.send(json.dumps({"type": "session.update", "session": {
        "type": "realtime", "model": "gpt-4o-realtime-preview",
        "output_modalities": ["audio"], "instructions": instructions,
        "audio": {
            "input": {
                "format": {"type": "audio/pcmu"},
                "turn_detection": {
                    "type": "server_vad", "threshold": settings.vad_threshold,
                    "prefix_padding_ms": settings.vad_prefix_padding_ms,
                    "silence_duration_ms": settings.vad_silence_duration_ms,
                    "create_response": True}},
            "output": {"format": {"type": "audio/pcmu"}, "voice": settings.voice_realtime_voice}},
        "tools": REALTIME_TOOLS, "tool_choice": "auto"}}))


async def run_realtime_bridge(client_ws: WebSocket, restaurant: dict, *, menu=None,
                              caller_phone="", call_control_id="", initial_messages=None) -> None:
    """Lance le bridge bidirectionnel pour la durée d'un appel.

    Args:
        client_ws: WebSocket Telnyx (audio entrant/sortant)
        restaurant: dict avec id, nom, telephone, horaires, quotas...
        menu: liste de plats {nom_plat, prix, description}
        caller_phone: numéro de l'appelant (E.164)
        call_control_id: ID Telnyx pour transférer l'appel si besoin
        initial_messages: messages déjà reçus avant le bridge (start event)
                          qu'on rejoue dans le flux normal
    """
    cid = str(uuid.uuid4())[:8]  # ID court pour corréler les logs d'un appel
    menu = menu or []
    instructions = _build_instructions(restaurant, menu, caller_phone)
    # Le greeting est un message "user" virtuel qui demande à MIA de saluer.
    # Plus naturel que de lui envoyer une phrase pré-écrite à dire telle quelle.
    greeting = f"Salue le client : 'Bonjour, bienvenue chez {restaurant.get('nom', 'le restaurant')}, MIA à l'appareil.'"

    # certifi fournit les CA Mozilla — nécessaire dans certains conteneurs où
    # le bundle CA système n'est pas à jour.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    log.info("[%s] Connexion OpenAI Realtime...", cid)

    async with websockets.connect(
        "wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview",
        additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        ssl=ssl_ctx,
    ) as openai_ws:
        await _init_session(openai_ws, instructions)
        log.info("[%s] OpenAI connecté — %s (%d plats)", cid, restaurant.get("nom"), len(menu))

        # ─── État partagé entre les 3 tâches ───
        stream_sid = None                      # ID du stream Telnyx (reçu dans 'start')
        stream_ready = asyncio.Event()         # Set quand 'start' reçu → débloque le greeting
        last_item = None                       # Dernier item audio en cours de lecture
        current_response_id = None             # ID de la réponse OpenAI active (pour cancel)
        pending_transfer = False               # True si on doit transférer après le goodbye

# ───────────────────────────────────────
        # Tâche 1 : Telnyx → OpenAI
        # ───────────────────────────────────────
        async def receive_from_client():
            """Lit chaque paquet audio Telnyx et le transmet à OpenAI."""
            nonlocal stream_sid
            try:
                async def messages():
                    # On rejoue d'abord les messages bufferisés (notamment 'start'),
                    # puis on bascule sur le flux WebSocket normal.
                    for m in initial_messages or []:
                        yield m
                    async for msg in client_ws.iter_text():
                        yield msg

                async for msg in messages():
                    data = json.loads(msg)
                    ev = data.get("event")
                    if ev == "media":
                        media = data.get("media", {})
                        # Filtre : on ne traite que l'audio entrant (du client),
                        # pas celui qu'on a déjà envoyé (echo Telnyx).
                        if media.get("track", "inbound") == "inbound":
                            # Telnyx (mode=rtp) envoie : [12 bytes RTP header][µ-law payload].
                            # OpenAI attend du µ-law BRUT. On dégage le header
                            # avant de forwarder, sinon les 12 bytes de header
                            # binaire polluent le signal et OpenAI hallucine.
                            rtp_pkt = base64.b64decode(media["payload"])
                            raw_ulaw = rtp_pkt[12:] if len(rtp_pkt) > 12 else rtp_pkt
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(raw_ulaw).decode()}))
                    elif ev == "start":
                        # Le stream_sid sert à cibler nos messages sortants vers Telnyx.
                        stream_sid = data.get("stream_id") or data.get("start", {}).get("streamSid")
                        log.info("[%s] Stream démarré : %s", cid, stream_sid)
                        stream_ready.set()
            except WebSocketDisconnect:
                log.info("[%s] Client déconnecté", cid)

        # ───────────────────────────────────────
        # Tâche 2 : Message d'accueil
        # ───────────────────────────────────────
        async def send_greeting():
            """Déclenche le « Bonjour, bienvenue chez X » dès que Telnyx est prêt."""
            try:
                # Si Telnyx n'envoie pas 'start' en 5s, on abandonne — quelque chose
                # cloche dans le pipeline (DNS, TLS, etc.).
                await asyncio.wait_for(stream_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("[%s] Pas d'événement start Telnyx", cid)
                return
            # Pattern Realtime : on crée un item "user" puis on demande une response.
            await openai_ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": greeting}]}}))
            await openai_ws.send(json.dumps({"type": "response.create"}))

        # ───────────────────────────────────────
        # Tâche 3 : OpenAI → Telnyx (+ tools)
        # ───────────────────────────────────────
        async def send_to_client():
            """Lit les events OpenAI : audio sortant, function calls, barge-in."""
            nonlocal last_item, current_response_id, pending_transfer
            async for msg in openai_ws:
                resp = json.loads(msg)
                t = resp.get("type", "")

                # --- Erreurs OpenAI ---
                if t == "error":
                    code = resp.get("error", {}).get("code", "")
                    # Erreurs bénignes : buffer audio vide ou cancel sur item déjà
                    # consommé. On ignore (sinon on fermerait la session inutilement).
                    if code in ("item_truncate_invalid_item_id", "input_audio_buffer_commit_empty"):
                        continue
                    log.error("[%s] OpenAI error : %s", cid, resp)
                    return

                # --- Transcription du CLIENT (ce que MIA a entendu) ---
                # Émis par whisper après chaque tour de parole de l'utilisateur.
                if t == "conversation.item.input_audio_transcription.completed":
                    transcript = (resp.get("transcript") or "").strip()
                    if transcript:
                        conv.info("[%s] 👤 Client : %s", cid, transcript)

                # --- Transcription de MIA (ce qu'elle a dit) ---
                # Émis quand MIA a fini de générer une réponse audio.
                if t == "response.output_audio_transcript.done":
                    transcript = (resp.get("transcript") or "").strip()
                    if transcript:
                        conv.info("[%s] 🤖 MIA    : %s", cid, transcript)

                # --- Audio sortant (MIA parle) → forward vers Telnyx ---
                if t == "response.output_audio.delta" and resp.get("delta") and stream_sid:
                    # Telnyx accepte le µ-law brut en sortie (testé : voix
                    # cristalline avant qu'on ne wrap en RTP). On forward
                    # directement le delta OpenAI sans toucher.
                    if not await _safe_send(client_ws, {"event": "media", "streamSid": stream_sid,
                                                        "media": {"payload": resp["delta"]}}):
                        return

                # --- Nouvelle réponse OpenAI démarre ---
                elif t == "response.created":
                    # On garde l'ID pour pouvoir l'annuler si le client interrompt.
                    current_response_id = resp.get("response", {}).get("id")

                # --- Tracking du dernier item audio (pour le barge-in) ---
                elif resp.get("item_id") and resp["item_id"] != last_item:
                    last_item = resp["item_id"]

                # --- Barge-in : le client parle alors que MIA parle ---
                elif t == "input_audio_buffer.speech_started" and last_item:
                    # Coupure immédiate : on annule la réponse OpenAI ET on vide
                    # le buffer audio Telnyx (sinon le reste de la phrase de MIA
                    # déjà bufferisé continuerait de jouer).
                    if current_response_id:
                        await openai_ws.send(json.dumps({"type": "response.cancel",
                                                         "response_id": current_response_id}))
                    if stream_sid:
                        await _safe_send(client_ws, {"event": "clear", "streamSid": stream_sid})
                    last_item = None
                    current_response_id = None

                # --- Fin de réponse : exécution éventuelle des function calls ---
                elif t == "response.done":
                    last_item = None
                    current_response_id = None
                    output = resp.get("response", {}).get("output", [])
                    had_calls = False  # Distingue les réponses-tool des réponses-parole

                    for item in output:
                        if item.get("type") != "function_call":
                            continue
                        had_calls = True
                        try:
                            args = json.loads(item.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}

                        # Log de la décision de MIA avant exécution
                        conv.info("[%s] 🔧 Tool   : %s(%s)", cid, item.get("name"), json.dumps(args, ensure_ascii=False))

                        # asyncio.to_thread : le tool fait du SQL synchrone + HTTP SMS.
                        # On le déporte sur un thread pour ne pas bloquer la boucle
                        # asyncio qui sert l'audio en parallèle.
                        result = await asyncio.to_thread(
                            execute_tool_call, restaurant["id"], item.get("name"), args,
                            menu=menu, quota_reservations=restaurant.get("quota_reservations"),
                            quota_commandes=restaurant.get("quota_commandes"),
                            restaurant_phone=restaurant.get("telephone"),
                            caller_phone=caller_phone or None,
                            restaurant_name=restaurant.get("nom", ""))
                        conv.info("[%s] ✓ Result : success=%s%s", cid, result.get("success"),
                                  f" code={result['code']}" if result.get("code") else "")

                        # Flag transfert : on l'exécutera APRÈS que MIA ait fini de
                        # dire « je vous transfère, un instant » (cf. plus bas).
                        if result.get("transfer"):
                            pending_transfer = True

                        # Renvoie le résultat à OpenAI → MIA le lit au client.
                        await openai_ws.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": item.get("call_id"),
                            "output": json.dumps(result)}}))
                        await openai_ws.send(json.dumps({"type": "response.create"}))

                    # Transfert effectif : on attend ici la fin de la réponse-parole
                    # qui SUIT le function_call (pas la réponse du function_call).
                    # had_calls=False → c'est bien la réponse vocale, pas le tool.
                    if pending_transfer and not had_calls:
                        # 3 secondes pour laisser jouer la fin de l'audio bufferisé.
                        await asyncio.sleep(3)
                        if call_control_id and restaurant.get("telephone"):
                            await asyncio.to_thread(telnyx_transfer, call_control_id, restaurant["telephone"])
                        log.info("[%s] Transfert effectué", cid)
                        return

        # Lance les 3 tâches en parallèle. Si une tombe (déconnexion, erreur),
        # gather propage l'exception et on nettoie.
        try:
            await asyncio.gather(send_greeting(), receive_from_client(), send_to_client())
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("[%s] Erreur bridge : %s", cid, e)
