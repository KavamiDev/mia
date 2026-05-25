"""Tests E2E du bridge audio sans toucher OpenAI ni Telnyx réels.

Utilise les helpers tests/audio_helpers.py :
  - wav_to_telnyx_chunks pour simuler le flux Telnyx
  - MockOpenAIWebSocket pour intercepter ce que le bridge envoie à OpenAI

Si tu ajoutes des wavs réels dans tests/audio_fixtures/, complète ce module
avec des scénarios concrets (« bonjour seul → MIA ne crée pas de résa »,
« réservation complète + oui → CallLog avec resa code », etc.).
"""
import os
import tempfile

from tests.audio_helpers import (
    MockOpenAIWebSocket,
    make_rtp_packet,
    wav_to_telnyx_chunks,
)


def _make_synthetic_ulaw_wav(path: str, duration_ms: int = 200) -> None:
    """Crée un .wav µ-law mono 8 kHz contenant N ms de silence (0x7F = silence µ-law).

    Suffit pour valider la pipeline (parser RTP, dump, etc.) sans avoir une
    vraie voix. Pour des tests sémantiques (« MIA comprend X »), il faut
    capturer des wavs réels via AUDIO_DEBUG_DIR sur le serveur.
    """
    from app.services.audio_debug import _build_ulaw_wav

    n_samples = 8 * duration_ms  # 8 kHz × ms / 1000
    silence = bytes([0x7F] * n_samples)
    wav = _build_ulaw_wav(silence, sample_rate=8000)
    with open(path, "wb") as f:
        f.write(wav)


# ─────────────────────────────────────────
# wav_to_telnyx_chunks
# ─────────────────────────────────────────


def test_wav_to_chunks_produces_expected_frame_count():
    """200 ms de wav à 20 ms par frame → 10 chunks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = os.path.join(tmpdir, "test.wav")
        _make_synthetic_ulaw_wav(wav_path, duration_ms=200)

        chunks = wav_to_telnyx_chunks(wav_path, frame_ms=20)
        assert len(chunks) == 10


def test_chunks_format_matches_telnyx():
    """Chaque chunk est un JSON {event: media, media: {track, payload}}."""
    import json

    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = os.path.join(tmpdir, "test.wav")
        _make_synthetic_ulaw_wav(wav_path, duration_ms=40)

        chunks = wav_to_telnyx_chunks(wav_path, frame_ms=20)
        for chunk in chunks:
            data = json.loads(chunk)
            assert data["event"] == "media"
            assert data["media"]["track"] == "inbound"
            # payload = base64 d'un RTP packet (12 bytes header + 160 bytes audio)
            import base64
            payload = base64.b64decode(data["media"]["payload"])
            assert len(payload) == 12 + 160


def test_chunks_decode_back_to_audio_via_rtp_parser():
    """End-to-end : chunk → parser RTP → audio µ-law.

    Vérifie qu'on récupère bien la payload µ-law brute après strip.
    """
    import base64
    import json

    from app.services.audio_debug import strip_rtp_header

    with tempfile.TemporaryDirectory() as tmpdir:
        wav_path = os.path.join(tmpdir, "test.wav")
        _make_synthetic_ulaw_wav(wav_path, duration_ms=40)

        chunks = wav_to_telnyx_chunks(wav_path, frame_ms=20)
        reconstructed = b""
        for chunk in chunks:
            data = json.loads(chunk)
            rtp = base64.b64decode(data["media"]["payload"])
            reconstructed += strip_rtp_header(rtp)

        # On a injecté du silence (0x7F) → on doit récupérer le même
        assert all(b == 0x7F for b in reconstructed)


# ─────────────────────────────────────────
# MockOpenAIWebSocket : sanity checks
# ─────────────────────────────────────────


def test_mock_records_sent_messages():
    """Tout ce qu'on envoie via send() est conservé pour assertion."""
    import asyncio

    mock = MockOpenAIWebSocket()

    async def run():
        await mock.send('{"type": "session.update"}')
        await mock.send('{"type": "input_audio_buffer.append"}')

    asyncio.run(run())
    assert len(mock.sent) == 2
    assert "session.update" in mock.sent[0]


def test_mock_delivers_queued_events():
    """Les events queue_event() sont délivrés par async iteration."""
    import asyncio

    mock = MockOpenAIWebSocket()
    mock.queue_event({"type": "session.created"})
    mock.queue_event({"type": "response.done"})

    received = []

    async def run():
        async for msg in mock:
            received.append(msg)

    asyncio.run(run())
    assert len(received) == 2
    assert "session.created" in received[0]


def test_mock_counts_audio_chunks():
    """get_sent_audio_count() compte uniquement les input_audio_buffer.append."""
    import asyncio
    import json

    mock = MockOpenAIWebSocket()

    async def run():
        await mock.send(json.dumps({"type": "session.update"}))
        for _ in range(5):
            await mock.send(json.dumps({"type": "input_audio_buffer.append", "audio": "..."}))
        await mock.send(json.dumps({"type": "response.create"}))

    asyncio.run(run())
    assert mock.get_sent_audio_count() == 5


# ─────────────────────────────────────────
# RTP forge cohérent avec le parser
# ─────────────────────────────────────────


def test_make_rtp_packet_is_strippable():
    """make_rtp_packet + strip_rtp_header = round-trip parfait."""
    from app.services.audio_debug import strip_rtp_header

    payload = bytes(range(160))
    pkt = make_rtp_packet(payload, seq=42, timestamp=1234)
    assert len(pkt) == 12 + 160
    assert strip_rtp_header(pkt) == payload
