"""Purge RGPD des transcripts d'appel anciens.

Usage :
    python -m scripts.purge_old_calls              # purge à 30 jours (défaut env)
    python -m scripts.purge_old_calls --days 60    # override
    python -m scripts.purge_old_calls --dry-run    # liste sans supprimer

Recommandation : exécuter quotidiennement via cron / systemd timer.
Exemple crontab :
    0 3 * * * cd /opt/mia && .venv/bin/python -m scripts.purge_old_calls >> /var/log/mia-purge.log 2>&1

Logique :
  - Les CallLog dont `started_at < now - retention_days` sont supprimés.
  - Les UsageLog référençant ces CallLog passent en NULL (ON DELETE SET NULL)
    → l'historique de facturation est CONSERVÉ (anonymisé : plus de lien transcript).
  - Pas de soft-delete : pour le RGPD on veut une suppression réelle.

Audit :
  - Le nombre de lignes supprimées est logué et imprimé sur stdout.
  - Si --dry-run, on print juste la liste sans toucher la DB.
"""
import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

# Permet d'exécuter le script depuis n'importe où : ajoute la racine au path.
import os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import CallLog  # noqa: E402

logging.basicConfig(format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                    level=logging.INFO)
log = logging.getLogger("mia.purge")


def purge_old_call_logs(days: int, dry_run: bool = False) -> int:
    """Supprime les CallLog plus vieux que `days` jours.

    Returns:
        Le nombre de lignes supprimées (ou qui auraient été supprimées en dry-run).
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    db = SessionLocal()
    try:
        q = db.query(CallLog).filter(CallLog.started_at < cutoff)
        count = q.count()

        if dry_run:
            log.info("[DRY-RUN] %d CallLog seraient supprimés (started_at < %s)",
                     count, cutoff.isoformat())
            # Affiche les 5 premiers pour vérification
            for cl in q.order_by(CallLog.started_at).limit(5).all():
                log.info("  → #%d caller=%s started=%s", cl.id,
                         cl.caller_phone or "anon", cl.started_at)
            return count

        if count == 0:
            log.info("Aucun CallLog à purger (cutoff %s).", cutoff.isoformat())
            return 0

        # bulk delete — plus rapide qu'un par un, et déclenche les ON DELETE
        # SET NULL des UsageLog.
        q.delete(synchronize_session=False)
        db.commit()
        log.info("✅ %d CallLog supprimés (started_at < %s)", count, cutoff.isoformat())
        return count
    except Exception as e:
        db.rollback()
        log.exception("Échec purge : %s", e)
        raise
    finally:
        db.close()


def purge_caller_phone(phone: str, dry_run: bool = False) -> int:
    """Droit à l'effacement RGPD : supprime tous les CallLog d'un numéro précis.

    À utiliser quand un client demande explicitement la suppression de ses données.
    Le numéro doit être au format E.164 (+33...).
    """
    db = SessionLocal()
    try:
        q = db.query(CallLog).filter(CallLog.caller_phone == phone)
        count = q.count()
        if dry_run:
            log.info("[DRY-RUN] %d CallLog seraient supprimés pour %s", count, phone)
            return count
        if count == 0:
            log.info("Aucun CallLog trouvé pour %s.", phone)
            return 0
        q.delete(synchronize_session=False)
        db.commit()
        log.info("✅ %d CallLog supprimés pour %s (droit à l'effacement)", count, phone)
        return count
    except Exception as e:
        db.rollback()
        log.exception("Échec purge par téléphone : %s", e)
        raise
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge RGPD des CallLog anciens")
    parser.add_argument("--days", type=int, default=settings.rgpd_call_log_retention_days,
                        help="Rétention en jours (défaut : %(default)d)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche ce qui serait supprimé sans toucher la DB")
    parser.add_argument("--phone", type=str, default=None,
                        help="Purge uniquement les CallLog d'un numéro (droit à l'effacement)")
    args = parser.parse_args()

    if args.phone:
        purge_caller_phone(args.phone, dry_run=args.dry_run)
    else:
        purge_old_call_logs(args.days, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
