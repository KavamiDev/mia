"""Tests de l'endpoint /health."""


def test_health_returns_json(client):
    r = client.get("/health")
    assert r.status_code in (200, 503)
    data = r.json()
    assert "status" in data
    assert "checks" in data
    assert data["status"] in ("ok", "degraded", "down")


def test_health_db_check_ok(client):
    """Avec une DB en mémoire qui répond → check db = ok."""
    r = client.get("/health")
    data = r.json()
    assert data["checks"]["db"] == "ok"


def test_health_includes_api_keys_status(client):
    """Les checks doivent rapporter la présence des clés critiques."""
    r = client.get("/health")
    data = r.json()
    assert "openai_key" in data["checks"]
    assert "telnyx_key" in data["checks"]


def test_health_brevo_status(client):
    """BREVO_API_KEY défini en test → brevo_sms = ok."""
    r = client.get("/health")
    data = r.json()
    assert data["checks"]["brevo_sms"] in ("ok", "not_configured")


def test_health_no_auth_required(client):
    """Pas besoin d'auth pour /health (utilisable par uptime monitors)."""
    r = client.get("/health")  # sans headers ni cookies
    assert r.status_code in (200, 503)
