"""
Singleton Pyrogram client for the worker process.
Duplicates the SQLite session per-PID to avoid 'database is locked' errors 
when multiple workers run concurrently.
"""
import asyncio
import os
import shutil
import structlog
from typing import Optional
from pyrogram import Client
from ingestion_service.config import settings

logger = structlog.get_logger()

_client: Optional[Client] = None
_lock = asyncio.Lock()


async def get_client() -> Client:
    """Get or create the shared Pyrogram client for this worker process."""
    global _client

    async with _lock:
        if _client is not None and _client.is_connected:
            return _client

        # Clean up stale client
        if _client is not None:
            try:
                await _client.stop()
            except Exception:
                pass
            _client = None

        sessions_dir = settings.SESSIONS_DIR
        session_name = settings.TELEGRAM_SESSION_NAME
        expected_path = os.path.join(sessions_dir, f"{session_name}.session")

        if not os.path.exists(expected_path):
            raise RuntimeError(
                f"Session file not found: {expected_path}\n"
                f"Run: python tools/create_session.py"
            )

        # ── FIX: Generate a process-specific session file to avoid SQLite locks ──
        pid_session_name = f"{session_name}_worker_{os.getpid()}"
        pid_session_path = os.path.join(sessions_dir, f"{pid_session_name}.session")
        
        # Clone the main authorized session so this process has exclusive DB read access
        shutil.copy2(expected_path, pid_session_path)

        logger.info("telegram_client_starting", session=pid_session_name, workdir=sessions_dir)

        _client = Client(
            name=pid_session_name,
            api_id=int(settings.TELEGRAM_API_ID),
            api_hash=settings.TELEGRAM_API_HASH,
            workdir=sessions_dir,
            no_updates=True,
            in_memory=False,
        )
        await _client.start()
        me = await _client.get_me()
        logger.info("telegram_client_ready", user=me.username)
        return _client


async def stop_client():
    """Gracefully stop the shared client."""
    global _client
    if _client is not None:
        try:
            await _client.stop()
        except Exception:
            pass
        _client = None
        logger.info("telegram_client_stopped")