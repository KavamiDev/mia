"""
Modèles SQLAlchemy — représentation des tables PostgreSQL.

Tables :
  restaurants   — Fiche restaurant (nom, tel, horaires, quotas, numéro Telnyx)
  reservations  — Réservation créée par MIA (code unique, personnes, date/heure)
  commandes     — Commande à emporter créée par MIA (code unique, items JSONB)
  menu          — Plats proposés par chaque restaurant (nom, prix, description)
  call_logs     — Transcript + tool calls de chaque appel téléphonique (SAV)
"""
from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
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
    # Toggles SMS — un SMS coûte ~0.05 € métropole, ~0.10 € Réunion (Brevo).
    # Désactiver pour les restos qui consultent le dashboard et veulent éviter
    # les frais SMS. Default True/True pour rétro-compat.
    sms_to_client = Column(Boolean, nullable=False, default=True, server_default="true")
    sms_to_restaurant = Column(Boolean, nullable=False, default=True, server_default="true")
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


class User(Base):
    """Utilisateur du dashboard admin (multi-tenant).

    Un user appartient à UN restaurant (ou aucun s'il est admin global).
    Le password est stocké en PBKDF2-SHA256 100k itérations (stdlib, pas de
    dépendance externe — passlib/bcrypt ajouteraient ~5 MB pour 0 gain ici).

    Format password_hash : `<salt_b64>$<hash_b64>`
    """
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    # restaurant_id NULL pour les admins globaux (voient tous les restos).
    restaurant_id = Column(Integer, ForeignKey("restaurants.id", ondelete="CASCADE"),
                           nullable=True, index=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    restaurant = relationship("Restaurant")


class UsageLog(Base):
    """Trace d'usage facturable par appel (billing + alerte seuil).

    Une ligne par appel téléphonique : durée + coût estimé en EUR. Permet de
    calculer rapidement la consommation mensuelle d'un restaurant et d'alerter
    quand un seuil est dépassé.

    cost_estimate_eur = duration_seconds × _COST_PER_SECOND_EUR (cf. billing_service)
    """
    __tablename__ = "usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    call_log_id = Column(Integer, ForeignKey("call_logs.id", ondelete="SET NULL"),
                         nullable=True)
    duration_seconds = Column(Integer, nullable=False, default=0)
    cost_estimate_eur = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)


class CallLog(Base):
    """Trace d'un appel téléphonique complet (transcript + tool calls).

    Objectif : SAV. Quand le restaurateur dit « la résa de jeudi est fausse »,
    on retrouve l'appel d'origine + ce que MIA a entendu et fait.

    Champs :
      transcript : liste chronologique [{ts, who: 'client'|'mia', text}]
      tool_calls : liste [{ts, name, args, success, code, blocked_reason}]
      reservation_code / commande_code : liens vers les codes générés (si tool exec OK)
      sav_flagged / sav_notes : drapeau et note libre saisis depuis le dashboard

    ⚠ RGPD : transcript contient des données personnelles (voix de l'appelant).
    Mettre en place une purge auto (ex: cron quotidien) au-delà de 30 jours.
    """
    __tablename__ = "call_logs"

    id = Column(Integer, primary_key=True, index=True)
    restaurant_id = Column(Integer, ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=True, index=True)
    # Téléphone appelant en E.164 (vide pour les appels anonymes/masqués).
    caller_phone = Column(String(20), nullable=True, index=True)
    # ID Telnyx — utile pour corréler avec les logs côté provider.
    call_control_id = Column(String(64), nullable=True)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    # Transcript chrono : [{"ts": iso, "who": "client"|"mia", "text": "..."}]
    transcript = Column(JSONB, nullable=False, default=list)
    # Function calls : [{"ts", "name", "args", "success", "code", "blocked_reason"}]
    tool_calls = Column(JSONB, nullable=False, default=list)
    # Liens directs vers les codes générés (recherche rapide).
    reservation_code = Column(String(10), nullable=True, index=True)
    commande_code = Column(String(10), nullable=True, index=True)
    # SAV : flag manuel + note libre du restaurateur/admin.
    sav_flagged = Column(Boolean, nullable=False, default=False, index=True)
    sav_notes = Column(Text, nullable=True)

    restaurant = relationship("Restaurant")
