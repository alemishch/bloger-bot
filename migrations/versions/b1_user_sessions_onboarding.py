"""add chat_sessions, chat_messages, onboarding_responses; extend users table

Revision ID: b1_user_sessions
Revises: a8a60c03fb9b
Create Date: 2026-03-16
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b1_user_sessions"
down_revision: Union[str, None] = "a8a60c03fb9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE onboardingstatus AS ENUM ('not_started', 'in_progress', 'completed');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)

    op.add_column("users", sa.Column("blogger_id", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("onboarding_status", sa.Text(), nullable=True, server_default="not_started"))
    op.add_column("users", sa.Column("onboarding_step", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("long_term_profile", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("amocrm_contact_id", sa.String(64), nullable=True))

    op.execute("UPDATE users SET blogger_id = 'yuri' WHERE blogger_id IS NULL")
    op.execute("ALTER TABLE users ALTER COLUMN blogger_id SET NOT NULL")
    op.execute("ALTER TABLE users ALTER COLUMN blogger_id TYPE bloggerid USING blogger_id::bloggerid")
    op.execute("ALTER TABLE users ALTER COLUMN onboarding_status DROP DEFAULT")
    op.execute("ALTER TABLE users ALTER COLUMN onboarding_status TYPE onboardingstatus USING onboarding_status::onboardingstatus")
    op.execute("ALTER TABLE users ALTER COLUMN onboarding_status SET DEFAULT 'not_started'::onboardingstatus")

    op.create_table(
        "chat_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("blogger_id", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_message_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.execute("ALTER TABLE chat_sessions ALTER COLUMN blogger_id TYPE bloggerid USING blogger_id::bloggerid")

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chat_sessions.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_chat_messages_session_created", "chat_messages", ["session_id", "created_at"])

    op.create_table(
        "onboarding_responses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("blogger_id", sa.Text(), nullable=False),
        sa.Column("step_id", sa.String(64), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer_value", sa.Text(), nullable=False),
        sa.Column("answer_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
    )
    op.execute("ALTER TABLE onboarding_responses ALTER COLUMN blogger_id TYPE bloggerid USING blogger_id::bloggerid")
    op.create_index("ix_onboarding_user_step", "onboarding_responses", ["user_id", "step_id"])


def downgrade() -> None:
    op.drop_table("onboarding_responses")
    op.drop_index("ix_chat_messages_session_created", "chat_messages")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_column("users", "amocrm_contact_id")
    op.drop_column("users", "long_term_profile")
    op.drop_column("users", "onboarding_step")
    op.drop_column("users", "onboarding_status")
    op.drop_column("users", "blogger_id")
    op.execute("DROP TYPE IF EXISTS onboardingstatus")
