"""
find_chat_id.py — discover chat/group IDs using Pyrogram (QR login)

Usage:
    pip install pyrogram TgCrypto
    python tools/find_chat_id.py
"""

import asyncio
import os
from pyrogram import Client
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
SESSION_NAME = "finder_session"


async def main():
    if not API_ID or not API_HASH:
        print("❌ Set TELEGRAM_API_ID and TELEGRAM_API_HASH env vars first")
        return

    print(f"🔑 Using API_ID={API_ID}")
    print("📱 QR login will be used\n")

    async with Client(
        SESSION_NAME,
        api_id=API_ID,
        api_hash=API_HASH
    ) as app:

        # If first login, Pyrogram will automatically trigger QR login
        me = await app.get_me()

        print(f"\n✅ Logged in as: {me.first_name} (@{me.username})\n")

        print(f"{'Type':<12} {'ID':<20} {'Title'}")
        print("─" * 60)

        async for dialog in app.get_dialogs():
            chat = dialog.chat
            chat_type = chat.type.name.lower()
            title = (
                getattr(chat, "title", None)
                or f"{getattr(chat, 'first_name', '')} {getattr(chat, 'last_name', '')}".strip()
            )
            print(f"{chat_type:<12} {chat.id:<20} {title}")

    print(f"\n💾 Session saved as '{SESSION_NAME}.session'")
    print("   Backup this file.")


if __name__ == "__main__":
    asyncio.run(main())