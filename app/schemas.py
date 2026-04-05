"""
Schémas Pydantic — validation des données entrantes/sortantes de l'API REST.

Chaque modèle *Create est utilisé pour les requêtes POST/PUT.
Chaque modèle *Response est renvoyé par l'API (from_attributes=True permet
la conversion automatique depuis les objets SQLAlchemy).
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ──────────────────────────────────────
# Restaurant
# ──────────────────────────────────────

class RestaurantCreate(BaseModel):
    nom: str = Field(..., min_length=1, max_length=255)
    telephone: str = Field(..., min_length=4, max_length=20)
    adresse: str | None = None
    horaires: str | None = None
    incoming_phone_number: str | None = None
    quota_reservations: int | None = Field(None, ge=1)
    quota_commandes: int | None = Field(None, ge=1)


class RestaurantResponse(RestaurantCreate):
    model_config = {"from_attributes": True}
    id: int
    created_at: datetime


# ──────────────────────────────────────
# Réservation
# ──────────────────────────────────────

class ReservationResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    restaurant_id: int
    code: str
    personnes: int
    heure: str
    telephone: str | None = None
    date: str
    created_at: datetime


# ──────────────────────────────────────
# Menu
# ──────────────────────────────────────

class MenuItemCreate(BaseModel):
    restaurant_id: int
    nom_plat: str = Field(..., min_length=1, max_length=255)
    prix: float = Field(..., gt=0)
    description: str | None = None


class MenuItemResponse(MenuItemCreate):
    model_config = {"from_attributes": True}
    id: int


# ──────────────────────────────────────
# Commande
# ──────────────────────────────────────

class CommandeResponse(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    restaurant_id: int
    code: str
    items: Any
    prix_total: float
    telephone: str | None = None
    created_at: datetime
