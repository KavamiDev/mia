"""Helpers pour tester le bridge audio sans appeler OpenAI/Telnyx réels.

Deux outils principaux :
  1. wav_to_telnyx_chunks() — convertit un .wav µ-law 8 kHz mono en la
     séquence exacte de paquets WebSocket que Telnyx enverrait
     (1 paquet RTP/20 ms, base64 du contenu RTP complet).
  2. MockOpenAIWebSocket — substitut de websockets.WebSocketClientProtocol.
     Enregistre tout ce qu'on lui envoie + peut être programmé pour rejouer
     des events OpenAI prédéfinis (audio out, function_call, etc.).

Pourquoi : permettre des tests E2E déterministes (ne touche pas le réseau,
pas de coût OpenAI, reproductibles en CI) qui prouvent que le bridge fait
ce qu'on attend sur des cas concrets (parser RTP, garde double-confirm,
flush du transcript en DB).
"""
from __future__ import annotations

import base64
import json
import struct
from typing import Any


# ─────────────────────────────────────────
# Lecture WAV µ-law (format code 7)
# ─────────────────────────────────────────


def read_ulaw_wav(path: str) -> bytes:
    """Lit un .wav µ-law mono 8 kHz et retourne la payload µ-law brute.

    Format attendu :
      - RIFF / WAVE
      - fmt code = 7 (µ-law)
      - 1 channel, 8000 Hz, 8 bits/sample

    Lève ValueError si le format ne correspond pas.
    """
    with open(path, "rb") as f:
        data = f.read()

    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path} n'est pas un fichier WAV valide")

    # Cherche le chunk "fmt " — parsing minimal mais robuste aux chunks parasites
    pos = 12
    fmt_chunk = None
    data_chunk = None
    while pos < len(data) - 8:
        chunk_id = data[pos:pos + 4]
        chunk_size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if chunk_id == b"fmt ":
            fmt_chunk = data[pos + 8:pos + 8 + chunk_size]
        elif chunk_id == b"data":
            data_chunk = data[pos + 8:pos + 8 + chunk_size]
            break  # ce qui suit n'est pas notre problème
        pos += 8 + chunk_size + (chunk_size % 2)  # padding align 2

    if not fmt_chunk or not data_chunk:
        raise ValueError(f"{path} : fmt ou data chunk introuvable")

    fmt_code, channels, sample_rate, _byte_rate, _block_align, bits = struct.unpack(
        "<HHIIHH", fmt_chunk[:16])
    if fmt_code != 7:
        raise ValueError(f"{path} : format code {fmt_code} (attendu 7=µ-law)")
    if channels != 1 or sample_rate != 8000 or bits != 8:
        raise ValueError(
            f"{path} : config {channels}ch/{sample_rate}Hz/{bits}b "
            "(attendu mono 8 kHz 8 bits)"
        )

    return data_chunk


# ─────────────────────────────────────────
# Construction de paquets RTP (comme Telnyx)
# ─────────────────────────────────────────


def make_rtp_packet(payload_ulaw: bytes, seq: int = 0, timestamp: int = 0,
                    ssrc: int = 0x12345678) -> bytes:
    """Construit un paquet RTP minimal (12 bytes header + payload µ-law).

    Format header (RFC 3550) :
      byte 0 : V=2 P=0 X=0 CC=0 = 0x80
      byte 1 : M=0 PT=0 = 0x00 (PCMU)
      bytes 2-3 : sequence (big-endian)
      bytes 4-7 : timestamp
      bytes 8-11 : SSRC
    """
    header = struct.pack(
        ">BBHII",
        0x80,           # V=2, P=0, X=0, CC=0
        0x00,           # M=0, PT=0 (PCMU)
        seq & 0xFFFF,
        timestamp & 0xFFFFFFFF,
        ssrc & 0xFFFFFFFF,
    )
    return header + payload_ulaw


def wav_to_telnyx_chunks(path: str, frame_ms: int = 20) -> list[str]:
    """Convertit un .wav µ-law en liste de messages Telnyx au format JSON string.

    Chaque chunk simule une frame audio reçue de Telnyx : `event=media` avec
    `media.payload` = base64(RTP packet de 20 ms).

    Le test peut alors faire :
        for chunk in wav_to_telnyx_chunks("hello.wav"):
            await client_ws.send_text(chunk)
    """
    samples_per_frame = 8 * frame_ms  # 8 kHz × ms / 1000 = bytes (1 sample = 1 byte µ-law)
    ulaw = read_ulaw_wav(path)
    chunks = []
    seq = 0
    for i in range(0, len(ulaw), samples_per_frame):
        frame = ulaw[i:i + samples_per_frame]
        if len(frame) < samples_per_frame:
            # On laisse le dernier paquet partiel — Telnyx fait pareil
            pass
        rtp = make_rtp_packet(frame, seq=seq, timestamp=seq * samples_per_frame)
        chunks.append(json.dumps({
            "event": "media",
            "media": {"track": "inbound", "payload": base64.b64encode(rtp).decode()},
        }))
        seq += 1
    return chunks


# ─────────────────────────────────────────
# Mock OpenAI WebSocket
# ─────────────────────────────────────────


class MockOpenAIWebSocket:
    """Substitut de l'objet `websockets.WebSocketClientProtocol`.

    Usage :
        mock = MockOpenAIWebSocket()
        mock.queue_event({"type": "response.output_audio_transcript.done",
                          "transcript": "Bonjour, je vous écoute"})
        # Plug dans le bridge à la place de openai_ws
        ...
        # Vérifier ce que le bridge a envoyé à OpenAI
        sent = mock.get_sent()
        assert any('input_audio_buffer.append' in s for s in sent)
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._queue: list[dict] = []
        self._closed = False

    def queue_event(self, event: dict) -> None:
        """Ajoute un event que le bridge recevra via `async for msg in openai_ws`."""
        self._queue.append(event)

    async def send(self, data: str) -> None:
        """Capturé : ce que le bridge envoie à OpenAI."""
        if self._closed:
            raise RuntimeError("WebSocket fermé")
        self.sent.append(data)

    def __aiter__(self) -> "MockOpenAIWebSocket":
        return self

    async def __anext__(self) -> str:
        """Délivre les events queue_event() dans l'ordre, puis StopAsyncIteration."""
        if not self._queue:
            self._closed = True
            raise StopAsyncIteration
        return json.dumps(self._queue.pop(0))

    async def __aenter__(self) -> "MockOpenAIWebSocket":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self._closed = True

    def get_sent_types(self) -> list[str]:
        """Liste des types d'events envoyés (pour assertions concises)."""
        types = []
        for msg in self.sent:
            try:
                types.append(json.loads(msg).get("type", ""))
            except Exception:
                types.append("")
        return types

    def get_sent_audio_count(self) -> int:
        """Nombre de chunks audio envoyés (input_audio_buffer.append)."""
        return sum(1 for t in self.get_sent_types() if t == "input_audio_buffer.append")
