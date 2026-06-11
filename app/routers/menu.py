"""
API REST — Gestion du menu des restaurants.

Protégée par X-API-Key. Permet l'ajout unitaire, en masse (bulk),
la consultation et la suppression de plats.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.database import get_db
from app.models import MenuItem, Restaurant
from app.schemas import MenuItemCreate, MenuItemResponse
from app.services import restaurant_cache

router = APIRouter(prefix="/menu", tags=["menu"], dependencies=[Depends(require_api_key)])


class MenuBulkItem(BaseModel):
    nom_plat: str = Field(..., min_length=1, max_length=255)
    prix: float = Field(..., gt=0)
    description: str | None = None


class MenuBulkRequest(BaseModel):
    items: list[MenuBulkItem] = Field(..., min_length=1)


@router.get("/restaurant/{restaurant_id}", response_model=list[MenuItemResponse])
def get_menu(restaurant_id: int, db: Session = Depends(get_db)):
    """Retourne tous les plats d'un restaurant."""
    return db.query(MenuItem).filter(MenuItem.restaurant_id == restaurant_id).all()


@router.post("/restaurant/{restaurant_id}/bulk")
def create_menu_bulk(restaurant_id: int, data: MenuBulkRequest, db: Session = Depends(get_db)):
    """Import en masse du menu d'un restaurant."""
    restaurant = db.query(Restaurant).filter(Restaurant.id == restaurant_id).first()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant non trouvé")
    created = []
    for item in data.items:
        m = MenuItem(restaurant_id=restaurant_id, nom_plat=item.nom_plat, prix=item.prix, description=item.description)
        db.add(m)
        created.append({"nom_plat": m.nom_plat, "prix": m.prix})
    db.commit()
    restaurant_cache.invalidate(restaurant_id)
    return {"ok": True, "added": len(created), "items": created}


@router.post("", response_model=MenuItemResponse, status_code=201)
def create_menu_item(data: MenuItemCreate, db: Session = Depends(get_db)):
    """Ajoute un plat au menu."""
    m = MenuItem(**data.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    restaurant_cache.invalidate(m.restaurant_id)
    return m


@router.delete("/{item_id}")
def delete_menu_item(item_id: int, db: Session = Depends(get_db)):
    """Supprime un plat du menu."""
    m = db.query(MenuItem).filter(MenuItem.id == item_id).first()
    if not m:
        raise HTTPException(status_code=404, detail="Plat non trouvé")
    rid = m.restaurant_id
    db.delete(m)
    db.commit()
    restaurant_cache.invalidate(rid)
    return {"ok": True}
