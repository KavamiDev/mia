"""
Connexion PostgreSQL via SQLAlchemy.

pool_pre_ping=True  : vérifie la connexion avant chaque requête (évite les
                      erreurs « connection closed » après un redémarrage DB).
pool_recycle=300     : renouvelle les connexions toutes les 5 min pour les
                      environnements qui coupent les connexions inactives.
"""
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Classe de base pour tous les modèles SQLAlchemy."""


def get_db() -> Generator[Session]:
    """Dépendance FastAPI : fournit une session DB et la ferme après usage."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
