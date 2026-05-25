"""Tests du script de purge RGPD."""
from datetime import UTC, datetime, timedelta

from app.models import CallLog
from scripts.purge_old_calls import purge_caller_phone, purge_old_call_logs


def test_purge_keeps_recent(db_session, sample_restaurant):
    """CallLog récent (< retention_days) → NON supprimé."""
    cl = CallLog(
        restaurant_id=sample_restaurant.id, caller_phone="+33612345678",
        started_at=datetime.now(UTC) - timedelta(days=2),
        transcript=[], tool_calls=[],
    )
    db_session.add(cl)
    db_session.commit()

    deleted = purge_old_call_logs(days=30)
    assert deleted == 0
    assert db_session.query(CallLog).count() == 1


def test_purge_removes_old(db_session, sample_restaurant):
    """CallLog > retention_days → supprimé."""
    cl = CallLog(
        restaurant_id=sample_restaurant.id, caller_phone="+33612345678",
        started_at=datetime.now(UTC) - timedelta(days=45),
        transcript=[], tool_calls=[],
    )
    db_session.add(cl)
    db_session.commit()

    deleted = purge_old_call_logs(days=30)
    assert deleted == 1
    assert db_session.query(CallLog).count() == 0


def test_purge_dry_run_no_delete(db_session, sample_restaurant):
    """--dry-run → count seulement, pas de suppression."""
    cl = CallLog(
        restaurant_id=sample_restaurant.id, caller_phone="+33612345678",
        started_at=datetime.now(UTC) - timedelta(days=60),
        transcript=[], tool_calls=[],
    )
    db_session.add(cl)
    db_session.commit()

    deleted = purge_old_call_logs(days=30, dry_run=True)
    assert deleted == 1  # rapporté
    assert db_session.query(CallLog).count() == 1  # toujours là


def test_purge_by_phone(db_session, sample_restaurant):
    """Droit à l'effacement : supprime UNIQUEMENT le téléphone visé."""
    db_session.add_all([
        CallLog(restaurant_id=sample_restaurant.id, caller_phone="+33612345678",
                started_at=datetime.now(UTC), transcript=[], tool_calls=[]),
        CallLog(restaurant_id=sample_restaurant.id, caller_phone="+33612345678",
                started_at=datetime.now(UTC), transcript=[], tool_calls=[]),
        CallLog(restaurant_id=sample_restaurant.id, caller_phone="+33799999999",
                started_at=datetime.now(UTC), transcript=[], tool_calls=[]),
    ])
    db_session.commit()
    assert db_session.query(CallLog).count() == 3

    deleted = purge_caller_phone("+33612345678")
    assert deleted == 2
    assert db_session.query(CallLog).count() == 1
    remaining = db_session.query(CallLog).first()
    assert remaining.caller_phone == "+33799999999"


def test_purge_phone_not_found_no_op(db_session, sample_restaurant):
    """Numéro inconnu → 0 supprimé, pas d'erreur."""
    deleted = purge_caller_phone("+33000000000")
    assert deleted == 0
