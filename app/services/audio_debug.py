"""Outils audio : parser RTP robuste + dump WAV pour debug du flux entrant.

Pourquoi ce module existe :

  Telnyx (mode bidirectional_mode=rtp) envoie chaque paquet audio sous forme
  d'un datagramme RTP RFC 3550 encodé en base64. Le header RTP a une taille
  VARIABLE (12 bytes minimum + CSRC + extension + padding), pas fixe.

  Avant ce module, on faisait `pkt[12:]` aveuglément. Sur 99 % des paquets
  Telnyx ça marche (header minimal), mais dès qu'une extension ou un CSRC
  apparaît (ce qui peut arriver sur certains transferts SIP), on garde des
  bytes binaires parasites en début d'audio → OpenAI hallucine.

  Bonus : le dumper WAV permet de vérifier MANUELLEMENT que ce qu'on envoie
  à OpenAI est bien de la parole intelligible (le test "débile mais efficace").
"""
from __future__ import annotations

import audioop  # type: ignore[deprecated]
import logging
import struct
import time
from pathlib import Path

log = logging.getLogger("mia.audio")


# ──────────────────────────────────────────────────────────
# Amplification du signal entrant
# ──────────────────────────────────────────────────────────
#
# Diagnostic prod sur audio Telnyx +262 (Réunion) :
#   RMS=716, max amplitude=1884 / 32767 = 5.7% du max possible
#   → le modèle de transcription reçoit un signal très faible et
#     "remplit les blancs" avec des hallucinations plausibles.
#
# Fix : on amplifie le signal x5 par défaut avant envoi à OpenAI.
# Audio passe de ~6% à ~28% du max → niveau confortable pour la transcription.


def amplify_ulaw(ulaw_bytes: bytes, gain: float = 5.0) -> bytes:
    """Amplifie un signal µ-law en passant par PCM16.

    µ-law → PCM16 → multiplier par gain → re-µ-law

    audioop.mul peut overflow si gain trop élevé : on attrape et on
    baisse le gain progressivement plutôt que de cliper sauvagement.
    """
    if not ulaw_bytes or gain == 1.0:
        return ulaw_bytes
    try:
        pcm16 = audioop.ulaw2lin(ulaw_bytes, 2)
        amplified = audioop.mul(pcm16, 2, gain)
        return audioop.lin2ulaw(amplified, 2)
    except audioop.error:
        # Overflow : on retente avec un gain réduit
        try:
            pcm16 = audioop.ulaw2lin(ulaw_bytes, 2)
            amplified = audioop.mul(pcm16, 2, gain * 0.5)
            return audioop.lin2ulaw(amplified, 2)
        except Exception:
            # Si même le gain réduit échoue, on retourne tel quel
            return ulaw_bytes


# ──────────────────────────────────────────────────────────
# Parser RTP (RFC 3550)
# ──────────────────────────────────────────────────────────
#
# Structure du header RTP (12 bytes minimum) :
#
#    0                   1                   2                   3
#    0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |V=2|P|X|  CC   |M|     PT      |       sequence number         |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |                           timestamp                           |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |           synchronization source (SSRC) identifier            |
#   +=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+=+
#   |            contributing source (CSRC) identifiers ... (0-15)  |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |   extension header (présent ssi X=1)                          |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |   payload audio (µ-law dans notre cas)                        |
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
#   |   padding (présent ssi P=1, le dernier byte = nombre de bytes)|
#   +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+


def strip_rtp_header(pkt: bytes) -> bytes:
    """Retire le header RTP variable et retourne la payload audio brute.

    Si le paquet n'est pas un RTP valide (version != 2, trop court), on le
    retourne tel quel — Telnyx peut occasionnellement envoyer du raw µ-law
    (mode non-RTP) et on ne veut pas perdre l'audio.

    Returns:
        Les bytes µ-law purs, sans header ni padding.
    """
    if len(pkt) < 12:
        # Trop court pour être un header RTP → probablement déjà du raw µ-law
        return pkt

    byte0 = pkt[0]
    version = (byte0 >> 6) & 0x3
    if version != 2:
        # Pas un RTP RFC 3550 → considérer comme raw µ-law (Telnyx peut le faire
        # si stream_bidirectional_mode est passé à autre chose que "rtp")
        return pkt

    padding = (byte0 >> 5) & 0x1
    extension = (byte0 >> 4) & 0x1
    cc = byte0 & 0xF  # CSRC count : 0 à 15

    header_len = 12 + 4 * cc

    # Header d'extension RTP : 4 bytes (profile + length en words de 4 bytes)
    # suivi de length × 4 bytes de données d'extension.
    if extension and len(pkt) >= header_len + 4:
        ext_len_words = int.from_bytes(pkt[header_len + 2:header_len + 4], "big")
        header_len += 4 + 4 * ext_len_words

    if header_len >= len(pkt):
        # Header annoncé plus grand que le paquet → corruption, retourne raw
        return pkt

    payload = pkt[header_len:]

    # Si bit padding P=1, le dernier byte de la payload indique combien de
    # bytes à retirer en fin (le byte de count compris).
    if padding and payload:
        pad_count = payload[-1]
        if 0 < pad_count <= len(payload):
            payload = payload[:-pad_count]

    return payload


# ──────────────────────────────────────────────────────────
# Dumper WAV pour debug du flux audio entrant
# ──────────────────────────────────────────────────────────


class AudioDumper:
    """Dump les N premières secondes d'audio µ-law dans un fichier .wav.

    Format : WAV avec format code 7 (µ-law) 8 kHz mono — directement lisible
    par tout lecteur (QuickTime, VLC, Audacity, ffplay, etc.).

    Usage :
        dumper = AudioDumper(call_id="abc1234", out_dir="/tmp/mia-audio",
                             max_seconds=5)
        for chunk in audio_stream:
            dumper.write(chunk)   # No-op après max_seconds
        dumper.close()
    """

    # µ-law 8 kHz mono : 1 byte par sample, 8000 samples par seconde
    SAMPLE_RATE = 8000
    BYTES_PER_SEC = 8000

    def __init__(self, call_id: str, out_dir: str, max_seconds: int = 5) -> None:
        self.call_id = call_id
        self.max_bytes = self.BYTES_PER_SEC * max_seconds
        self.buf = bytearray()
        self.closed = False

        try:
            self.out_path = Path(out_dir) / f"call-{int(time.time())}-{call_id}.wav"
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log.warning("AudioDumper : impossible de créer %s : %s", out_dir, e)
            self.closed = True

    def write(self, ulaw_bytes: bytes) -> None:
        """Ajoute des bytes µ-law au buffer. No-op si limite atteinte."""
        if self.closed or len(self.buf) >= self.max_bytes:
            return
        # Cap à max_bytes pour éviter une croissance non bornée si close() oublié
        remaining = self.max_bytes - len(self.buf)
        self.buf.extend(ulaw_bytes[:remaining])
        if len(self.buf) >= self.max_bytes:
            self.close()

    def close(self) -> None:
        """Écrit le WAV sur disque (si pas déjà fait) et libère le buffer."""
        if self.closed:
            return
        self.closed = True
        if not self.buf:
            return
        try:
            wav = _build_ulaw_wav(bytes(self.buf), self.SAMPLE_RATE)
            self.out_path.write_bytes(wav)
            log.info("AudioDumper : %d ms d'audio dumpés → %s",
                     int(len(self.buf) * 1000 / self.BYTES_PER_SEC), self.out_path)
        except Exception as e:
            log.warning("AudioDumper close failed : %s", e)


def _build_ulaw_wav(ulaw_data: bytes, sample_rate: int) -> bytes:
    """Construit un fichier WAV complet (header + data) pour audio µ-law mono.

    Format WAV µ-law :
      - format code 7 (WAVE_FORMAT_MULAW)
      - 1 canal mono
      - 8 bits par sample
      - sample_rate = 8000 (téléphonie)

    Pourquoi pas la stdlib `wave` : le module `wave` ne supporte que PCM brut,
    pas µ-law. Pour µ-law on doit construire le header à la main avec le bon
    format code, sinon les lecteurs jouent l'audio comme du PCM bruité.
    """
    n_samples = len(ulaw_data)
    # Subchunk1 (fmt) = 18 bytes pour µ-law (16 + 2 de cbSize)
    fmt_chunk = struct.pack(
        "<4sIHHIIHHH",
        b"fmt ",      # subchunk1 ID
        18,           # subchunk1 size
        7,            # format code : 7 = µ-law
        1,            # channels
        sample_rate,
        sample_rate,  # byte rate (= sample_rate × channels × bits/8)
        1,            # block align
        8,            # bits per sample
        0,            # cbSize (extension size for non-PCM)
    )
    fact_chunk = struct.pack("<4sII", b"fact", 4, n_samples)
    data_chunk = struct.pack("<4sI", b"data", n_samples) + ulaw_data
    # RIFF header : taille totale = (len de tout ce qui suit "RIFF" et 4 bytes de size)
    riff_size = 4 + len(fmt_chunk) + len(fact_chunk) + len(data_chunk)
    riff_header = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
    return riff_header + fmt_chunk + fact_chunk + data_chunk
