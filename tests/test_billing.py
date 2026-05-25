"""Tests du service de facturation."""
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.models import UsageLog
from app.services.billing_service import (
    all_restaurants_summary,
    cost_for_duration,
    monthly_summary,
    record_usage,
)


# ─────────────────────────────────────────
# cost_for_duration
# ─────────────────────────────────────────


def test_cost_zero_for_zero_duration():
    assert cost_for_duration(0) == 0.0


def test_cost_one_minute():
    """1 min × tarif → cost = tarif."""
    assert cost_for_duration(60) == round(settings.billing_eur_per_minute, 4)


def test_cost_30_seconds_is_half_minute():
    expected = round(0.5 * settings.billing_eur_per_minute, 4)
    assert cost_for_duration(30) == expected


def test_cost_negative_clamped_to_zero():
    """Durée négative (cas pathologique) → 0, pas crash."""
    assert cost_for_duration(-5) == 0.0


# ─────────────────────────────────────────
# record_usage + monthly_summary
# ─────────────────────────────────────────


def test_record_usage_creates_row(db_session, sample_restaurant):
    u = record_usage(db_session, sample_restaurant.id, call_log_id=None,
                     duration_seconds=120)
    assert u.id is not None
    assert u.restaurant_id == sample_restaurant.id
    assert u.duration_seconds == 120
    assert u.cost_estimate_eur == cost_for_duration(120)


def test_monthly_summary_aggregates_correctly(db_session, sample_restaurant):
    """3 appels de 60s → 3 appels, 180s, 3 × tarif minute."""
    for _ in range(3):
        record_usage(db_session, sample_restaurant.id, None, 60)

    s = monthly_summary(db_session, sample_restaurant.id)
    assert s["call_count"] == 3
    assert s["total_seconds"] == 180
    expected_eur = round(3 * settings.billing_eur_per_minute, 4)
    assert abs(s["total_eur"] - expected_eur) < 1e-6


def test_monthly_summary_ignores_other_months(db_session, sample_restaurant):
    """Un appel dans un autre mois ne doit pas être comptabilisé."""
    now = datetime.now(UTC)
    # Crée manuellement un usage dans le mois précédent
    last_month = now.replace(day=1) - timedelta(days=15)
    db_session.add(UsageLog(
        restaurant_id=sample_restaurant.id, call_log_id=None,
        duration_seconds=3600, cost_estimate_eur=99.99,
        created_at=last_month,
    ))
    db_session.commit()

    s = monthly_summary(db_session, sample_restaurant.id)
    assert s["call_count"] == 0
    assert s["total_eur"] == 0.0


def test_monthly_summary_for_other_restaurant_isolated(db_session, sample_restaurant):
    """L'usage d'un resto ne pollue pas l'agrégat d'un autre."""
    record_usage(db_session, sample_restaurant.id, None, 60)
    # Restaurant_id qui n'existe pas → query retourne 0
    s = monthly_summary(db_session, 9999)
    assert s["call_count"] == 0
    assert s["total_eur"] == 0.0


def test_all_restaurants_summary(db_session, sample_restaurant):
    """all_restaurants_summary retourne 1 ligne par resto avec usage."""
    record_usage(db_session, sample_restaurant.id, None, 90)
    summaries = all_restaurants_summary(db_session)
    assert len(summaries) == 1
    assert summaries[0]["restaurant_id"] == sample_restaurant.id
    assert summaries[0]["total_seconds"] == 90
