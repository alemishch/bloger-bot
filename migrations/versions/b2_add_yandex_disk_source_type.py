"""add yandex_disk to sourcetype enum

Revision ID: b2_yandex_disk
Revises: b1_user_sessions
Create Date: 2026-03-20
"""
from typing import Sequence, Union
from alembic import op

revision: str = "b2_yandex_disk"
down_revision: Union[str, None] = "b1_user_sessions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE sourcetype ADD VALUE IF NOT EXISTS 'yandex_disk'")


def downgrade() -> None:
    pass
