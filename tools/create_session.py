"""
Creates a Pyrogram session using phone number login.
Run ONCE on your local machine, then the session file works headlessly.

Usage:
    python tools/create_session.py
"""
import asyncio
import os
from pyrogram import Client
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
PHONE = os.getenv("PHONE_NUM", "")  # e.g. +79813529684
SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "content_parser")
SESSIONS_DIR = "sessions"


async def main():
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    session_path = os.path.join(SESSIONS_DIR, SESSION_NAME)

    print(f"📁 Session will be saved to: {session_path}.session")
    print(f"📱 Phone: {PHONE}\n")

    app = Client(
        session_path,
        api_id=API_ID,
        api_hash=API_HASH,
        phone_number=PHONE,  # skip interactive prompt
    )

    await app.start()
    me = await app.get_me()
    print(f"\n✅ Logged in as: {me.first_name} (@{me.username})")
    print(f"   user_id: {me.id}")

    # Verify session is persisted
    import sqlite3
    conn = sqlite3.connect(f"{session_path}.session")
    rows = conn.execute("SELECT dc_id, user_id FROM sessions").fetchall()
    print(f"   Session DB: {rows}")
    conn.close()

    print("\nListing chats to find IDs:\n")
    print(f"{'Type':<15} {'ID':<22} {'Title'}")
    print("─" * 65)
    async for dialog in app.get_dialogs():
        chat = dialog.chat
        title = (
            getattr(chat, "title", None)
            or f"{getattr(chat, 'first_name', '')} {getattr(chat, 'last_name', '')}".strip()
        )
        print(f"{chat.type.name.lower():<15} {str(chat.id):<22} {title}")

    await app.stop()
    print(f"\n✅ Session saved: {session_path}.session")
    print("   Copy to sessions/ directory, then restart ingestion-worker.")


if __name__ == "__main__":
    asyncio.run(main())