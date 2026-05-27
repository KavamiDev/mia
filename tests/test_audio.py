"""Tests du parser RTP et du dumper WAV.

Le parser RTP est CRITIQUE : un bug ici → bytes parasites en début d'audio
→ hallucinations OpenAI. On couvre les 4 variantes du header (RFC 3550) :
minimal, CSRC, extension, padding.
"""
import os
import struct
import tempfile

from app.services.audio_debug import AudioDumper, amplify_ulaw, strip_rtp_header


# ─────────────────────────────────────────
# Parser RTP (RFC 3550)
# ─────────────────────────────────────────


def _make_rtp(payload: bytes, *, cc: int = 0, x: int = 0, p: int = 0,
              ext_data: bytes = b"", padding_count: int = 0) -> bytes:
    """Forge un paquet RTP de test.

    cc : nombre de CSRC (0-15)
    x  : bit extension (0/1)
    p  : bit padding (0/1)
    """
    byte0 = (2 << 6) | (p << 5) | (x << 4) | (cc & 0xF)
    header = bytes([byte0, 0x00, 0x00, 0x01]) + bytes(8)  # 12 bytes minimum
    header += bytes(4 * cc)  # CSRC list
    if x:
        # Extension header : 2 bytes profile + 2 bytes length (en words de 4)
        ext_len_words = len(ext_data) // 4
        header += b"\x00\x00" + ext_len_words.to_bytes(2, "big") + ext_data
    pkt = header + payload
    if p:
        # padding : N-1 bytes + un byte count
        pkt += b"\x00" * (padding_count - 1) + bytes([padding_count])
    return pkt


def test_strip_minimal_header():
    """Header simple 12 bytes, payload 160 bytes (20ms d'audio à 8 kHz)."""
    payload = bytes(range(160))
    pkt = _make_rtp(payload)
    assert strip_rtp_header(pkt) == payload


def test_strip_with_csrc():
    """Header + 2 CSRC = 12 + 8 = 20 bytes à dégager."""
    payload = bytes(range(100))
    pkt = _make_rtp(payload, cc=2)
    assert strip_rtp_header(pkt) == payload


def test_strip_with_max_csrc():
    """Max CSRC count = 15 → header = 12 + 60 = 72 bytes."""
    payload = bytes(range(80))
    pkt = _make_rtp(payload, cc=15)
    assert strip_rtp_header(pkt) == payload


def test_strip_with_extension():
    """Extension header présent (X=1) avec 2 words de data."""
    payload = bytes(range(160))
    pkt = _make_rtp(payload, x=1, ext_data=bytes(8))
    assert strip_rtp_header(pkt) == payload


def test_strip_with_padding():
    """Padding (P=1) : 3 bytes à retirer en fin de payload."""
    payload = bytes(range(160))
    pkt = _make_rtp(payload, p=1, padding_count=3)
    assert strip_rtp_header(pkt) == payload


def test_strip_combined_csrc_extension_padding():
    """Cas pire : CSRC=2 + extension + padding tous activés."""
    payload = bytes(range(120))
    pkt = _make_rtp(payload, cc=2, x=1, ext_data=bytes(4), p=1, padding_count=2)
    assert strip_rtp_header(pkt) == payload


def test_strip_non_rtp_passthrough():
    """Si version != 2 (pas un RTP), on retourne tel quel.

    Cas : Telnyx pourrait envoyer du raw µ-law si stream_bidirectional_mode
    n'est pas configuré sur "rtp". On ne veut pas perdre l'audio.
    """
    fake_raw = bytes(180)  # tous à 0 → version = 0
    assert strip_rtp_header(fake_raw) == fake_raw


def test_strip_packet_too_short():
    """Paquet < 12 bytes : retour brut (corruption ou keep-alive)."""
    short = bytes(8)
    assert strip_rtp_header(short) == short


def test_strip_empty_packet():
    """Paquet vide : retour vide, pas de crash."""
    assert strip_rtp_header(b"") == b""


def test_strip_corrupted_header_extension():
    """Extension annoncée plus longue que le paquet → retour brut sans crash."""
    # CC=0, X=1, mais le paquet est trop court pour contenir l'extension annoncée
    header = bytes([0x90, 0x00, 0x00, 0x01]) + bytes(8)  # 12 bytes
    # Extension : 2 bytes profile + 2 bytes length = "9999 words" qui dépassent
    header += b"\x00\x00\xFF\xFF"
    # Pas de payload après — extension annoncée = 65535*4 bytes, on n'en a pas
    result = strip_rtp_header(header)
    # On doit retourner sans crash. Le contenu retourné peut être vide.
    assert isinstance(result, bytes)


# ─────────────────────────────────────────
# AudioDumper (génération WAV µ-law)
# ─────────────────────────────────────────


def test_dumper_writes_valid_wav():
    """Le fichier produit doit avoir un header WAV µ-law valide."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d = AudioDumper("test123", tmpdir, max_seconds=1)
        d.write(bytes([0x7F] * 4000))  # 500ms à 8 kHz
        d.close()

        files = os.listdir(tmpdir)
        assert len(files) == 1
        path = os.path.join(tmpdir, files[0])

        with open(path, "rb") as f:
            header = f.read(44)

        # RIFF/WAVE magic
        assert header[:4] == b"RIFF"
        assert header[8:12] == b"WAVE"
        # Format code 7 = µ-law (au lieu de 1 = PCM)
        fmt_code = struct.unpack("<H", header[20:22])[0]
        assert fmt_code == 7, f"format code should be 7 (mu-law), got {fmt_code}"
        # 1 canal mono
        channels = struct.unpack("<H", header[22:24])[0]
        assert channels == 1
        # 8000 Hz
        sr = struct.unpack("<I", header[24:28])[0]
        assert sr == 8000


def test_dumper_respects_max_seconds():
    """Au-delà de max_seconds, les écritures sont des no-ops."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d = AudioDumper("limit", tmpdir, max_seconds=1)  # 8000 bytes max
        d.write(bytes(5000))   # 5000 bytes
        d.write(bytes(5000))   # ajouterait 5000 → 10000, mais cappé à 8000
        d.close()

        path = os.path.join(tmpdir, os.listdir(tmpdir)[0])
        size = os.path.getsize(path)
        # WAV header ~46 bytes + 8000 bytes de payload
        assert 8040 <= size <= 8100, f"taille inattendue : {size}"


def test_dumper_no_op_if_dir_invalid():
    """Si le dossier ne peut pas être créé, le dumper ne crash pas."""
    # /dev/null/foo ne peut pas avoir de sous-dossier
    d = AudioDumper("test", "/dev/null/foo", max_seconds=1)
    d.write(bytes(1000))  # ne doit pas crasher
    d.close()              # ne doit pas crasher


# ─────────────────────────────────────────
# amplify_ulaw — augmente le niveau du signal entrant
# ─────────────────────────────────────────


def test_amplify_gain_1_returns_input():
    """Gain=1.0 → aucune modification (no-op)."""
    data = bytes(range(100))
    assert amplify_ulaw(data, gain=1.0) == data


def test_amplify_empty_returns_empty():
    assert amplify_ulaw(b"", gain=5.0) == b""


def test_amplify_increases_signal_amplitude():
    """Un signal faible doit avoir une amplitude supérieure après gain x5."""
    import audioop

    # Génère 100 ms de signal faible (sinus PCM16 → µ-law)
    import math
    pcm = b"".join(int(2000 * math.sin(i * 0.1)).to_bytes(2, "little", signed=True)
                   for i in range(800))
    ulaw = audioop.lin2ulaw(pcm, 2)

    amplified = amplify_ulaw(ulaw, gain=5.0)
    # Re-convertir pour comparer les amplitudes
    pcm_after = audioop.ulaw2lin(amplified, 2)
    rms_before = audioop.rms(pcm, 2)
    rms_after = audioop.rms(pcm_after, 2)
    # Le signal amplifié doit être au moins 2× plus fort (compromis µ-law non linéaire)
    assert rms_after > rms_before * 2, f"rms_before={rms_before}, rms_after={rms_after}"


def test_amplify_overflow_handled_gracefully():
    """Gain énorme + signal fort → pas de crash, retourne audio valide."""
    import audioop
    # Signal déjà fort
    pcm = b"\xff\x7f" * 800  # max PCM16
    ulaw = audioop.lin2ulaw(pcm, 2)
    result = amplify_ulaw(ulaw, gain=100.0)
    # Doit retourner des bytes valides (soit amplifié soit unchanged)
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_dumper_close_idempotent():
    """close() peut être appelée plusieurs fois sans effet de bord."""
    with tempfile.TemporaryDirectory() as tmpdir:
        d = AudioDumper("idem", tmpdir, max_seconds=1)
        d.write(bytes(100))
        d.close()
        d.close()  # ne doit pas crasher ni dupliquer le fichier
        assert len(os.listdir(tmpdir)) == 1
