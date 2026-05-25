"""Tests du multi-tenant : login users + scope dashboard.

Vérifie qu'un user "restaurant" ne peut voir QUE son restaurant, et qu'un
admin voit tout. Couvre aussi le bootstrap auto du superadmin.
"""
import os

from app.models import Reservation, Restaurant, User
from app.services.call_log_service import CallTranscript
from app.utils.auth_hash import hash_password


# ─────────────────────────────────────────
# Bootstrap superadmin
# ─────────────────────────────────────────


def test_bootstrap_creates_admin_on_first_run(client, db_session):
    """Au startup, si table users vide → admin@mia.local créé."""
    # Le TestClient déclenche le startup au 1er request.
    client.get("/")
    admin = db_session.query(User).filter(User.email == "admin@mia.local").first()
    assert admin is not None
    assert admin.is_admin is True
    assert admin.restaurant_id is None


# ─────────────────────────────────────────
# Login email + password
# ─────────────────────────────────────────


def _admin_cookies(client):
    """Helper : se connecte comme admin et retourne les cookies."""
    r = client.post("/dashboard/login",
                    data={"email": "admin@mia.local",
                          "password": os.environ["DASHBOARD_PASSWORD"]},
                    follow_redirects=False)
    assert r.status_code == 302
    return r.cookies


def test_login_with_valid_credentials(client, db_session):
    """Email + password corrects → 302 + cookie session."""
    client.get("/")  # déclenche bootstrap
    r = client.post("/dashboard/login",
                    data={"email": "admin@mia.local",
                          "password": os.environ["DASHBOARD_PASSWORD"]},
                    follow_redirects=False)
    assert r.status_code == 302
    assert "mia_session" in r.cookies


def test_login_with_wrong_password(client, db_session):
    client.get("/")
    r = client.post("/dashboard/login",
                    data={"email": "admin@mia.local", "password": "wrong"},
                    follow_redirects=False)
    assert r.status_code == 200  # reste sur la page de login avec erreur
    assert "incorrect" in r.text.lower()


def test_login_unknown_email(client, db_session):
    client.get("/")
    r = client.post("/dashboard/login",
                    data={"email": "ghost@nowhere.com", "password": "any"},
                    follow_redirects=False)
    assert r.status_code == 200
    assert "incorrect" in r.text.lower()


# ─────────────────────────────────────────
# Scope multi-tenant
# ─────────────────────────────────────────


def test_admin_sees_all_restaurants(client, db_session):
    """Admin → voit tous les restaurants dans la liste."""
    # Crée 2 restos
    r1 = Restaurant(nom="Resto A", telephone="+33611111111")
    r2 = Restaurant(nom="Resto B", telephone="+33622222222")
    db_session.add_all([r1, r2])
    db_session.commit()

    cookies = _admin_cookies(client)
    resp = client.get("/dashboard/restaurants", cookies=cookies)
    assert "Resto A" in resp.text
    assert "Resto B" in resp.text


def test_restaurant_user_sees_only_own(client, db_session):
    """User scopé sur restaurant_id=1 → ne voit pas restaurant_id=2."""
    r1 = Restaurant(nom="Mon Resto", telephone="+33611111111")
    r2 = Restaurant(nom="Autre Resto", telephone="+33622222222")
    db_session.add_all([r1, r2])
    db_session.commit()
    db_session.refresh(r1)
    db_session.refresh(r2)

    # Crée un user scopé sur r1
    u = User(email="resto1@mia.local", password_hash=hash_password("pass123"),
             restaurant_id=r1.id, is_admin=False)
    db_session.add(u)
    db_session.commit()

    # Login en tant que user1
    login = client.post("/dashboard/login",
                        data={"email": "resto1@mia.local", "password": "pass123"},
                        follow_redirects=False)
    assert login.status_code == 302
    cookies = login.cookies

    resp = client.get("/dashboard/restaurants", cookies=cookies)
    assert "Mon Resto" in resp.text
    assert "Autre Resto" not in resp.text


def test_restaurant_user_blocked_from_other_restaurant(client, db_session):
    """User scopé sur r1 → GET /restaurants/r2 → 403."""
    r1 = Restaurant(nom="Mon Resto", telephone="+33611111111")
    r2 = Restaurant(nom="Autre", telephone="+33622222222")
    db_session.add_all([r1, r2])
    db_session.commit()
    db_session.refresh(r1)
    db_session.refresh(r2)

    u = User(email="r1@x.com", password_hash=hash_password("pass123"),
             restaurant_id=r1.id, is_admin=False)
    db_session.add(u)
    db_session.commit()

    login = client.post("/dashboard/login",
                        data={"email": "r1@x.com", "password": "pass123"},
                        follow_redirects=False)
    cookies = login.cookies

    resp = client.get(f"/dashboard/restaurants/{r2.id}", cookies=cookies)
    assert resp.status_code == 403


def test_restaurant_user_cannot_create_restaurant(client, db_session):
    """Non-admin → GET /restaurants/new → 403 (création réservée admin)."""
    r1 = Restaurant(nom="Mon Resto", telephone="+33611111111")
    db_session.add(r1)
    db_session.commit()
    db_session.refresh(r1)

    u = User(email="r1@x.com", password_hash=hash_password("pass123"),
             restaurant_id=r1.id, is_admin=False)
    db_session.add(u)
    db_session.commit()

    login = client.post("/dashboard/login",
                        data={"email": "r1@x.com", "password": "pass123"},
                        follow_redirects=False)
    cookies = login.cookies

    resp = client.get("/dashboard/restaurants/new", cookies=cookies)
    assert resp.status_code == 403


def test_user_cannot_see_other_restaurant_reservations(client, db_session):
    """User scopé : /reservations ne montre que ses propres résa."""
    r1 = Restaurant(nom="R1", telephone="+33611111111")
    r2 = Restaurant(nom="R2", telephone="+33622222222")
    db_session.add_all([r1, r2])
    db_session.commit()
    db_session.refresh(r1)
    db_session.refresh(r2)

    db_session.add_all([
        Reservation(restaurant_id=r1.id, code="R-MINE", personnes=2,
                    heure="20h", date="2026-06-15"),
        Reservation(restaurant_id=r2.id, code="R-OTHER", personnes=4,
                    heure="19h", date="2026-06-15"),
    ])
    db_session.commit()

    u = User(email="r1@x.com", password_hash=hash_password("pass123"),
             restaurant_id=r1.id, is_admin=False)
    db_session.add(u)
    db_session.commit()

    login = client.post("/dashboard/login",
                        data={"email": "r1@x.com", "password": "pass123"},
                        follow_redirects=False)
    cookies = login.cookies

    resp = client.get("/dashboard/reservations", cookies=cookies)
    assert "R-MINE" in resp.text
    assert "R-OTHER" not in resp.text


def test_user_cannot_see_other_restaurant_calls(client, db_session):
    """User scopé : /calls/<id> d'un autre resto → 403."""
    r1 = Restaurant(nom="R1", telephone="+33611111111")
    r2 = Restaurant(nom="R2", telephone="+33622222222")
    db_session.add_all([r1, r2])
    db_session.commit()
    db_session.refresh(r1)
    db_session.refresh(r2)

    ct = CallTranscript(restaurant_id=r2.id, caller_phone="", call_control_id="")
    ct.add_client("hello")
    other_call_id = ct.flush()

    u = User(email="r1@x.com", password_hash=hash_password("pass123"),
             restaurant_id=r1.id, is_admin=False)
    db_session.add(u)
    db_session.commit()

    login = client.post("/dashboard/login",
                        data={"email": "r1@x.com", "password": "pass123"},
                        follow_redirects=False)
    cookies = login.cookies

    resp = client.get(f"/dashboard/calls/{other_call_id}", cookies=cookies)
    assert resp.status_code == 403


def test_users_page_admin_only(client, db_session):
    """Non-admin → /dashboard/users → 403."""
    r1 = Restaurant(nom="R1", telephone="+33611111111")
    db_session.add(r1)
    db_session.commit()
    db_session.refresh(r1)

    u = User(email="r1@x.com", password_hash=hash_password("pass123"),
             restaurant_id=r1.id, is_admin=False)
    db_session.add(u)
    db_session.commit()

    login = client.post("/dashboard/login",
                        data={"email": "r1@x.com", "password": "pass123"},
                        follow_redirects=False)
    cookies = login.cookies

    resp = client.get("/dashboard/users", cookies=cookies)
    assert resp.status_code == 403


def test_admin_can_create_user(client, db_session):
    """Admin peut créer un user via POST /dashboard/users."""
    r1 = Restaurant(nom="R1", telephone="+33611111111")
    db_session.add(r1)
    db_session.commit()
    db_session.refresh(r1)

    cookies = _admin_cookies(client)
    resp = client.post("/dashboard/users",
                       data={"email": "newuser@x.com", "password": "pass123",
                             "restaurant_id": str(r1.id), "is_admin": ""},
                       cookies=cookies, follow_redirects=False)
    assert resp.status_code == 302

    created = db_session.query(User).filter(User.email == "newuser@x.com").first()
    assert created is not None
    assert created.restaurant_id == r1.id
    assert created.is_admin is False
