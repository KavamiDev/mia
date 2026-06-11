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
from app.services.audio_debug import (AudioDumper, amplify_ulaw, normalize_ulaw,
                                       strip_rtp_header)
from app.services.call_log_service import CallTranscript
from app.services.sms_service import send_sms
from app.services.telnyx_service import telnyx_transfer
from app.services.tool_service import execute_tool_call
from app.utils.confirmation import expects_short_reply, is_confirmed

# Tools qui modifient l'état (DB + SMS) — exigent une confirmation orale
# du client juste avant. Sinon : on bloque pour éviter les SAV.
_CONFIRMED_TOOLS = ("create_reservation", "create_commande")

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


# Réfs fortes vers les tâches SMS d'arrière-plan : sans elles, asyncio peut
# garbage-collecter une tâche en cours (fire-and-forget classique).
_sms_tasks: set[asyncio.Task] = set()


def _spawn_sms_tasks(outbox: list[tuple[str, str]], cid: str) -> None:
    """Envoie les SMS en arrière-plan, HORS du chemin critique vocal.

    Avant : le bridge attendait DB + 2 requêtes HTTP SMS (timeout 15s chacune)
    avant de renvoyer le function_call_output → le client restait en silence
    après son « oui ». Maintenant le récap vocal part immédiatement et les SMS
    suivent en parallèle (best-effort, comme avant : un SMS qui échoue ne
    remet pas en cause la réservation).
    """
    def _log_result(task: asyncio.Task) -> None:
        _sms_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.warning("[%s] SMS arrière-plan échoué : %s", cid, exc)

    for to, body in outbox:
        task = asyncio.create_task(asyncio.to_thread(send_sms, to, body),
                                   name=f"sms-{cid}")
        _sms_tasks.add(task)
        task.add_done_callback(_log_result)


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


def _vad_config(silence_ms: int) -> dict:
    """Bloc turn_detection complet pour session.update.

    Centralisé car envoyé deux fois : à l'init de session ET en cours d'appel
    par le VAD adaptatif (silence réduit quand une réponse courte est attendue).
    """
    return {"type": "server_vad", "threshold": settings.vad_threshold,
            "prefix_padding_ms": settings.vad_prefix_padding_ms,
            "silence_duration_ms": silence_ms,
            "create_response": True}


async def _connect_openai(cid: str, ssl_ctx, *, attempts: int | None = None,
                          base_delay: float = 0.5):
    """Connexion WSS OpenAI Realtime avec retry + backoff exponentiel.

    Un blip réseau/DNS transitoire au décrochage ne doit pas faire perdre
    l'appel : on retente (0.5s puis 1s par défaut). Le client entend au pire
    1-2 secondes de silence en plus avant le greeting.

    Returns:
        La connexion ouverte, ou None si toutes les tentatives ont échoué
        (l'appelant doit alors terminer l'appel proprement).
    """
    attempts = max(attempts if attempts is not None else settings.openai_connect_attempts, 1)
    for attempt in range(1, attempts + 1):
        try:
            return await websockets.connect(
                f"wss://api.openai.com/v1/realtime?model={settings.openai_realtime_model}",
                additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                ssl=ssl_ctx,
                open_timeout=10,
            )
        except Exception as e:
            if attempt == attempts:
                log.error("[%s] Connexion OpenAI échouée après %d tentatives : %s",
                          cid, attempts, e)
                return None
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("[%s] Connexion OpenAI %d/%d échouée (%s) — retry dans %.1fs",
                        cid, attempt, attempts, e, delay)
            await asyncio.sleep(delay)
    return None


async def _init_session(openai_ws, instructions: str, transcription_prompt: str = "") -> None:
    """Configure la session OpenAI : audio PCMU, VAD serveur, transcription FR, tools.

    PCMU (G.711 µ-law) est le codec téléphonique standard utilisé par
    Telnyx — on évite ainsi un transcodage côté serveur.

    `server_vad` : c'est OpenAI qui détecte la fin de la parole côté
    utilisateur, déclenche automatiquement une réponse (create_response=True).

    Transcription : modèle configurable (settings.openai_transcription_model).
    Le `prompt` (bias lexical) est OPTIONNEL — on a observé en prod que biaiser
    avec la liste des plats faisait halluciner des noms de plats sur audio
    bruité (« Pizza Végétarienne » au lieu de « 4 personnes »). On laisse
    la transcription FR native faire son boulot sans bias polluant.
    """
    transcription_cfg = {
        "model": settings.openai_transcription_model,
        "language": "fr",
    }
    # Bias prompt seulement si très court (vocabulaire métier sans noms propres).
    # Si plus de 200 chars : on skip pour ne pas polluer.
    if transcription_prompt and len(transcription_prompt) <= 200:
        transcription_cfg["prompt"] = transcription_prompt

    await openai_ws.send(json.dumps({"type": "session.update", "session": {
        "type": "realtime", "model": settings.openai_realtime_model,
        "output_modalities": ["audio"], "instructions": instructions,
        "audio": {
            "input": {
                "format": {"type": "audio/pcmu"},
                "transcription": transcription_cfg,
                "turn_detection": _vad_config(settings.vad_silence_duration_ms)},
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

    # PAS de bias prompt : depuis qu'on amplifie le signal (gain x5), le
    # modèle n'a plus besoin de "compléter" l'audio. Tout bias devient une
    # source d'hallucination. On laisse la transcription FR native faire.
    nom = restaurant.get("nom", "le restaurant")
    transcription_prompt = ""

    # Greeting : éviter "chez Chez Marco" si le nom commence déjà par "Chez/Au/Le/La".
    nom_prefixe = nom.lower().split(" ", 1)[0] if nom else ""
    chez = "" if nom_prefixe in ("chez", "au", "aux", "le", "la", "les", "l'") else "chez "
    greeting = f"Salue le client : 'Bonjour, bienvenue {chez}{nom}, MIA à l'appareil.'"

    # certifi fournit les CA Mozilla — nécessaire dans certains conteneurs où
    # le bundle CA système n'est pas à jour.
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    log.info("[%s] Connexion OpenAI Realtime...", cid)

    openai_ws = await _connect_openai(cid, ssl_ctx)
    if openai_ws is None:
        # Sans OpenAI, MIA est muette : on termine — l'appelant (webhook)
        # fermera la socket Telnyx et le client retombera sur la messagerie.
        return
    async with openai_ws:
        await _init_session(openai_ws, instructions, transcription_prompt)
        log.info("[%s] OpenAI connecté — %s (%d plats)", cid, restaurant.get("nom"), len(menu))

        # ─── État partagé entre les 3 tâches ───
        stream_sid = None                      # ID du stream Telnyx (reçu dans 'start')
        stream_ready = asyncio.Event()         # Set quand 'start' reçu → débloque le greeting
        last_item = None                       # Dernier item audio en cours de lecture
        current_response_id = None             # ID de la réponse OpenAI active (pour cancel)
        pending_transfer = False               # True si on doit transférer après le goodbye
        last_client_text = ""                  # Dernière transcription du client (pour confirmation)
        current_vad_silence = settings.vad_silence_duration_ms  # VAD adaptatif (cf. plus bas)

        # Dumper audio optionnel (debug du flux entrant). Activé via env
        # AUDIO_DEBUG_DIR. Écrit un .wav µ-law avec les 5 premières secondes
        # d'audio entrant pour valider à l'oreille que le décodage RTP est OK.
        audio_dumper = (AudioDumper(cid, settings.audio_debug_dir, max_seconds=5)
                        if settings.audio_debug_dir else None)

        # Buffer transcript pour le dashboard SAV. Flushé en DB à la fin.
        # Si l'appel échoue avant le moindre échange, flush() est un no-op.
        call_log = CallTranscript(
            restaurant_id=restaurant.get("id"),
            caller_phone=caller_phone,
            call_control_id=call_control_id,
        )

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
                            # Telnyx (mode=rtp) envoie : [RTP header variable][µ-law payload].
                            # Le header fait 12 bytes minimum mais peut être plus
                            # gros (CSRC, extension). On utilise un parser RFC 3550
                            # robuste pour ne pas laisser de bytes parasites en
                            # début de signal (cause d'hallucinations OpenAI).
                            rtp_pkt = base64.b64decode(media["payload"])
                            raw_ulaw = strip_rtp_header(rtp_pkt)
                            # Amplification audio :
                            #   - Si AUDIO_INPUT_GAIN > 0 : gain fixe LEGACY (debug)
                            #   - Sinon : AGC dynamique vers AUDIO_TARGET_RMS
                            # L'AGC évite à la fois la sous-amplification (modèle
                            # hallucine) ET la saturation (modèle parle russe).
                            if settings.audio_input_gain > 0:
                                raw_ulaw = amplify_ulaw(raw_ulaw, settings.audio_input_gain)
                            else:
                                raw_ulaw = normalize_ulaw(
                                    raw_ulaw,
                                    target_rms=settings.audio_target_rms,
                                    max_gain=settings.audio_max_gain,
                                )
                            if audio_dumper:
                                audio_dumper.write(raw_ulaw)
                            await openai_ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(raw_ulaw).decode()}))
                    elif ev == "start":
                        # Le stream_sid sert à cibler nos messages sortants vers Telnyx.
                        stream_sid = data.get("stream_id") or data.get("start", {}).get("streamSid")
                        log.info("[%s] Stream démarré : %s", cid, stream_sid)
                        stream_ready.set()
                    elif ev == "stop":
                        # 🔴 CRITIQUE : le client a raccroché. Telnyx envoie cet event.
                        # Sans le détecter, le bridge attendait jusqu'au timeout 600s
                        # = facturait ~3$ d'OpenAI Realtime POUR RIEN par appel raccroché.
                        log.info("[%s] Event 'stop' reçu de Telnyx — appel raccroché", cid)
                        return
            except WebSocketDisconnect:
                log.info("[%s] Client déconnecté (WS close)", cid)

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
            nonlocal last_item, current_response_id, pending_transfer, last_client_text
            nonlocal current_vad_silence
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
                        last_client_text = transcript
                        call_log.add_client(transcript)

                # --- Transcription de MIA (ce qu'elle a dit) ---
                # Émis quand MIA a fini de générer une réponse audio.
                if t == "response.output_audio_transcript.done":
                    transcript = (resp.get("transcript") or "").strip()
                    if transcript:
                        conv.info("[%s] 🤖 MIA    : %s", cid, transcript)
                        call_log.add_mia(transcript)

                        # ─── VAD ADAPTATIF ───
                        # Si MIA vient de poser une question de validation
                        # (« Je valide ? »), la réponse attendue est courte
                        # (« oui ») : on abaisse le silence VAD pour ce tour
                        # (~-400ms de latence sur le tour le plus critique).
                        # Sinon on restaure la valeur par défaut. La règle est
                        # ré-évaluée à CHAQUE tour de MIA → auto-corrective.
                        target_ms = (settings.vad_confirmation_silence_ms
                                     if expects_short_reply(transcript)
                                     else settings.vad_silence_duration_ms)
                        if target_ms > 0 and target_ms != current_vad_silence:
                            current_vad_silence = target_ms
                            await openai_ws.send(json.dumps({
                                "type": "session.update", "session": {
                                    "type": "realtime",
                                    "audio": {"input": {
                                        "turn_detection": _vad_config(target_ms)}}}}))
                            log.info("[%s] VAD silence → %dms", cid, target_ms)

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
                        tool_name = item.get("name", "")
                        try:
                            args = json.loads(item.get("arguments", "{}"))
                        except json.JSONDecodeError:
                            args = {}

                        # Log de la décision de MIA avant exécution
                        conv.info("[%s] 🔧 Tool   : %s(%s)", cid, tool_name, json.dumps(args, ensure_ascii=False))

                        # ─── GARDE DOUBLE-CONFIRMATION ───
                        # Pour create_reservation/create_commande, on exige que la
                        # dernière transcription client contienne un mot de validation
                        # explicite (« oui », « c'est bon », « validez »...).
                        # Sinon : on bloque, on log, et on renvoie un faux résultat à
                        # MIA qui lui dit de refaire son récap.
                        # → Évite les SAV quand la transcription hallucine un OUI.
                        if tool_name in _CONFIRMED_TOOLS and not is_confirmed(last_client_text):
                            blocked_reason = (
                                f"Pas de confirmation orale détectée dans la dernière "
                                f"transcription client : {last_client_text!r}"
                            )
                            conv.warning("[%s] ⛔ Tool BLOQUÉ (%s) : %s", cid, tool_name, blocked_reason)
                            call_log.add_tool(tool_name, args, {"success": False},
                                              blocked_reason=blocked_reason)
                            fake_result = {
                                "success": False,
                                "blocked": True,
                                "recap_vocal": (
                                    "Avant de valider, je dois être sûre. Refaites-moi le "
                                    "récap en disant clairement 'OUI' ou 'c'est bon' pour "
                                    "que je confirme."
                                ),
                            }
                            await openai_ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {"type": "function_call_output",
                                         "call_id": item.get("call_id"),
                                         "output": json.dumps(fake_result)}}))
                            await openai_ws.send(json.dumps({"type": "response.create"}))
                            continue  # passe au prochain item sans exécuter le tool

                        # asyncio.to_thread : le tool fait du SQL synchrone.
                        # On le déporte sur un thread pour ne pas bloquer la boucle
                        # asyncio qui sert l'audio en parallèle.
                        # defer_sms=True : les SMS ne sont PAS envoyés dans le
                        # tool (2 requêtes HTTP, timeout 15s chacune) mais
                        # récupérés dans sms_outbox et envoyés en arrière-plan
                        # APRÈS avoir débloqué la réponse vocale de MIA.
                        result = await asyncio.to_thread(
                            execute_tool_call, restaurant["id"], tool_name, args,
                            menu=menu, quota_reservations=restaurant.get("quota_reservations"),
                            quota_commandes=restaurant.get("quota_commandes"),
                            restaurant_phone=restaurant.get("telephone"),
                            caller_phone=caller_phone or None,
                            restaurant_name=restaurant.get("nom", ""),
                            sms_to_client=restaurant.get("sms_to_client", True),
                            sms_to_restaurant=restaurant.get("sms_to_restaurant", True),
                            defer_sms=True)
                        # Pop AVANT toute sérialisation : les numéros de téléphone
                        # ne doivent fuiter ni vers OpenAI ni dans le call_log.
                        sms_outbox = result.pop("sms_outbox", [])
                        conv.info("[%s] ✓ Result : success=%s%s", cid, result.get("success"),
                                  f" code={result['code']}" if result.get("code") else "")

                        # Enregistre dans le buffer transcript pour le dashboard SAV
                        call_log.add_tool(tool_name, args, result)

                        # Flag transfert : on l'exécutera APRÈS que MIA ait fini de
                        # dire « je vous transfère, un instant » (cf. plus bas).
                        if result.get("transfer"):
                            pending_transfer = True

                        # Renvoie le résultat à OpenAI → MIA le lit au client.
                        await openai_ws.send(json.dumps({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": item.get("call_id"),
                            "output": json.dumps(result)}}))
                        await openai_ws.send(json.dumps({"type": "response.create"}))

                        # SMS en parallèle de la réponse vocale (fire-and-forget).
                        if sms_outbox:
                            _spawn_sms_tasks(sms_outbox, cid)

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

        # 🔴 FIX CRITIQUE COÛTS : on utilise asyncio.wait(FIRST_COMPLETED) au lieu
        # de asyncio.gather. Dès QU'UNE tâche se termine (event 'stop' Telnyx,
        # WebSocketDisconnect, erreur OpenAI), on cancel IMMÉDIATEMENT les
        # autres tâches. Sinon le bridge continuait à tourner jusqu'au timeout
        # 600s en facturant OpenAI Realtime pour rien (~3$ par appel raccroché).
        tasks = [
            asyncio.create_task(send_greeting(), name=f"greet-{cid}"),
            asyncio.create_task(receive_from_client(), name=f"recv-{cid}"),
            asyncio.create_task(send_to_client(), name=f"send-{cid}"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # Cancel toutes les tâches restantes — l'appel est terminé
            for task in pending:
                task.cancel()
            # On attend brièvement leur fin pour libérer proprement les ressources
            if pending:
                await asyncio.wait(pending, timeout=2.0)
            log.info("[%s] Bridge terminé : %d done, %d cancelled", cid, len(done), len(pending))
            # Propager une éventuelle exception remontée par la tâche qui s'est terminée
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    log.warning("[%s] Tâche %s terminée avec exception : %s",
                                cid, task.get_name(), exc)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.exception("[%s] Erreur bridge : %s", cid, e)
        finally:
            # Force le flush du dump audio même si l'appel coupe avant les 5s.
            if audio_dumper:
                audio_dumper.close()
            # Persiste le transcript en DB pour le dashboard SAV. Asynchrone
            # pour ne pas bloquer le close du WebSocket (mais on attend la fin
            # quand même pour avoir un log propre dans les serveurs courts).
            try:
                await asyncio.to_thread(call_log.flush)
            except Exception as e:
                log.warning("[%s] Flush CallLog échec : %s", cid, e)
