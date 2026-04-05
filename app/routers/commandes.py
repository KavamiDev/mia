"""
API REST — Lecture des commandes.

Protégée par X-API-Key. Les commandes sont créées uniquement
par MIA via les tools OpenAI (tool_service.py), pas via cette API.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app.models import Commande
from app.schemas import CommandeResponse

router = APIRouter(prefix="/commandes", tags=["commandes"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[CommandeResponse])
def list_commandes(restaurant_id: int | None = None, limit: int = 200, db: Session = Depends(get_db)):
    """Liste les commandes, optionnellement filtrées par restaurant."""
    q = db.query(Commande)
    if restaurant_id:
        q = q.filter(Commande.restaurant_id == restaurant_id)
    return q.order_by(Commande.created_at.desc()).limit(limit).all()
