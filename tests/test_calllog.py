"""Tests du système de log d'appel (chantier 4 — dashboard SAV).

Couvre :
  - CallTranscript : buffer + flush en DB + idempotence
  - Modèle CallLog : création, champs requis, JSON columns
  - Routes dashboard : list/detail/flag SAV avec auth cookie
"""
import time
from unittest.mock import patch

import pytest

from app.models import CallLog
from app.services.call_log_service import CallTranscript


# ─────────────────────────────────────────
# CallTranscript : buffer + flush
# ─────────────────────────────────────────


def test_transcript_accumulates_client_and_mia(db_session, sample_restaurant):
    """add_client + add_mia + add_tool s'accumulent et flush en DB."""
    ct = CallTranscript(restaurant_id=sample_restaurant.id,
                        caller_phone="+33612345678",
                        call_control_id="call_abc123")
    ct.add_client("Bonjour, je voudrais réserver")
    ct.add_mia("Pour combien de personnes ?")
    ct.add_client("4 personnes, demain à 20h")
    ct.add_mia("Je récap : 4 personnes demain à 20h. Je valide ?")
    ct.add_client("Oui c'est bon")
    ct.add_tool("create_reservation",
                {"personnes": 4, "heure": "20h", "date": "2026-06-15"},
                {"success": True, "code": "R4T2K"})

    call_id = ct.flush()
    assert call_id is not None

    cl = db_session.query(CallLog).filter(CallLog.id == call_id).first()
    assert cl is not None
    assert cl.restaurant_id == sample_restaurant.id
    assert cl.caller_phone == "+33612345678"
    assert cl.call_control_id == "call_abc123"
    assert len(cl.transcript) == 5
    assert cl.transcript[0]["who"] == "client"
    assert cl.transcript[0]["text"] == "Bonjour, je voudrais réserver"
    assert len(cl.tool_calls) == 1
    assert cl.tool_calls[0]["name"] == "create_reservation"
    assert cl.tool_calls[0]["success"] is True
    assert cl.tool_calls[0]["code"] == "R4T2K"
    # Lien direct vers la résa via reservation_code
    assert cl.reservation_code == "R4T2K"
    assert cl.commande_code is None
    assert cl.duration_seconds is not None and cl.duration_seconds >= 0


def test_transcript_links_commande_code(db_session, sample_restaurant):
    """create_commande → commande_code rempli."""
    ct = CallTranscript(restaurant_id=sample_restaurant.id,
                        caller_phone="", call_control_id="")
    ct.add_tool("create_commande", {"items": [{"plat": "Pizza", "qty": 2}]},
                {"success": True, "code": "C7XYZ"})
    call_id = ct.flush()
    cl = db_session.query(CallLog).filter(CallLog.id == call_id).first()
    assert cl.commande_code == "C7XYZ"
    assert cl.reservation_code is None


def test_transcript_blocked_tool_recorded(db_session, sample_restaurant):
    """Tool bloqué par garde double-confirm → success=False + blocked_reason."""
    ct = CallTranscript(restaurant_id=sample_restaurant.id,
                        caller_phone="", call_control_id="")
    ct.add_tool("create_reservation",
                {"personnes": 2},
                {"success": False},
                blocked_reason="Pas de confirmation orale détectée")
    call_id = ct.flush()
    cl = db_session.query(CallLog).filter(CallLog.id == call_id).first()
    assert cl.tool_calls[0]["blocked_reason"] == "Pas de confirmation orale détectée"
    assert cl.tool_calls[0]["success"] is False
    # Pas de code généré → reservation_code reste null
    assert cl.reservation_code is None


def test_transcript_flush_idempotent(db_session, sample_restaurant):
    """flush() deux fois → seul le premier crée une ligne en DB."""
    ct = CallTranscript(restaurant_id=sample_restaurant.id,
                        caller_phone="", call_control_id="")
    ct.add_client("Test")
    id1 = ct.flush()
    id2 = ct.flush()
    assert id1 is not None
    assert id2 is None  # 2e flush no-op
    assert db_session.query(CallLog).count() == 1


def test_transcript_empty_call_no_flush(db_session, sample_restaurant):
    """Appel sans aucun échange → flush() retourne None, pas de ligne DB."""
    ct = CallTranscript(restaurant_id=sample_restaurant.id,
                        caller_phone="", call_control_id="")
    assert ct.flush() is None
    assert db_session.query(CallLog).count() == 0


def test_transcript_ignores_empty_text(db_session, sample_restaurant):
    """add_client('') / add_mia('') sont ignorés (whisper retourne parfois vide)."""
    ct = CallTranscript(restaurant_id=sample_restaurant.id,
                        caller_phone="", call_control_id="")
    ct.add_client("")
    ct.add_client(None)  # type: ignore[arg-type]
    ct.add_mia("")
    ct.add_client("contenu réel")
    call_id = ct.flush()
    cl = db_session.query(CallLog).filter(CallLog.id == call_id).first()
    assert len(cl.transcript) == 1


# ─────────────────────────────────────────
# Routes dashboard /calls
# ─────────────────────────────────────────


def _login_dashboard(client):
    """Connecte le client comme admin et retourne les cookies de session.

    Le superadmin admin@mia.local est créé automatiquement au startup si la
    table users est vide (cf. app.main._bootstrap_admin_user).
    """
    import os
    r = client.post("/dashboard/login",
                    data={"email": "admin@mia.local",
                          "password": os.environ["DASHBOARD_PASSWORD"]},
                    follow_redirects=False)
    assert r.status_code == 302, f"Login failed: {r.status_code} {r.text[:300]}"
    return r.cookies


def test_create_restaurant_duplicate_phone_returns_form_with_error(client, db_session):
    """POST /restaurants/new avec un incoming_phone_number déjà pris →
    pas de 500, mais form ré-affiché avec un flash d'erreur clair."""
    from app.models import Restaurant
    db_session.add(Restaurant(nom="Existing", telephone="+33611111111",
                              incoming_phone_number="+33644645590"))
    db_session.commit()

    cookies = _login_dashboard(client)
    r = client.post("/dashboard/restaurants/new", cookies=cookies, follow_redirects=False,
                    data={"nom": "Doublon", "telephone": "+33622222222",
                          "incoming_phone_number": "+33644645590",
                          "quota_reservations": "", "quota_commandes": "",
                          "sms_to_client": "on", "sms_to_restaurant": "on"})
    # Pas de 500 : on rend le form avec le message
    assert r.status_code == 200
    assert "déjà utilisé" in r.text or "Conflit" in r.text


def test_calls_page_requires_login(client):
    """Sans cookie → redirect vers login."""
    r = client.get("/dashboard/calls", follow_redirects=False)
    assert r.status_code == 302
    assert "/dashboard/login" in r.headers["location"]


def test_calls_page_empty_after_login(client, db_session, sample_restaurant):
    """Avec login + DB vide → page s'affiche, dit 'Aucun appel'."""
    cookies = _login_dashboard(client)
    r = client.get("/dashboard/calls", cookies=cookies)
    assert r.status_code == 200
    assert "Aucun appel" in r.text or "0 appel" in r.text.lower()


def test_calls_page_lists_recorded_calls(client, db_session, sample_restaurant):
    """Une fois un CallLog en DB, il apparaît dans la liste."""
    cookies = _login_dashboard(client)

    # Crée 2 appels (un normal, un SAV)
    ct1 = CallTranscript(sample_restaurant.id, "+33612345678", "call_1")
    ct1.add_client("Bonjour")
    ct1.add_mia("Salut")
    ct1.flush()

    ct2 = CallTranscript(sample_restaurant.id, "+33687654321", "call_2")
    ct2.add_client("Une réservation")
    ct2.add_tool("create_reservation", {"personnes": 4},
                 {"success": True, "code": "R9XY2"})
    ct2.flush()

    r = client.get("/dashboard/calls", cookies=cookies)
    assert r.status_code == 200
    # Numéros masqués ou non, on doit voir au moins le code de résa
    assert "R9XY2" in r.text
    assert sample_restaurant.nom in r.text


def test_call_detail_page(client, db_session, sample_restaurant):
    """GET /calls/<id> affiche la conversation."""
    cookies = _login_dashboard(client)

    ct = CallTranscript(sample_restaurant.id, "+33612345678", "call_xyz")
    ct.add_client("Vous êtes ouverts ce soir ?")
    ct.add_mia("Oui, de 19h à 22h.")
    call_id = ct.flush()

    r = client.get(f"/dashboard/calls/{call_id}", cookies=cookies)
    assert r.status_code == 200
    assert "Vous êtes ouverts ce soir" in r.text
    assert "Oui, de 19h à 22h" in r.text


def test_call_detail_404_redirects_to_list(client, db_session, sample_restaurant):
    """GET /calls/9999 (inexistant) → redirect vers la liste."""
    cookies = _login_dashboard(client)
    r = client.get("/dashboard/calls/9999", cookies=cookies, follow_redirects=False)
    assert r.status_code == 302
    assert "/dashboard/calls" in r.headers["location"]


def test_flag_sav_updates_call(client, db_session, sample_restaurant):
    """POST /calls/<id>/sav avec note → sav_flagged=True et note enregistrée."""
    cookies = _login_dashboard(client)

    ct = CallTranscript(sample_restaurant.id, "+33612345678", "call_to_flag")
    ct.add_client("Test")
    call_id = ct.flush()

    r = client.post(
        f"/dashboard/calls/{call_id}/sav",
        data={"sav_notes": "Date incorrecte, client a appelé pour annuler"},
        cookies=cookies, follow_redirects=False,
    )
    assert r.status_code == 302

    cl = db_session.query(CallLog).filter(CallLog.id == call_id).first()
    assert cl.sav_flagged is True
    assert "Date incorrecte" in cl.sav_notes


def test_unflag_sav(client, db_session, sample_restaurant):
    """POST avec unflag=1 → sav_flagged=False et note vidée."""
    cookies = _login_dashboard(client)

    ct = CallTranscript(sample_restaurant.id, "+33612345678", "call_to_unflag")
    ct.add_client("Test")
    call_id = ct.flush()

    # D'abord flag
    client.post(f"/dashboard/calls/{call_id}/sav",
                data={"sav_notes": "test"}, cookies=cookies)
    # Puis unflag
    r = client.post(f"/dashboard/calls/{call_id}/sav",
                    data={"unflag": "1"}, cookies=cookies, follow_redirects=False)
    assert r.status_code == 302

    db_session.expire_all()
    cl = db_session.query(CallLog).filter(CallLog.id == call_id).first()
    assert cl.sav_flagged is False
    assert cl.sav_notes is None
