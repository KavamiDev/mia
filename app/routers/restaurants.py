"""
API REST — Gestion des restaurants.

Toutes les routes sont protégées par X-API-Key (voir auth.py).
Utilisé par des outils externes ou des scripts d'administration.
Le dashboard admin utilise ses propres routes (/dashboard/*).
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app.models import Restaurant
from app.schemas import RestaurantCreate, RestaurantResponse
from app.services import restaurant_cache

router = APIRouter(prefix="/restaurants", tags=["restaurants"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=list[RestaurantResponse])
def list_restaurants(db: Session = Depends(get_db)):
    return db.query(Restaurant).all()


@router.get("/{restaurant_id}", response_model=RestaurantResponse)
def get_restaurant(restaurant_id: int, db: Session = Depends(get_db)):
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Restaurant non trouvé")
    return r


@router.post("", response_model=RestaurantResponse, status_code=201)
def create_restaurant(data: RestaurantCreate, db: Session = Depends(get_db)):
    r = Restaurant(**data.model_dump())
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


@router.put("/{restaurant_id}", response_model=RestaurantResponse)
def update_restaurant(restaurant_id: int, data: RestaurantCreate, db: Session = Depends(get_db)):
    r = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Restaurant non trouvé")
    for k, v in data.model_dump().items():
        setattr(r, k, v)
    db.commit()
    db.refresh(r)
    restaurant_cache.invalidate(restaurant_id)
    return r
