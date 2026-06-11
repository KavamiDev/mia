"""Tests des function calls exécutés par MIA (réservation, commande, transfert).

Ces tests touchent la DB SQLite in-memory + mockent les SMS (sinon ils
tentent d'appeler Telnyx/OVH).
"""
from datetime import date
from unittest.mock import patch

from app.services import tool_service
from app.services.tool_service import _code, _spell, execute_tool_call


# ─────────────────────────────────────────
# Code generation
# ─────────────────────────────────────────


def test_code_format():
    """Code = préfixe + 4 caractères depuis l'alphabet sans ambiguïté."""
    c = _code("R")
    assert len(c) == 5
    assert c[0] == "R"
    # Pas de caractères ambigus à l'oral : 0, O, 1, I, L, 5, S, 8, B
    for ch in c[1:]:
        assert ch not in "01ILO5SB8"


def test_code_entropy():
    """Génère 100 codes : doivent tous être différents (sanity check randomness)."""
    codes = {_code("C") for _ in range(100)}
    assert len(codes) > 95  # < 5% de collisions sur 100 = OK pour 27^4 espace


def test_spell_separates_characters():
    """_spell ajoute des espaces pour épeler le code à l'oral."""
    assert _spell("R4T2K") == "R 4 T 2 K"
    assert _spell("ABC") == "A B C"


# ─────────────────────────────────────────
# create_reservation
# ─────────────────────────────────────────


def test_create_reservation_success(sample_restaurant, db_session):
    """Réservation valide → success + code créé + ligne en DB."""
    with patch.object(tool_service, "send_sms", return_value=True) as mock_sms:
        result = execute_tool_call(
            sample_restaurant.id,
            "create_reservation",
            {"personnes": 4, "heure": "20h30", "date": "2026-06-15"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678",
            caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )

        assert result["success"] is True
        assert result["code"].startswith("R")
        assert "4 personnes" in result["recap_vocal"]
        assert "20h30" in result["recap_vocal"]
        # 2 SMS envoyés (resto + client) — defaults sms_to_*=True
        assert mock_sms.call_count == 2


# ─────────────────────────────────────────
# Toggle SMS (économie Brevo)
# ─────────────────────────────────────────


def test_reservation_no_sms_when_both_disabled(sample_restaurant, db_session):
    """Toggles SMS off → AUCUN SMS envoyé mais résa créée."""
    with patch.object(tool_service, "send_sms") as mock_sms:
        result = execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 2, "heure": "20h", "date": "2026-06-15"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
            sms_to_client=False, sms_to_restaurant=False,
        )
        assert result["success"] is True
        mock_sms.assert_not_called()
        # Le récap vocal ne doit PAS mentionner le SMS
        assert "SMS" not in result["recap_vocal"]


def test_reservation_only_resto_sms(sample_restaurant, db_session):
    """sms_to_client=False, sms_to_restaurant=True → 1 seul SMS (au resto)."""
    with patch.object(tool_service, "send_sms") as mock_sms:
        execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 2, "heure": "20h", "date": "2026-06-15"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
            sms_to_client=False, sms_to_restaurant=True,
        )
        assert mock_sms.call_count == 1
        # Le destinataire doit être le resto, pas le client
        assert mock_sms.call_args[0][0] == "+33612345678"


def test_commande_short_sms_format(sample_restaurant, db_session):
    """Les SMS commande sont compacts (< 160 chars = 1 SMS unique chez Brevo)."""
    menu = [
        {"nom_plat": "Pizza Margherita", "prix": 12.0, "description": ""},
        {"nom_plat": "Pizza Reine", "prix": 14.0, "description": ""},
        {"nom_plat": "Coca", "prix": 3.0, "description": ""},
    ]
    with patch.object(tool_service, "send_sms") as mock_sms:
        execute_tool_call(
            sample_restaurant.id, "create_commande",
            {"items": [{"plat": "Pizza Margherita", "qty": 2},
                       {"plat": "Pizza Reine", "qty": 1},
                       {"plat": "Coca", "qty": 2}]},
            menu=menu, quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )
        # 2 appels (resto + client)
        assert mock_sms.call_count == 2
        for call in mock_sms.call_args_list:
            body = call[0][1]
            assert len(body) <= 160, f"SMS trop long ({len(body)} chars) : {body!r}"


def test_date_short_format_in_sms(sample_restaurant, db_session):
    """Le SMS resto contient la date au format court 15/06 (pas 2026-06-15)."""
    with patch.object(tool_service, "send_sms") as mock_sms:
        execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 4, "heure": "20h30", "date": "2026-06-15"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )
        # Vérifie qu'aucun SMS ne contient le format long ISO
        for call in mock_sms.call_args_list:
            body = call[0][1]
            assert "2026-06-15" not in body, f"SMS contient format ISO long : {body!r}"
            assert "15/06" in body, f"SMS devrait contenir 15/06 : {body!r}"


def test_create_reservation_respects_quota(sample_restaurant, db_session):
    """Quota atteint → success=False + message d'erreur poli."""
    from app.models import Reservation

    # Bourre la DB jusqu'au quota
    for i in range(20):
        db_session.add(Reservation(
            restaurant_id=sample_restaurant.id,
            code=f"R{i:04d}",
            personnes=2,
            heure="19h",
            date="2026-06-15",
        ))
    db_session.commit()

    with patch.object(tool_service, "send_sms"):
        result = execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 4, "heure": "20h", "date": "2026-06-15"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )

        assert result["success"] is False
        assert "plus de table" in result["recap_vocal"].lower() or \
               "disponible" in result["recap_vocal"].lower()


def test_create_reservation_no_quota(sample_restaurant, db_session):
    """Si quota_reservations=None → pas de limite."""
    with patch.object(tool_service, "send_sms"):
        for i in range(50):  # bien au-delà de tout quota raisonnable
            r = execute_tool_call(
                sample_restaurant.id, "create_reservation",
                {"personnes": 2, "heure": "20h", "date": "2026-07-01"},
                menu=[], quota_reservations=None, quota_commandes=None,
                restaurant_phone="+33612345678", caller_phone="+33712345678",
                restaurant_name="Chez Marco",
            )
            assert r["success"] is True


def test_create_reservation_minimum_1_person(sample_restaurant, db_session):
    """Si personnes=0 ou négatif → forcé à 1 (sanity)."""
    with patch.object(tool_service, "send_sms"):
        result = execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 0, "heure": "20h", "date": "2026-06-15"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )
        assert result["success"] is True
        # Vérifie en DB que personnes >= 1
        from app.models import Reservation
        r = db_session.query(Reservation).filter_by(code=result["code"]).first()
        assert r is not None
        assert r.personnes >= 1


# ─────────────────────────────────────────
# create_commande
# ─────────────────────────────────────────


def test_create_commande_calculates_total(sample_restaurant, db_session):
    """Total = somme(prix × qty) avec match insensible à la casse."""
    menu = [
        {"nom_plat": "Pizza Margherita", "prix": 12.0, "description": ""},
        {"nom_plat": "Pizza Reine", "prix": 14.0, "description": ""},
        {"nom_plat": "Coca", "prix": 3.0, "description": ""},
    ]
    with patch.object(tool_service, "send_sms", return_value=True) as mock_sms:
        result = execute_tool_call(
            sample_restaurant.id, "create_commande",
            {"items": [
                {"plat": "Pizza Margherita", "qty": 2},
                {"plat": "coca", "qty": 1},  # casse différente → doit matcher
            ]},
            menu=menu, quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )

        assert result["success"] is True
        # 2 × 12 + 1 × 3 = 27
        assert "27" in result["recap_vocal"]
        # SMS envoyés
        assert mock_sms.call_count == 2


def test_create_commande_unknown_plat_zero_price(sample_restaurant, db_session):
    """Plat absent du menu → prix 0 (MIA est censée ne proposer que le menu)."""
    menu = [{"nom_plat": "Pizza Margherita", "prix": 12.0, "description": ""}]
    with patch.object(tool_service, "send_sms"):
        result = execute_tool_call(
            sample_restaurant.id, "create_commande",
            {"items": [{"plat": "Plat Inconnu", "qty": 1}]},
            menu=menu, quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )
        assert result["success"] is True
        # Total = 0 (plat inconnu)
        assert "0.00" in result["recap_vocal"]


def test_create_commande_respects_quota(sample_restaurant, db_session):
    """Quota commandes du jour atteint → success=False."""
    from app.models import Commande

    for i in range(5):
        db_session.add(Commande(
            restaurant_id=sample_restaurant.id,
            code=f"C{i:04d}",
            items=[{"plat": "test", "qty": 1, "prix_unitaire": 1.0}],
            prix_total=1.0,
        ))
    db_session.commit()

    with patch.object(tool_service, "send_sms"):
        result = execute_tool_call(
            sample_restaurant.id, "create_commande",
            {"items": [{"plat": "Pizza Margherita", "qty": 1}]},
            menu=[{"nom_plat": "Pizza Margherita", "prix": 12.0, "description": ""}],
            quota_reservations=20, quota_commandes=5,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )
        assert result["success"] is False
        assert "limite" in result["recap_vocal"].lower() or \
               "atteint" in result["recap_vocal"].lower()


# ─────────────────────────────────────────
# transfer_to_human
# ─────────────────────────────────────────


def test_transfer_sets_flag():
    """transfer_to_human → success + transfer=True + message vocal."""
    result = execute_tool_call(
        1, "transfer_to_human",
        {"raison": "demande client"},
        menu=[], quota_reservations=None, quota_commandes=None,
        restaurant_phone="+33612345678", caller_phone="+33712345678",
        restaurant_name="Chez Marco",
    )
    assert result["success"] is True
    assert result["transfer"] is True
    assert "transfère" in result["recap_vocal"].lower() or \
           "passe" in result["recap_vocal"].lower()


# ─────────────────────────────────────────
# Unknown function
# ─────────────────────────────────────────


def test_unknown_tool_returns_error():
    """Tool inexistant → error sans crash."""
    result = execute_tool_call(
        1, "nuke_database",
        {}, menu=[], quota_reservations=None, quota_commandes=None,
        restaurant_phone="", caller_phone="", restaurant_name="",
    )
    assert result["success"] is False
    assert "Fonction" in result.get("error", "") or "inconnue" in result.get("error", "")


# ─────────────────────────────────────────
# defer_sms : SMS hors du chemin critique vocal
# ─────────────────────────────────────────


def test_defer_sms_reservation_returns_outbox(sample_restaurant, db_session):
    """defer_sms=True → aucun envoi direct, les SMS sont dans sms_outbox."""
    with patch.object(tool_service, "send_sms") as mock_sms:
        result = execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 4, "heure": "20h30", "date": "2026-06-15"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco", defer_sms=True,
        )

        assert result["success"] is True
        mock_sms.assert_not_called()
        outbox = result["sms_outbox"]
        assert len(outbox) == 2
        destinations = [to for to, _ in outbox]
        assert "+33612345678" in destinations  # resto
        assert "+33712345678" in destinations  # client
        # Le contenu reste identique au mode synchrone
        assert any(result["code"] in body for _, body in outbox)


def test_defer_sms_commande_returns_outbox(sample_restaurant, db_session):
    with patch.object(tool_service, "send_sms") as mock_sms:
        result = execute_tool_call(
            sample_restaurant.id, "create_commande",
            {"items": [{"plat": "Pizza Margherita", "qty": 2}]},
            menu=[{"nom_plat": "Pizza Margherita", "prix": 12.0}],
            quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco", defer_sms=True,
        )

        assert result["success"] is True
        mock_sms.assert_not_called()
        assert len(result["sms_outbox"]) == 2


def test_defer_sms_respects_toggles(sample_restaurant, db_session):
    """Toggles SMS désactivés → outbox vide (et toujours aucun envoi direct)."""
    with patch.object(tool_service, "send_sms") as mock_sms:
        result = execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 2, "heure": "19h", "date": "2026-06-16"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
            sms_to_client=False, sms_to_restaurant=False, defer_sms=True,
        )

        assert result["success"] is True
        mock_sms.assert_not_called()
        assert result["sms_outbox"] == []


def test_default_mode_still_sends_synchronously(sample_restaurant, db_session):
    """Sans defer_sms (API inchangée) → envoi direct, pas de sms_outbox."""
    with patch.object(tool_service, "send_sms", return_value=True) as mock_sms:
        result = execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 3, "heure": "21h", "date": "2026-06-17"},
            menu=[], quota_reservations=20, quota_commandes=50,
            restaurant_phone="+33612345678", caller_phone="+33712345678",
            restaurant_name="Chez Marco",
        )

        assert result["success"] is True
        assert mock_sms.call_count == 2
        assert "sms_outbox" not in result
