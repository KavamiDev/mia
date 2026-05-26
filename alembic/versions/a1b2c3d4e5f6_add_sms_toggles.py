"""add sms toggles to restaurants

Ajoute les colonnes sms_to_client et sms_to_restaurant sur la table
restaurants. Defaut True pour rétro-compat (comportement antérieur :
toujours envoyer les SMS).

Permet de réduire les coûts Brevo (0.045 €/SMS métropole, 0.10 €/SMS
Réunion) en désactivant l'envoi sur les restaus qui consultent le
dashboard.

Revision ID: a1b2c3d4e5f6
Revises: 0f1b217c2182
Create Date: 2026-05-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "0f1b217c2182"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL avec default True : les lignes existantes héritent de True.
    # server_default est CRITIQUE — sans lui, postgres refuse l'ALTER sur
    # une table non-vide à cause des lignes existantes.
    op.add_column(
        "restaurants",
        sa.Column("sms_to_client", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
    )
    op.add_column(
        "restaurants",
        sa.Column("sms_to_restaurant", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("restaurants", "sms_to_restaurant")
    op.drop_column("restaurants", "sms_to_client")
