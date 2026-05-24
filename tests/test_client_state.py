"""Tests de la signature HMAC du client_state Telnyx.

Le client_state est envoyé à Telnyx lors du décrochage et rejoué dans le
WebSocket. Sans signature, un attaquant pourrait forger un client_state
avec un restaurant_id arbitraire et ouvrir la session OpenAI sur le compte
d'un autre restaurant.

Tests :
  - Round-trip sign → verify
  - Détection d'altération (changement d'un byte)
  - Expiration (TTL 120s)
  - Absence de signature
"""
import base64
import json
import time
from unittest.mock import patch

from app.routers.voice_webhook import _sign_client_state, _verify_client_state


def test_sign_verify_roundtrip():
    """Signature puis vérification du même payload → données identiques."""
    payload = {"restaurant_id": 42, "caller_phone": "+33612345678", "call_control_id": "abc"}
    token = _sign_client_state(payload)
    decoded = _verify_client_state(token)

    assert decoded is not None
    assert decoded["restaurant_id"] == 42
    assert decoded["caller_phone"] == "+33612345678"
    assert decoded["call_control_id"] == "abc"


def test_sign_produces_base64_only():
    """Telnyx exige du base64 pur — pas de '.' ni autre séparateur dans le token."""
    payload = {"restaurant_id": 1, "caller_phone": "", "call_control_id": ""}
    token = _sign_client_state(payload)
    # Doit décoder en base64 sans erreur
    decoded = base64.b64decode(token)
    assert decoded  # bytes non vides
    # Pas de séparateur custom
    assert "." not in token
    assert ":" not in token


def test_verify_rejects_tampered_payload():
    """Si on modifie le payload après signature → verify rejette."""
    payload = {"restaurant_id": 1, "caller_phone": "+33612345678", "call_control_id": "xyz"}
    token = _sign_client_state(payload)

    # On décode, modifie restaurant_id, re-encode SANS resigner
    data = json.loads(base64.b64decode(token).decode())
    data["restaurant_id"] = 999  # ← tentative d'escalade
    tampered = base64.b64encode(json.dumps(data).encode()).decode()

    assert _verify_client_state(tampered) is None


def test_verify_rejects_missing_signature():
    """Token sans champ 'sig' → rejet."""
    payload = {"restaurant_id": 1, "ts": int(time.time())}
    no_sig = base64.b64encode(json.dumps(payload).encode()).decode()
    assert _verify_client_state(no_sig) is None


def test_verify_rejects_expired_token():
    """Token > 120s d'âge → rejet (anti-replay)."""
    payload = {"restaurant_id": 1, "caller_phone": "", "call_control_id": ""}
    # On signe avec un timestamp dans le passé (200s avant)
    with patch("app.routers.voice_webhook.time.time", return_value=time.time() - 200):
        token = _sign_client_state(payload)
    # Vérification avec l'horloge actuelle → trop vieux
    assert _verify_client_state(token) is None


def test_verify_rejects_empty_token():
    assert _verify_client_state("") is None
    assert _verify_client_state(None) is None  # type: ignore[arg-type]


def test_verify_rejects_garbage():
    """Garbage qui n'est même pas du base64 valide → None sans crash."""
    assert _verify_client_state("!!!not_base64!!!") is None
    assert _verify_client_state("notbase64atall===") is None


def test_verify_rejects_valid_b64_but_not_json():
    """Base64 OK mais pas du JSON dedans → None."""
    bad = base64.b64encode(b"this is not json").decode()
    assert _verify_client_state(bad) is None


def test_sign_with_different_secrets_produces_different_tokens():
    """Changer le secret produit une signature différente (sanity check HMAC)."""
    payload = {"restaurant_id": 1, "caller_phone": "", "call_control_id": ""}
    token1 = _sign_client_state(payload)

    from app import config
    original = config.settings.dashboard_secret
    object.__setattr__(config.settings, "dashboard_secret", "different-secret-value")
    try:
        token2 = _sign_client_state(payload)
        # Les payloads contiennent la sig dans le JSON encodé → tokens différents
        assert token1 != token2
    finally:
        object.__setattr__(config.settings, "dashboard_secret", original)
