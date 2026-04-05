"""
Modèles SQLAlchemy — représentation des tables PostgreSQL.

Tables :
  restaurants   — Fiche restaurant (nom, tel, horaires, quotas, numéro Telnyx)
  reservations  — Réservation créée par MIA (code unique, personnes, date/heure)
  commandes     — Commande à emporter créée par MIA (code unique, items JSONB)
  menu          — Plats proposés par chaque restaurant (nom, prix, description)
"""
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base


class Restaurant(Base):
    __tablename__ = "restaurants"

    id = Column(Integer, primary_key=True, index=True)
    nom = Column(String(255), nullable=False)
    # Numéro du restaurateur (reçoit les SMS de notification).
    telephone = Column(String(20), nullable=False)
    adresse = Column(Text, nullable=True)
    horaires = Column(Text, nullable=True)
    # Numéro Telnyx assigné : permet de router l'appel entrant vers ce restaurant.
    incoming_phone_number = Column(String(20), unique=True, nullable=True, index=True)
    # Limites journalières. NULL = illimité.
    quota_reservations = Column(Integer, nullable=True)
    quota_commandes = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    reservations = relationship("Reservation", back_populates="restaurant", cascade="all, delete-orphan")
    commandes = relationship("Commande", back_populates="restaurant", cascade="all, delete-orphan")
    menu_items = relationship("MenuItem", back_populates="restaurant", cascade="all, delete-orphan")


class Reservation(Base):
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False)
    # Code court unique (ex: R4T2K) communiqué au client et au restaurateur.
    code = Column(String(10), nullable=False, index=True)
    personnes = Column(Integer, nullable=False)
    heure = Column(String(10), nullable=False)
    # Numéro de l'appelant, récupéré automatiquement (jamais demandé par MIA).
    telephone = Column(String(20), nullable=True)
    date = Column(String(10), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    restaurant = relationship("Restaurant", back_populates="reservations")


class Commande(Base):
    __tablename__ = "commandes"

    id = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False)
    code = Column(String(10), nullable=False, index=True)
    # Liste d'items [{plat, qty, prix_unitaire}] — stockée en JSONB.
    items = Column(JSONB, nullable=False)
    prix_total = Column(Float, nullable=False, default=0)
    telephone = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    restaurant = relationship("Restaurant", back_populates="commandes")


class MenuItem(Base):
    __tablename__ = "menu"

    id = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False)
    nom_plat = Column(String(255), nullable=False)
    prix = Column(Float, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    restaurant = relationship("Restaurant", back_populates="menu_items")
