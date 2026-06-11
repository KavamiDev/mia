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

# Dimensionnement du pool : uniquement pour PostgreSQL. SQLite (tests) utilise
# un pool spécifique qui n'accepte pas ces arguments.
_pool_kwargs = (
    {"pool_size": settings.db_pool_size,
     "max_overflow": settings.db_max_overflow,
     "pool_timeout": settings.db_pool_timeout}
    if settings.database_url.startswith("postgresql") else {}
)
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300,
                       **_pool_kwargs)
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
