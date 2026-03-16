"""Telegram message handlers."""
import structlog
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart

from bot.config import settings, load_blogger_config
from bot.llm_client import ask_llm
from bot.db import upsert_user

logger = structlog.get_logger()
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    cfg = load_blogger_config()

    user_info = await upsert_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
    )

    if user_info["is_new"]:
        logger.info("new_user", telegram_id=message.from_user.id)
    else:
        logger.info("returning_user", telegram_id=message.from_user.id)

    await message.answer(cfg["welcome_message"])


@router.message(F.text)
async def handle_text(message: Message):
    query = message.text.strip()
    if not query:
        return

    thinking_msg = await message.answer("🔍 Ищу ответ в базе знаний…")

    try:
        result = await ask_llm(query=query, blogger_id=settings.BLOGGER_ID)
        answer = result.get("answer", "Не удалось получить ответ.")

        await thinking_msg.edit_text(answer)

        logger.info("question_answered",
                     telegram_id=message.from_user.id,
                     query_len=len(query), answer_len=len(answer))
    except Exception as e:
        logger.error("llm_error", error=str(e), telegram_id=message.from_user.id)
        await thinking_msg.edit_text(
            "Произошла ошибка при обработке вопроса. Попробуйте позже."
        )
