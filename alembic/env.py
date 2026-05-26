"""Alembic env — configuration des migrations MIA.

Particularités :
  - URL DB lue depuis app.config.settings (pas depuis alembic.ini)
    → un seul endroit de vérité pour la conf.
  - target_metadata = Base.metadata pour autogénération via :
        alembic revision --autogenerate -m "..."
  - render_as_batch=True pour que les migrations marchent aussi sur SQLite
    (tests E2E) : postgres ignore l'option, SQLite l'utilise pour émuler
    les ALTER TABLE qu'il ne supporte pas nativement.
"""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Imports app : doivent être après l'init pour récupérer la config.
from app import models  # noqa: F401  — peuple Base.metadata
from app.config import settings as app_settings
from app.database import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# ⚠ On NE passe PAS par config.set_main_option("sqlalchemy.url", ...) car
# configparser interprète les `%` dans l'URL comme syntaxe d'interpolation.
# Un password postgres contenant `@` (encodé `%40`) fait planter alembic.
# Solution : on utilise directement app_settings.database_url plus bas.
_DB_URL = app_settings.database_url


def run_migrations_offline() -> None:
    """Génère le SQL sans connexion (alembic upgrade --sql)."""
    context.configure(
        url=_DB_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Mode normal : applique les migrations sur la DB connectée."""
    # create_engine direct sur l'URL (pas via engine_from_config qui passerait
    # par configparser → bug interpolation sur le `%` du password).
    connectable = create_engine(_DB_URL, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
