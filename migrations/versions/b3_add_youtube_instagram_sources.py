"""add instagram_reels, instagram_post to sourcetype enum

Revision ID: b3_ig_yt
Revises: b2_yandex_disk
Create Date: 2026-03-21
"""
from typing import Sequence, Union
from alembic import op

revision: str = "b3_ig_yt"
down_revision: Union[str, None] = "b2_yandex_disk"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE sourcetype ADD VALUE IF NOT EXISTS 'instagram_reels'")
    op.execute("ALTER TYPE sourcetype ADD VALUE IF NOT EXISTS 'instagram_post'")


def downgrade() -> None:
    pass
