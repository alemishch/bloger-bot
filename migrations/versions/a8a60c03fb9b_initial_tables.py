"""initial tables

Revision ID: a8a60c03fb9b
Revises: 
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a8a60c03fb9b'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Create ENUMs with lowercase values ──
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE sourcetype AS ENUM ('telegram', 'youtube', 'pdf');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE contenttype AS ENUM ('video', 'audio', 'text', 'post');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE bloggerid AS ENUM ('yuri', 'maria');
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)
    op.execute("""
        DO $$ BEGIN
            CREATE TYPE jobstatus AS ENUM (
                'discovered', 'downloading', 'downloaded', 'download_failed',
                'transcribing', 'transcribed', 'transcription_failed',
                'labeling', 'labeled', 'label_failed',
                'chunking', 'vectorized', 'ready', 'failed'
            );
        EXCEPTION WHEN duplicate_object THEN null;
        END $$;
    """)

    # ── content_sources ──
    op.create_table(
        'content_sources',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('source_type', sa.Text(), nullable=False),
        sa.Column('blogger_id', sa.Text(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('last_parsed_message_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
    )
    op.execute("ALTER TABLE content_sources ALTER COLUMN source_type TYPE sourcetype USING source_type::sourcetype")
    op.execute("ALTER TABLE content_sources ALTER COLUMN blogger_id TYPE bloggerid USING blogger_id::bloggerid")

    # ── content_items ──
    # ALL enum columns start as TEXT with NO server_default
    # We cast to enum THEN set the default — PostgreSQL requires this order
    op.create_table(
        'content_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('source_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content_sources.id'), nullable=False),
        sa.Column('source_message_id', sa.Integer(), nullable=True),
        sa.Column('source_url', sa.String(1024), nullable=True),
        sa.Column('title', sa.String(512), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('media_type', sa.String(64), nullable=True),
        sa.Column('duration_seconds', sa.Float(), nullable=True),
        sa.Column('file_size_bytes', sa.Integer(), nullable=True),
        sa.Column('file_path', sa.String(1024), nullable=True),
        sa.Column('transcript_path', sa.String(1024), nullable=True),
        sa.Column('transcript_text', sa.Text(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('tags', sa.JSON(), nullable=True),
        sa.Column('themes', sa.JSON(), nullable=True),
        sa.Column('problems_solved', sa.JSON(), nullable=True),
        sa.Column('tools_mentioned', sa.JSON(), nullable=True),
        sa.Column('target_audience', sa.String(64), nullable=True),
        sa.Column('content_category', sa.String(64), nullable=True),
        sa.Column('is_paid', sa.Boolean(), nullable=True, server_default='false'),
        sa.Column('label_metadata', sa.JSON(), nullable=True),
        sa.Column('chroma_collection', sa.String(255), nullable=True),
        sa.Column('chunk_count', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('raw_metadata', sa.JSON(), nullable=True),
        sa.Column('date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        # ↓ TEXT with NO default — cast to enum below, then set default
        sa.Column('content_type', sa.Text(), nullable=False),
        sa.Column('blogger_id', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
    )

    # Step 1: cast TEXT → enum (works because no server_default exists yet)
    op.execute("ALTER TABLE content_items ALTER COLUMN content_type TYPE contenttype USING content_type::contenttype")
    op.execute("ALTER TABLE content_items ALTER COLUMN blogger_id TYPE bloggerid USING blogger_id::bloggerid")
    op.execute("ALTER TABLE content_items ALTER COLUMN status TYPE jobstatus USING status::jobstatus")

    # Step 2: NOW set the default (after cast succeeds)
    op.execute("ALTER TABLE content_items ALTER COLUMN status SET DEFAULT 'discovered'::jobstatus")

    op.create_index('ix_content_items_status', 'content_items', ['status'])
    op.create_index(
        'ix_content_items_source_message',
        'content_items',
        ['source_id', 'source_message_id'],
        unique=True,
    )

    # ── content_chunks ──
    op.create_table(
        'content_chunks',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('content_item_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('content_items.id'), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('start_time', sa.Float(), nullable=True),
        sa.Column('end_time', sa.Float(), nullable=True),
        sa.Column('token_count', sa.Integer(), nullable=True),
        sa.Column('chroma_id', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
    )

    # ── users ──
    op.create_table(
        'users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('telegram_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('username', sa.String(255), nullable=True),
        sa.Column('first_name', sa.String(255), nullable=True),
        sa.Column('last_name', sa.String(255), nullable=True),
        sa.Column('phone', sa.String(32), nullable=True),
        sa.Column('email', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('profile_data', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
    )


def downgrade() -> None:
    op.drop_table('users')
    op.drop_table('content_chunks')
    op.drop_index('ix_content_items_source_message', 'content_items')
    op.drop_index('ix_content_items_status', 'content_items')
    op.drop_table('content_items')
    op.drop_table('content_sources')
    op.execute('DROP TYPE IF EXISTS jobstatus')
    op.execute('DROP TYPE IF EXISTS bloggerid')
    op.execute('DROP TYPE IF EXISTS contenttype')
    op.execute('DROP TYPE IF EXISTS sourcetype')