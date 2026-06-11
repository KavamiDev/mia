"""Tests du cache TTL restaurant+menu et de son invalidation par l'API.

Le cache élimine les SELECT au décrochage d'un appel ; les mutations menu et
restaurant doivent l'invalider immédiatement (sinon MIA proposerait un menu
périmé pendant TTL secondes).
"""
from app.services import restaurant_cache

RESTO = {"id": 1, "nom": "Chez Test", "telephone": "+33612345678"}
MENU = [{"nom_plat": "Pizza", "prix": 12.0, "description": ""}]


def setup_function():
    restaurant_cache.clear()


def test_get_miss_returns_none():
    assert restaurant_cache.get(999) is None


def test_put_get_roundtrip():
    restaurant_cache.put(1, RESTO, MENU, ttl=60)
    cached = restaurant_cache.get(1)
    assert cached is not None
    restaurant, menu = cached
    assert restaurant == RESTO
    assert menu == MENU


def test_get_returns_copies():
    """Le bridge ne doit pas pouvoir muter l'entrée partagée du cache."""
    restaurant_cache.put(1, RESTO, MENU, ttl=60)
    restaurant, menu = restaurant_cache.get(1)
    restaurant["nom"] = "Hacké"
    menu[0]["prix"] = 0.0
    restaurant2, menu2 = restaurant_cache.get(1)
    assert restaurant2["nom"] == "Chez Test"
    assert menu2[0]["prix"] == 12.0


def test_expiry():
    restaurant_cache.put(1, RESTO, MENU, ttl=-1)  # déjà expiré
    assert restaurant_cache.get(1) is None


def test_ttl_zero_disables_cache():
    restaurant_cache.put(1, RESTO, MENU, ttl=0)
    assert restaurant_cache.get(1) is None


def test_invalidate():
    restaurant_cache.put(1, RESTO, MENU, ttl=60)
    restaurant_cache.invalidate(1)
    assert restaurant_cache.get(1) is None


def test_invalidate_unknown_id_is_noop():
    restaurant_cache.invalidate(12345)  # ne lève pas


# ─────────────────────────────────────────
# Intégration : les mutations API invalident le cache
# ─────────────────────────────────────────


def test_menu_create_invalidates_cache(client, auth_headers, sample_restaurant):
    rid = sample_restaurant.id
    restaurant_cache.put(rid, RESTO, MENU, ttl=60)
    resp = client.post("/menu", headers=auth_headers, json={
        "restaurant_id": rid, "nom_plat": "Nouvelle Pizza", "prix": 15.0})
    assert resp.status_code == 201
    assert restaurant_cache.get(rid) is None


def test_menu_bulk_invalidates_cache(client, auth_headers, sample_restaurant):
    rid = sample_restaurant.id
    restaurant_cache.put(rid, RESTO, MENU, ttl=60)
    resp = client.post(f"/menu/restaurant/{rid}/bulk", headers=auth_headers,
                       json={"items": [{"nom_plat": "Calzone", "prix": 13.0}]})
    assert resp.status_code == 200
    assert restaurant_cache.get(rid) is None


def test_menu_delete_invalidates_cache(client, auth_headers, sample_restaurant, db_session):
    from app.models import MenuItem

    rid = sample_restaurant.id
    item = db_session.query(MenuItem).filter(MenuItem.restaurant_id == rid).first()
    restaurant_cache.put(rid, RESTO, MENU, ttl=60)
    resp = client.delete(f"/menu/{item.id}", headers=auth_headers)
    assert resp.status_code == 200
    assert restaurant_cache.get(rid) is None


def test_restaurant_update_invalidates_cache(client, auth_headers, sample_restaurant):
    rid = sample_restaurant.id
    restaurant_cache.put(rid, RESTO, MENU, ttl=60)
    resp = client.put(f"/restaurants/{rid}", headers=auth_headers, json={
        "nom": "Chez Marco 2", "telephone": "+33612345678"})
    assert resp.status_code == 200
    assert restaurant_cache.get(rid) is None
