"""Telegram message handlers — onboarding + RAG Q&A."""
import structlog
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.enums import ChatAction

from bot.config import settings, load_blogger_config
from bot.llm_client import ask_llm
from bot.db import upsert_user, save_onboarding_response, update_onboarding_state, get_user_state
from bot.onboarding import (
    load_scenario, get_step, get_first_step_id,
    build_step_message, get_lead_magnet_text,
)

logger = structlog.get_logger()
router = Router()

# In-memory multi-select accumulator: {telegram_id: {step_id: [values]}}
_multi_select: dict[int, dict[str, list[str]]] = {}


def _cfg():
    return load_blogger_config()


async def _send_step(message_or_cb, step_id: str, user_name: str = "друг"):
    """Send an onboarding step to the user."""
    step = get_step(step_id)
    if not step:
        logger.error("onboarding_step_not_found", step_id=step_id)
        return

    text, kb, parse_mode = build_step_message(step, user_name)

    target = message_or_cb if isinstance(message_or_cb, Message) else message_or_cb.message
    kwargs = {"reply_markup": kb} if kb else {}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    await target.answer(text, **kwargs)

    if step.get("type") == "message" and not step.get("buttons") and not step.get("finish"):
        next_id = step.get("next")
        if next_id:
            await _send_step(target, next_id, user_name)


# ── /start ──────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_info = await upsert_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        blogger_id=settings.BLOGGER_ID,
    )

    name = message.from_user.first_name or "друг"

    if user_info["is_new"] or user_info.get("onboarding_status") == "not_started":
        logger.info("onboarding_start", telegram_id=message.from_user.id)
        first_step = get_first_step_id()
        await update_onboarding_state(message.from_user.id, "in_progress", first_step)
        await _send_step(message, first_step, name)
    else:
        cfg = _cfg()
        await message.answer(
            f"С возвращением, {name}! 👋\n\n"
            f"Задай мне любой вопрос — я подберу ответ из базы знаний {cfg.get('display_name', 'эксперта')}."
        )


# ── Onboarding callbacks ────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ob:"))
async def onboarding_callback(callback: CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) < 3:
        await callback.answer()
        return

    _, step_id, value = parts
    telegram_id = callback.from_user.id
    name = callback.from_user.first_name or "друг"
    step = get_step(step_id)
    if not step:
        await callback.answer()
        return

    is_multiple = step.get("multiple", False)

    if is_multiple and value != "__done__":
        if telegram_id not in _multi_select:
            _multi_select[telegram_id] = {}
        if step_id not in _multi_select[telegram_id]:
            _multi_select[telegram_id][step_id] = []

        selections = _multi_select[telegram_id][step_id]
        if value in selections:
            selections.remove(value)
            await callback.answer(f"Убрано ❌")
        else:
            max_choices = step.get("max_choices", 10)
            if len(selections) >= max_choices:
                await callback.answer(f"Максимум {max_choices} варианта")
                return
            selections.append(value)
            opt_text = next((o["text"] for o in step.get("options", []) if o["value"] == value), value)
            await callback.answer(f"Выбрано ✅ ({len(selections)})")
        return

    if is_multiple and value == "__done__":
        selections = _multi_select.get(telegram_id, {}).get(step_id, [])
        if not selections:
            await callback.answer("Выбери хотя бы один вариант")
            return
        final_value = ",".join(selections)
        _multi_select.pop(telegram_id, None)
    else:
        final_value = value

    question_text = step.get("question", step.get("text", ""))
    await save_onboarding_response(
        telegram_id=telegram_id,
        blogger_id=settings.BLOGGER_ID,
        step_id=step_id,
        question_text=question_text[:500],
        answer_value=final_value,
    )

    next_step_id = step.get("next")
    if step.get("type") == "message" and "buttons" in step:
        btn = next((b for b in step["buttons"] if b["value"] == value), None)
        if btn and "next" in btn:
            next_step_id = btn["next"]

    if next_step_id:
        next_step = get_step(next_step_id)
        if next_step and next_step.get("finish"):
            await update_onboarding_state(telegram_id, "completed", next_step_id)
            text, _, parse_mode = build_step_message(next_step, name)
            await callback.message.answer(text)

            if step_id == "symptoms" or "symptoms" in str(_multi_select.get(telegram_id, {})):
                pass
            lead = get_lead_magnet_text(final_value.split(","))
            if lead.strip():
                await callback.message.answer(lead)
        else:
            await update_onboarding_state(telegram_id, "in_progress", next_step_id)
            await _send_step(callback, next_step_id, name)
    else:
        await update_onboarding_state(telegram_id, "completed", step_id)

    await callback.answer()


# ── /help ───────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    cfg = _cfg()
    name = cfg.get("display_name", "эксперт")
    await message.answer(
        f"🤖 <b>Что я умею:</b>\n\n"
        f"💬 <b>Задать вопрос</b> — напиши текстом, я найду ответ из базы знаний {name}.\n\n"
        f"📋 /about — информация о боте\n"
        f"🔄 /restart — пройти онбординг заново\n"
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
        f"Я использую базу знаний эксперта для ответов на вопросы "
        f"о здоровье, психосоматике и работе с телом.\n\n"
        f"Я не заменяю врача и не ставлю диагнозы.\n\n"
        f"Версия: 0.2.0 (Stage 1)",
        parse_mode="HTML",
    )


# ── /restart — redo onboarding ──────────────────────────────────────────────

@router.message(Command("restart"))
async def cmd_restart(message: Message):
    name = message.from_user.first_name or "друг"
    first_step = get_first_step_id()
    await update_onboarding_state(message.from_user.id, "in_progress", first_step)
    await _send_step(message, first_step, name)


# ── Text messages → RAG Q&A ─────────────────────────────────────────────────

@router.message(F.text)
async def handle_text(message: Message):
    query = message.text.strip()
    if not query or query.startswith("/"):
        return

    user_state = await get_user_state(message.from_user.id)
    if user_state and user_state.get("onboarding_status") == "in_progress":
        await message.answer(
            "Сначала давай закончим знакомство! Нажми на одну из кнопок выше ☝️"
        )
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
