"""Calcul d'usage et facturation estimée par restaurant.

Pas de Stripe pour le moment — on agrège juste les durées d'appels pour
afficher la consommation mensuelle au restaurateur. Quand un seuil est
dépassé, on log un warning (et envoie à Sentry si configuré).

Une ligne `UsageLog` est créée par appel téléphonique terminé. Le
`call_log_service.CallTranscript.flush()` invoque ce module pour enregistrer
l'usage en même temps que le transcript.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import UsageLog

log = logging.getLogger("mia.billing")


def cost_for_duration(duration_seconds: int) -> float:
    """Calcule le coût estimé d'un appel à partir de sa durée.

    Cost = (duration_seconds / 60) × BILLING_EUR_PER_MINUTE
    Arrondi à 4 décimales pour conserver la précision sub-centime.
    """
    minutes = max(duration_seconds, 0) / 60.0
    return round(minutes * settings.billing_eur_per_minute, 4)


def record_usage(db: Session, restaurant_id: int, call_log_id: int | None,
                 duration_seconds: int) -> UsageLog:
    """Enregistre un UsageLog. Appelé par CallTranscript.flush()."""
    cost = cost_for_duration(duration_seconds)
    usage = UsageLog(
        restaurant_id=restaurant_id,
        call_log_id=call_log_id,
        duration_seconds=duration_seconds,
        cost_estimate_eur=cost,
    )
    db.add(usage)
    db.commit()
    db.refresh(usage)

    # Alerte si le restaurant dépasse le seuil mensuel
    _check_monthly_threshold(db, restaurant_id)
    return usage


def monthly_summary(db: Session, restaurant_id: int,
                    year: int | None = None, month: int | None = None) -> dict:
    """Agrège la consommation mensuelle d'un restaurant.

    Returns:
        {
          'restaurant_id': int,
          'year': int, 'month': int,
          'call_count': int,
          'total_seconds': int,
          'total_eur': float,
        }
    """
    now = datetime.now(UTC)
    year = year or now.year
    month = month or now.month

    # Borne début/fin du mois (UTC).
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)

    q = db.query(
        func.count(UsageLog.id),
        func.coalesce(func.sum(UsageLog.duration_seconds), 0),
        func.coalesce(func.sum(UsageLog.cost_estimate_eur), 0.0),
    ).filter(
        UsageLog.restaurant_id == restaurant_id,
        UsageLog.created_at >= start,
        UsageLog.created_at < end,
    )
    call_count, total_seconds, total_eur = q.one()

    return {
        "restaurant_id": restaurant_id,
        "year": year, "month": month,
        "call_count": int(call_count or 0),
        "total_seconds": int(total_seconds or 0),
        "total_eur": float(total_eur or 0.0),
    }


def all_restaurants_summary(db: Session, year: int | None = None,
                            month: int | None = None) -> list[dict]:
    """Agrégat par restaurant pour le mois courant (ou spécifié).

    Vue admin : tous les restos triés par consommation décroissante.
    """
    now = datetime.now(UTC)
    year = year or now.year
    month = month or now.month

    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)

    rows = db.query(
        UsageLog.restaurant_id,
        func.count(UsageLog.id),
        func.coalesce(func.sum(UsageLog.duration_seconds), 0),
        func.coalesce(func.sum(UsageLog.cost_estimate_eur), 0.0),
    ).filter(
        UsageLog.created_at >= start,
        UsageLog.created_at < end,
    ).group_by(UsageLog.restaurant_id).all()

    return [{
        "restaurant_id": rid,
        "call_count": int(cnt or 0),
        "total_seconds": int(secs or 0),
        "total_eur": float(eur or 0.0),
    } for rid, cnt, secs, eur in rows]


def _check_monthly_threshold(db: Session, restaurant_id: int) -> None:
    """Log + Sentry si le restaurant dépasse le seuil mensuel.

    Idempotent à l'échelle de la journée : log peut être noisy si appelé à
    chaque enregistrement, mais c'est volontaire — Sentry dédoublonne par
    fingerprint et ça nous fait des données réelles pour ajuster le seuil.
    """
    summary = monthly_summary(db, restaurant_id)
    if summary["total_eur"] < settings.billing_alert_eur:
        return

    log.warning(
        "BILLING : restaurant_id=%d dépasse %.2f € ce mois (consommation=%.2f €, %d appels)",
        restaurant_id, settings.billing_alert_eur,
        summary["total_eur"], summary["call_count"],
    )
    # Envoi vers Sentry si activé. Le log warning sera capturé automatiquement
    # par l'intégration logging, mais on peut aussi forcer un capture_message
    # pour mettre des tags business (restaurant_id).
    try:
        import sentry_sdk
        sentry_sdk.set_tag("restaurant_id", str(restaurant_id))
        sentry_sdk.capture_message(
            f"Billing threshold exceeded (restaurant={restaurant_id})",
            level="warning",
        )
    except Exception:
        pass  # Sentry pas configuré — pas grave
