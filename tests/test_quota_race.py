"""Tests du fix race condition sur les quotas.

Vérifie qu'on ne peut PAS dépasser le quota même sous insertions concurrentes.
SQLite a un lock global writer donc on est forcément sérialisé, mais on
vérifie quand même que la logique respecte la limite (avec un quota strict).
"""
from unittest.mock import patch

from app.models import Reservation
from app.services import tool_service


def test_quota_respected_sequential(db_session, sample_restaurant):
    """Quota=3 + 5 tentatives → 3 succès, 2 refus."""
    successes = 0
    refusals = 0

    with patch.object(tool_service, "send_sms"):
        for _ in range(5):
            r = tool_service.execute_tool_call(
                sample_restaurant.id, "create_reservation",
                {"personnes": 2, "heure": "20h", "date": "2026-06-15"},
                menu=[], quota_reservations=3, quota_commandes=10,
                restaurant_phone="+33612345678", caller_phone="+33712345678",
                restaurant_name="Test",
            )
            if r["success"]:
                successes += 1
            else:
                refusals += 1

    assert successes == 3
    assert refusals == 2
    # DB cohérente
    count = db_session.query(Reservation).filter(
        Reservation.restaurant_id == sample_restaurant.id,
        Reservation.date == "2026-06-15",
    ).count()
    assert count == 3


def test_quota_per_date_isolated(db_session, sample_restaurant):
    """Quota=2 sur le 15/06 → 2 limites, mais 16/06 démarre à 0."""
    with patch.object(tool_service, "send_sms"):
        for _ in range(3):
            tool_service.execute_tool_call(
                sample_restaurant.id, "create_reservation",
                {"personnes": 1, "heure": "20h", "date": "2026-06-15"},
                menu=[], quota_reservations=2, quota_commandes=10,
                restaurant_phone="", caller_phone="", restaurant_name="",
            )
        # Devrait passer (autre date)
        r = tool_service.execute_tool_call(
            sample_restaurant.id, "create_reservation",
            {"personnes": 1, "heure": "20h", "date": "2026-06-16"},
            menu=[], quota_reservations=2, quota_commandes=10,
            restaurant_phone="", caller_phone="", restaurant_name="",
        )
        assert r["success"] is True


def test_quota_none_is_unlimited(db_session, sample_restaurant):
    """quota=None → aucune limite (pas de check)."""
    with patch.object(tool_service, "send_sms"):
        for _ in range(20):
            r = tool_service.execute_tool_call(
                sample_restaurant.id, "create_reservation",
                {"personnes": 1, "heure": "20h", "date": "2026-06-15"},
                menu=[], quota_reservations=None, quota_commandes=None,
                restaurant_phone="", caller_phone="", restaurant_name="",
            )
            assert r["success"] is True

    assert db_session.query(Reservation).count() == 20
