"""Smoke tests des endpoints REST + sécurité X-API-Key.

But : valider que les routes sont joignables, retournent le bon code HTTP,
et que l'auth marche. On NE valide pas la logique métier (couverte par
test_tools.py) ni Telnyx (couvert par test_sms.py).
"""
import json
from unittest.mock import patch


# ─────────────────────────────────────────
# Health checks
# ─────────────────────────────────────────


def test_root_health(client):
    """GET / → 200 + JSON."""
    r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "app": "MIA"}


def test_voice_incoming_health(client):
    """GET /voice/incoming → 200 (utilisé par Telnyx pour pinger)."""
    r = client.get("/voice/incoming")
    assert r.status_code == 200


# ─────────────────────────────────────────
# Authentification API
# ─────────────────────────────────────────


def test_restaurants_requires_api_key(client):
    """Sans X-API-Key → 401."""
    r = client.get("/restaurants")
    assert r.status_code == 401


def test_restaurants_rejects_wrong_api_key(client):
    """Mauvaise X-API-Key → 401."""
    r = client.get("/restaurants", headers={"X-API-Key": "wrong-key"})
    assert r.status_code == 401


def test_restaurants_accepts_valid_key(client, auth_headers):
    """Bonne clé → 200 + liste vide initialement."""
    r = client.get("/restaurants", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []


# ─────────────────────────────────────────
# CRUD Restaurant
# ─────────────────────────────────────────


def test_create_restaurant(client, auth_headers):
    """POST /restaurants crée un restaurant valide."""
    payload = {
        "nom": "Le Bistrot",
        "telephone": "+33612345678",
        "adresse": "10 rue de la Paix",
        "horaires": "12h-14h, 19h-22h",
        "quota_reservations": 30,
    }
    r = client.post("/restaurants", json=payload, headers=auth_headers)
    assert r.status_code == 201
    data = r.json()
    assert data["nom"] == "Le Bistrot"
    assert data["quota_reservations"] == 30
    assert "id" in data


def test_create_restaurant_validation_error(client, auth_headers):
    """Nom vide → 422 (validation Pydantic)."""
    payload = {"nom": "", "telephone": "+33612345678"}
    r = client.post("/restaurants", json=payload, headers=auth_headers)
    assert r.status_code == 422


def test_get_restaurant_404(client, auth_headers):
    """Restaurant inexistant → 404."""
    r = client.get("/restaurants/9999", headers=auth_headers)
    assert r.status_code == 404


def test_update_restaurant(client, auth_headers):
    """PUT /restaurants/<id> modifie le restaurant."""
    # Crée
    payload = {"nom": "Avant", "telephone": "+33612345678"}
    created = client.post("/restaurants", json=payload, headers=auth_headers).json()

    # Update
    new_payload = {"nom": "Après", "telephone": "+33612345678", "horaires": "11h-15h"}
    r = client.put(f"/restaurants/{created['id']}", json=new_payload, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["nom"] == "Après"
    assert r.json()["horaires"] == "11h-15h"


# ─────────────────────────────────────────
# Menu
# ─────────────────────────────────────────


def test_menu_bulk_import(client, auth_headers):
    """Import en masse du menu d'un restaurant."""
    # Setup : crée le restaurant
    r_data = {"nom": "Pizzeria", "telephone": "+33612345678"}
    rid = client.post("/restaurants", json=r_data, headers=auth_headers).json()["id"]

    # Bulk import
    payload = {
        "items": [
            {"nom_plat": "Margherita", "prix": 12.0},
            {"nom_plat": "Reine", "prix": 14.0},
            {"nom_plat": "Tiramisu", "prix": 6.5, "description": "Maison"},
        ],
    }
    r = client.post(f"/menu/restaurant/{rid}/bulk", json=payload, headers=auth_headers)
    assert r.status_code == 200
    assert r.json()["added"] == 3

    # Vérifie via GET
    r2 = client.get(f"/menu/restaurant/{rid}", headers=auth_headers)
    assert r2.status_code == 200
    assert len(r2.json()) == 3


def test_menu_bulk_unknown_restaurant(client, auth_headers):
    """Bulk sur restaurant inexistant → 404."""
    payload = {"items": [{"nom_plat": "Test", "prix": 10.0}]}
    r = client.post("/menu/restaurant/9999/bulk", json=payload, headers=auth_headers)
    assert r.status_code == 404


def test_menu_create_and_delete(client, auth_headers):
    """POST /menu puis DELETE /menu/<id>."""
    r_data = {"nom": "Resto", "telephone": "+33612345678"}
    rid = client.post("/restaurants", json=r_data, headers=auth_headers).json()["id"]

    item = {"restaurant_id": rid, "nom_plat": "Plat test", "prix": 9.99}
    created = client.post("/menu", json=item, headers=auth_headers).json()
    item_id = created["id"]

    r = client.delete(f"/menu/{item_id}", headers=auth_headers)
    assert r.status_code == 200

    # Liste vide
    r2 = client.get(f"/menu/restaurant/{rid}", headers=auth_headers)
    assert r2.json() == []


# ─────────────────────────────────────────
# Webhook Telnyx (signature)
# ─────────────────────────────────────────


def test_webhook_rejects_invalid_signature(client):
    """POST /voice/incoming sans signature valide → 401.

    Si TELNYX_PUBLIC_KEY est définie, signature obligatoire. Comme on a une
    fake key dans les tests, on vérifie le rejet — sauf si la key n'est pas
    set, auquel cas le webhook accepte tout (dev mode).
    """
    from app import config
    # Force une clé pour activer la vérif
    object.__setattr__(config.settings, "telnyx_public_key", "fake-pub-key-base64==")
    try:
        body = json.dumps({"data": {"event_type": "call.initiated"}}).encode()
        r = client.post("/voice/incoming", content=body)
        # 401 attendu : signature absente ou invalide
        assert r.status_code in (400, 401)
    finally:
        object.__setattr__(config.settings, "telnyx_public_key", None)


def test_webhook_accepts_in_dev_mode_without_pub_key(client):
    """Sans TELNYX_PUBLIC_KEY (dev) → webhook accepté avec warning."""
    from app import config
    object.__setattr__(config.settings, "telnyx_public_key", None)

    # Event ignoré (pas call.initiated) → 200
    body = json.dumps({"data": {"event_type": "call.hangup"}}).encode()
    r = client.post("/voice/incoming", content=body,
                    headers={"content-type": "application/json"})
    assert r.status_code == 200


# ─────────────────────────────────────────
# Reservations / Commandes (lecture)
# ─────────────────────────────────────────


def test_list_reservations_empty(client, auth_headers):
    """GET /reservations sur DB vide → []."""
    r = client.get("/reservations", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []


def test_list_commandes_empty(client, auth_headers):
    r = client.get("/commandes", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []


def test_list_reservations_filter_by_restaurant(client, auth_headers):
    """?restaurant_id=X filtre bien."""
    r = client.get("/reservations?restaurant_id=42", headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == []
