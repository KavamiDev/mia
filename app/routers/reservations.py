"""
API REST — Lecture des réservations.

Protégée par X-API-Key. Les réservations sont créées uniquement
par MIA via les tools OpenAI (tool_service.py), pas via cette API.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app.models import Reservation
from app.schemas import ReservationResponse

router = APIRouter(prefix="/reservations", tags=["reservations"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[ReservationResponse])
def list_reservations(restaurant_id: int | None = None, limit: int = 200, db: Session = Depends(get_db)):
    """Liste les réservations, optionnellement filtrées par restaurant."""
    q = db.query(Reservation)
    if restaurant_id:
        q = q.filter(Reservation.restaurant_id == restaurant_id)
    return q.order_by(Reservation.date.desc(), Reservation.heure.desc()).limit(limit).all()
