"""Service de persistance des transcripts d'appel (dashboard SAV).

Pendant un appel, le bridge realtime accumule en mémoire :
  - chaque transcription client (whisper)
  - chaque réponse vocale MIA (transcript)
  - chaque function call (avec args + résultat)

À la fin de l'appel, le tout est flushé en DB dans la table call_logs.
Le dashboard /dashboard/calls/<id> affichera ensuite le transcript chrono.

⚠ Pas de PII en clair dans les logs serveur — tout va en DB protégée.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.database import SessionLocal
from app.models import CallLog
from app.services.billing_service import record_usage

log = logging.getLogger("mia.calllog")


class CallTranscript:
    """Accumulateur d'événements pour la durée d'un appel.

    Usage :
        ct = CallTranscript(restaurant_id=1, caller_phone="+33...", call_control_id="abc")
        ct.add_client("Bonjour vous avez de la pizza ?")
        ct.add_mia("Oui on a Margherita et Reine, vous voulez quoi ?")
        ct.add_tool("create_commande", {...args}, {"success": True, "code": "C4T2K"})
        ct.flush()  # → écrit en DB
    """

    def __init__(self, restaurant_id: int | None, caller_phone: str,
                 call_control_id: str) -> None:
        self.restaurant_id = restaurant_id
        self.caller_phone = caller_phone or None
        self.call_control_id = call_control_id or None
        self.started_at = datetime.now(UTC)
        self.transcript: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        # Codes générés (extraits du dernier tool exec réussi pour lien rapide)
        self.reservation_code: str | None = None
        self.commande_code: str | None = None
        self._flushed = False

    # ───── Méthodes d'ajout (appelées par le bridge) ─────

    def add_client(self, text: str) -> None:
        if not text:
            return
        self.transcript.append({
            "ts": datetime.now(UTC).isoformat(),
            "who": "client",
            "text": text.strip(),
        })

    def add_mia(self, text: str) -> None:
        if not text:
            return
        self.transcript.append({
            "ts": datetime.now(UTC).isoformat(),
            "who": "mia",
            "text": text.strip(),
        })

    def add_tool(self, name: str, args: dict, result: dict,
                 blocked_reason: str | None = None) -> None:
        """Ajoute un tool call avec son résultat. Si blocked_reason est défini,
        c'est que la double-confirmation a empêché l'exécution."""
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "name": name,
            "args": args,
            "success": bool(result.get("success")),
        }
        if result.get("code"):
            entry["code"] = result["code"]
            # Lien direct vers la résa/commande pour le dashboard
            if name == "create_reservation":
                self.reservation_code = result["code"]
            elif name == "create_commande":
                self.commande_code = result["code"]
        if blocked_reason:
            entry["blocked_reason"] = blocked_reason
            entry["success"] = False
        self.tool_calls.append(entry)

    # ───── Flush en DB ─────

    def flush(self) -> int | None:
        """Persiste l'appel en DB. Idempotent : flush deux fois = no-op.

        Returns:
            L'id du CallLog créé, ou None si pas d'événement (appel vide / échoué).
        """
        if self._flushed:
            return None
        self._flushed = True

        # Pas la peine d'écrire un appel sans aucun échange
        if not self.transcript and not self.tool_calls:
            return None

        ended = datetime.now(UTC)
        duration = int((ended - self.started_at).total_seconds())

        db = SessionLocal()
        try:
            cl = CallLog(
                restaurant_id=self.restaurant_id,
                caller_phone=self.caller_phone,
                call_control_id=self.call_control_id,
                started_at=self.started_at,
                ended_at=ended,
                duration_seconds=duration,
                transcript=self.transcript,
                tool_calls=self.tool_calls,
                reservation_code=self.reservation_code,
                commande_code=self.commande_code,
            )
            db.add(cl)
            db.commit()
            db.refresh(cl)

            # Enregistre l'usage facturable (billing) — best effort.
            # Si restaurant_id manquant (appel non routé), skip silencieusement.
            if self.restaurant_id and duration > 0:
                try:
                    record_usage(db, self.restaurant_id, cl.id, duration)
                except Exception as e:
                    log.warning("record_usage échoué pour CallLog #%d : %s", cl.id, e)

            log.info("CallLog #%d flushé (%ds, %d échanges, %d tools)",
                     cl.id, duration, len(self.transcript), len(self.tool_calls))
            return cl.id
        except Exception as e:
            db.rollback()
            log.exception("Flush CallLog échec : %s", e)
            return None
        finally:
            db.close()
