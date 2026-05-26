"""Tests du service SMS multi-provider.

On vérifie :
  1. Le routage automatique (+262 → Brevo, autres → Telnyx)
  2. L'auth Brevo (header api-key, body JSON)
  3. Le fallback silencieux (config manquante, erreur HTTP)
  4. Le masquage des numéros dans les logs (RGPD)
"""
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


def test_route_reunion_to_brevo():
    """Numéro +262 → _send_via_brevo."""
    with patch.object(sms_service, "_send_via_brevo", return_value=True) as brevo, \
         patch.object(sms_service, "_send_via_telnyx", return_value=True) as telnyx:
        result = sms_service.send_sms("+262692123456", "Test")
        assert result is True
        brevo.assert_called_once()
        telnyx.assert_not_called()


def test_route_local_reunion_to_brevo():
    """0692123456 (notation locale Réunion) → routé en +262 puis vers Brevo."""
    with patch.object(sms_service, "_send_via_brevo", return_value=True) as brevo, \
         patch.object(sms_service, "_send_via_telnyx", return_value=True) as telnyx:
        sms_service.send_sms("0692123456", "Test")
        brevo.assert_called_once()
        # Vérifie que c'est bien le E.164 +262 qui est passé à Brevo
        assert brevo.call_args[0][0] == "+262692123456"
        telnyx.assert_not_called()


def test_route_metropole_to_telnyx():
    """0612345678 → +33... → Telnyx."""
    with patch.object(sms_service, "_send_via_brevo", return_value=True) as brevo, \
         patch.object(sms_service, "_send_via_telnyx", return_value=True) as telnyx:
        sms_service.send_sms("0612345678", "Test")
        telnyx.assert_called_once()
        brevo.assert_not_called()


def test_invalid_number_returns_false():
    """Numéro invalide → False sans appel HTTP."""
    with patch.object(sms_service, "_send_via_brevo") as brevo, \
         patch.object(sms_service, "_send_via_telnyx") as telnyx:
        assert sms_service.send_sms("", "msg") is False
        assert sms_service.send_sms(None, "msg") is False
        brevo.assert_not_called()
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
# Provider Brevo
# ─────────────────────────────────────────


def test_brevo_success():
    """Brevo retourne messageId → True + headers + body corrects."""
    fake_resp = _mock_http_response({
        "reference": "MIA-001",
        "messageId": 281474976710655,
        "smsCount": 1,
        "usedCredits": 0.045,
        "remainingCredits": 95.5,
    })
    with patch("urllib.request.urlopen", return_value=fake_resp) as urlopen:
        result = sms_service._send_via_brevo("+262692123456", "Bonjour")
        assert result is True

        req = urlopen.call_args[0][0]
        # Brevo utilise un header api-key (pas Authorization Bearer)
        normalized = {k.lower(): v for k, v in req.headers.items()}
        assert "api-key" in normalized
        assert normalized["api-key"] == "xkeysib-test-key"
        # Endpoint
        assert "api.brevo.com" in req.full_url
        assert "/transactionalSMS/sms" in req.full_url
        # Body
        body = json.loads(req.data.decode())
        assert body["recipient"] == "+262692123456"
        assert body["content"] == "Bonjour"
        assert body["sender"] == "MIA"
        assert body["type"] == "transactional"


def test_brevo_missing_api_key_silent_fail():
    """Si BREVO_API_KEY vide → False sans appel HTTP."""
    from app import config
    original = config.settings.brevo_api_key
    object.__setattr__(config.settings, "brevo_api_key", "")
    try:
        with patch("urllib.request.urlopen") as urlopen:
            result = sms_service._send_via_brevo("+262692123456", "Test")
            assert result is False
            urlopen.assert_not_called()
    finally:
        object.__setattr__(config.settings, "brevo_api_key", original)


def test_brevo_http_error_silent():
    """Erreur 4xx Brevo (sender non validé, crédits épuisés...) → False, pas de crash."""
    err = HTTPError(
        url="https://api.brevo.com/v3/transactionalSMS/sms",
        code=400,
        msg="Bad Request",
        hdrs={},
        fp=BytesIO(b'{"code": "invalid_parameter", "message": "Invalid sender"}'),
    )
    with patch("urllib.request.urlopen", side_effect=err):
        assert sms_service._send_via_brevo("+262692123456", "Test") is False


def test_brevo_network_error_silent():
    """Timeout / réseau Brevo → False, pas d'exception."""
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timeout")):
        assert sms_service._send_via_brevo("+262692123456", "Test") is False


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
