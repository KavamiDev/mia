"""Tests du rate limiter à fenêtre glissante + intégration webhook Telnyx.

Le limiter protège POST /voice/incoming d'un flood (la vérif Ed25519 et le
lookup restaurant ont un coût, et le process sert aussi l'audio temps réel).
"""
import json
from unittest.mock import patch

from app.routers import voice_webhook
from app.utils.ratelimit import SlidingWindowRateLimiter


# ─────────────────────────────────────────
# Unitaire : SlidingWindowRateLimiter
# ─────────────────────────────────────────


def test_allows_under_limit():
    limiter = SlidingWindowRateLimiter(max_events=3, window_seconds=60)
    assert all(limiter.allow("ip1") for _ in range(3))


def test_blocks_over_limit():
    limiter = SlidingWindowRateLimiter(max_events=2, window_seconds=60)
    assert limiter.allow("ip1")
    assert limiter.allow("ip1")
    assert not limiter.allow("ip1")


def test_keys_are_independent():
    """Une IP saturée ne bloque pas les autres."""
    limiter = SlidingWindowRateLimiter(max_events=1, window_seconds=60)
    assert limiter.allow("attacker")
    assert not limiter.allow("attacker")
    assert limiter.allow("telnyx")


def test_window_slides():
    """Les événements sortis de la fenêtre libèrent de la place."""
    now = [1000.0]
    limiter = SlidingWindowRateLimiter(max_events=2, window_seconds=10,
                                       clock=lambda: now[0])
    assert limiter.allow("k")
    assert limiter.allow("k")
    assert not limiter.allow("k")
    now[0] += 11  # les 2 événements sont expirés
    assert limiter.allow("k")


def test_prune_bounds_memory():
    """Au-delà du seuil, les clés sans événement récent sont purgées."""
    from app.utils import ratelimit

    now = [1000.0]
    limiter = SlidingWindowRateLimiter(max_events=5, window_seconds=10,
                                       clock=lambda: now[0])
    with patch.object(ratelimit, "_PRUNE_THRESHOLD", 10):
        for i in range(10):
            limiter.allow(f"old-{i}")
        now[0] += 100  # toutes les anciennes clés sont périmées
        limiter.allow("fresh")
        assert "fresh" in limiter._events
        assert not any(k.startswith("old-") for k in limiter._events)


# ─────────────────────────────────────────
# Intégration : POST /voice/incoming → 429
# ─────────────────────────────────────────


def _post_webhook(client):
    body = {"data": {"event_type": "call.hangup", "payload": {}}}
    return client.post("/voice/incoming", content=json.dumps(body),
                       headers={"Content-Type": "application/json"})


def test_webhook_rate_limited(client):
    """Au-delà de la limite, le webhook répond 429 sans toucher la suite."""
    limiter = SlidingWindowRateLimiter(max_events=2, window_seconds=60)
    with patch.object(voice_webhook, "_webhook_limiter", limiter):
        assert _post_webhook(client).status_code == 200
        assert _post_webhook(client).status_code == 200
        resp = _post_webhook(client)
        assert resp.status_code == 429


def test_webhook_limiter_disabled(client):
    """Limiter None (env = 0) → aucun blocage."""
    with patch.object(voice_webhook, "_webhook_limiter", None):
        for _ in range(5):
            assert _post_webhook(client).status_code == 200
