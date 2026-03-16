"""Telegram message handlers."""
import structlog
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.enums import ChatAction
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.config import settings, load_blogger_config
from bot.llm_client import ask_llm
from bot.db import upsert_user

logger = structlog.get_logger()
router = Router()


def _cfg():
    return load_blogger_config()


# ── /start ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    cfg = _cfg()

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

    name = message.from_user.first_name or "друг"
    welcome = cfg["welcome_message"].replace("{name}", name)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❓ Задать вопрос", callback_data="ask_hint")],
        [InlineKeyboardButton(text="📋 О боте", callback_data="about")],
    ])

    await message.answer(welcome, reply_markup=kb)


# ── /help ───────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    cfg = _cfg()
    name = cfg.get("display_name", "эксперт")
    await message.answer(
        f"🤖 <b>Что я умею:</b>\n\n"
        f"💬 <b>Задать вопрос</b> — просто напиши текстом, я найду ответ из базы знаний {name}.\n\n"
        f"📋 /about — информация о боте\n"
        f"❓ /help — эта справка\n\n"
        f"⚠️ {cfg.get('legal_disclaimer', '')}",
        parse_mode="HTML",
    )


# ── /about ──────────────────────────────────────────────────────────────────

@router.message(Command("about"))
async def cmd_about(message: Message):
    cfg = _cfg()
    await message.answer(
        f"ℹ️ <b>Цифровой помощник {cfg.get('display_name', '')}</b>\n\n"
        f"Я использую базу знаний эксперта, чтобы помочь тебе разобраться "
        f"в вопросах здоровья, психосоматики и работы с телом.\n\n"
        f"Я не заменяю врача и не ставлю диагнозы.\n\n"
        f"Версия: 0.1.0 (Stage 1)",
        parse_mode="HTML",
    )


# ── Callback: about button ──────────────────────────────────────────────────

@router.callback_query(F.data == "about")
async def cb_about(callback: CallbackQuery):
    cfg = _cfg()
    await callback.message.answer(
        f"ℹ️ <b>Цифровой помощник {cfg.get('display_name', '')}</b>\n\n"
        f"Я использую базу знаний эксперта, чтобы помочь тебе разобраться "
        f"в вопросах здоровья, психосоматики и работы с телом.\n\n"
        f"Я не заменяю врача и не ставлю диагнозы.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "ask_hint")
async def cb_ask_hint(callback: CallbackQuery):
    await callback.message.answer(
        "Просто напиши свой вопрос текстом — я поищу ответ в базе знаний 🔍"
    )
    await callback.answer()


# ── Text messages → RAG Q&A ─────────────────────────────────────────────────

@router.message(F.text)
async def handle_text(message: Message):
    query = message.text.strip()
    if not query or query.startswith("/"):
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    thinking_msg = await message.answer("🔍 Изучаю вашу ситуацию…")

    try:
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

        result = await ask_llm(query=query, blogger_id=settings.BLOGGER_ID)
        answer = result.get("answer", "Не удалось получить ответ.")

        if len(answer) > 4000:
            answer = answer[:4000] + "…"

        await thinking_msg.edit_text(answer)

        logger.info("question_answered",
                     telegram_id=message.from_user.id,
                     query_len=len(query), answer_len=len(answer))
    except Exception as e:
        logger.error("llm_error", error=str(e), telegram_id=message.from_user.id)
        await thinking_msg.edit_text(
            "😔 Произошла ошибка при обработке вопроса. Попробуйте ещё раз через минуту."
        )
