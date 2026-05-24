"""Tests du service SMS multi-provider.

On vérifie :
  1. Le routage automatique (+262 → OVH, autres → Telnyx)
  2. La signature HMAC-SHA1 OVH
  3. Le fallback silencieux (config manquante, erreur HTTP)
  4. Le masquage des numéros dans les logs (RGPD)
"""
import hashlib
import json
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from app.services import sms_service


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────


def _mock_http_response(body: dict, status: int = 200):
    """Mock un urlopen() qui retourne un body JSON."""
    mock = MagicMock()
    mock.__enter__ = MagicMock(return_value=mock)
    mock.__exit__ = MagicMock(return_value=False)
    mock.read.return_value = json.dumps(body).encode()
    mock.status = status
    return mock


# ─────────────────────────────────────────
# Routage
# ─────────────────────────────────────────


def test_route_reunion_to_ovh():
    """Numéro +262 → _send_via_ovh."""
    with patch.object(sms_service, "_send_via_ovh", return_value=True) as ovh, \
         patch.object(sms_service, "_send_via_telnyx", return_value=True) as telnyx:
        result = sms_service.send_sms("+262692123456", "Test")
        assert result is True
        ovh.assert_called_once()
        telnyx.assert_not_called()


def test_route_local_reunion_to_ovh():
    """0692123456 (notation locale Réunion) → routé en +262 puis vers OVH."""
    with patch.object(sms_service, "_send_via_ovh", return_value=True) as ovh, \
         patch.object(sms_service, "_send_via_telnyx", return_value=True) as telnyx:
        sms_service.send_sms("0692123456", "Test")
        ovh.assert_called_once()
        # Vérifie que c'est bien le E.164 +262 qui est passé à OVH
        assert ovh.call_args[0][0] == "+262692123456"
        telnyx.assert_not_called()


def test_route_metropole_to_telnyx():
    """0612345678 → +33... → Telnyx."""
    with patch.object(sms_service, "_send_via_ovh", return_value=True) as ovh, \
         patch.object(sms_service, "_send_via_telnyx", return_value=True) as telnyx:
        sms_service.send_sms("0612345678", "Test")
        telnyx.assert_called_once()
        ovh.assert_not_called()


def test_invalid_number_returns_false():
    """Numéro invalide → False sans appel HTTP."""
    with patch.object(sms_service, "_send_via_ovh") as ovh, \
         patch.object(sms_service, "_send_via_telnyx") as telnyx:
        assert sms_service.send_sms("", "msg") is False
        assert sms_service.send_sms(None, "msg") is False
        ovh.assert_not_called()
        telnyx.assert_not_called()


# ─────────────────────────────────────────
# Provider Telnyx
# ─────────────────────────────────────────


def test_telnyx_success():
    """Envoi Telnyx OK → True + log id."""
    fake_resp = _mock_http_response({"data": {"id": "msg_abc123"}})
    with patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        result = sms_service._send_via_telnyx("+33612345678", "Bonjour")
        assert result is True
        # Vérifie l'URL Telnyx
        call = urlopen.call_args[0][0]
        assert "api.telnyx.com" in call.full_url
        # Body bien formé
        body = json.loads(call.data.decode())
        assert body["to"] == "+33612345678"
        assert body["text"] == "Bonjour"


def test_telnyx_http_error_silent():
    """Erreur 4xx Telnyx → False, pas d'exception."""
    err = HTTPError(
        url="https://api.telnyx.com/v2/messages",
        code=400,
        msg="Bad Request",
        hdrs={},
        fp=BytesIO(b'{"error": "Invalid recipient"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        result = sms_service._send_via_telnyx("+33612345678", "Test")
        assert result is False  # silent fail


def test_telnyx_network_error_silent():
    """Timeout / réseau → False, pas d'exception."""
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
        assert sms_service._send_via_telnyx("+33612345678", "Test") is False


# ─────────────────────────────────────────
# Provider OVH
# ─────────────────────────────────────────


def test_ovh_signature_format():
    """La signature OVH suit le format documenté : '$1$' + sha1_hex(...)."""
    sig = sms_service._ovh_sign(
        method="POST",
        url="https://eu.api.ovh.com/1.0/sms/test/jobs",
        body='{"a":1}',
        timestamp="1700000000",
        app_secret="my_secret",
        consumer_key="my_consumer",
    )
    expected_input = "my_secret+my_consumer+POST+https://eu.api.ovh.com/1.0/sms/test/jobs+{\"a\":1}+1700000000"
    expected = "$1$" + hashlib.sha1(expected_input.encode()).hexdigest()
    assert sig == expected
    assert sig.startswith("$1$")
    assert len(sig) == 3 + 40  # "$1$" + 40 hex chars de sha1


def test_ovh_success():
    """OVH retourne validReceivers → True."""
    fake_resp = _mock_http_response({
        "ids": [12345],
        "validReceivers": ["+262692123456"],
        "invalidReceivers": [],
    })
    with patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        result = sms_service._send_via_ovh("+262692123456", "Bonjour")
        assert result is True
        # urllib.request.Request normalise les headers via str.capitalize()
        # → "X-Ovh-Application" devient "X-ovh-application" dans req.headers.
        req = urlopen.call_args[0][0]
        normalized = {k.lower(): v for k, v in req.headers.items()}
        assert "x-ovh-application" in normalized
        assert "x-ovh-consumer" in normalized
        assert "x-ovh-signature" in normalized
        assert "x-ovh-timestamp" in normalized
        # La signature doit commencer par "$1$" suivi du sha1 hex (40 chars)
        assert normalized["x-ovh-signature"].startswith("$1$")
        # Vérifie URL
        assert "/sms/sms-test-1/jobs" in req.full_url
        # Vérifie body
        body = json.loads(req.data.decode())
        assert body["receivers"] == ["+262692123456"]
        assert body["sender"] == "MIA"
        assert body["noStopClause"] is True


def test_ovh_invalid_receiver():
    """OVH met le numéro dans invalidReceivers → False."""
    fake_resp = _mock_http_response({
        "ids": [],
        "validReceivers": [],
        "invalidReceivers": ["+262692123456"],
    })
    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = sms_service._send_via_ovh("+262692123456", "Test")
        assert result is False


def test_ovh_missing_config_silent_fail():
    """Si une seule des 4 clés OVH manque → False sans appel HTTP."""
    from app import config
    original = config.settings.ovh_application_key
    # On patche directement le settings (dataclass frozen → object.__setattr__)
    object.__setattr__(config.settings, "ovh_application_key", "")
    try:
        with patch("urllib.request.urlopen") as urlopen:
            result = sms_service._send_via_ovh("+262692123456", "Test")
            assert result is False
            urlopen.assert_not_called()
    finally:
        object.__setattr__(config.settings, "ovh_application_key", original)


def test_ovh_http_error_silent():
    """Erreur 403 OVH (auth/quota) → False, log warning, pas de crash."""
    err = HTTPError(
        url="https://eu.api.ovh.com/1.0/sms/foo/jobs",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=BytesIO(b'{"message": "This service does not exist"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        assert sms_service._send_via_ovh("+262692123456", "Test") is False


# ─────────────────────────────────────────
# Masquage RGPD
# ─────────────────────────────────────────


def test_mask_phone_partial():
    """Masque le milieu mais garde début+fin pour debug."""
    masked = sms_service._mask("+33612345678")
    assert "***" in masked
    assert masked.startswith("+3361")
    assert masked.endswith("78")
    # Pas de leak des chiffres du milieu
    assert "234" not in masked
    assert "456" not in masked


def test_mask_short_phone():
    """Numéro trop court : masquage agressif (rien à montrer)."""
    assert "***" in sms_service._mask("12")
