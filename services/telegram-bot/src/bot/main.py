"""Entry point for the Telegram bot (aiogram 3.x, polling mode)."""
import asyncio
import structlog
from aiogram import Bot, Dispatcher

from bot.config import settings, load_blogger_config
from bot.handlers import router

logger = structlog.get_logger()


async def main():
    cfg = load_blogger_config()
    token = cfg.get("telegram_bot_token", "")
    if not token or token.startswith("$"):
        raise RuntimeError(
            f"Bot token not set for blogger '{settings.BLOGGER_ID}'. "
            f"Set TELEGRAM_BOT_TOKEN_{settings.BLOGGER_ID.upper()} env var."
        )

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    me = await bot.get_me()
    logger.info("bot_started", username=me.username, blogger=settings.BLOGGER_ID)

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
