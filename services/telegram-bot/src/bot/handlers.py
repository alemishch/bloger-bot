"""Telegram message handlers — onboarding + analysis + RAG Q&A + commands."""
import asyncio
import structlog
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command
from aiogram.enums import ChatAction

from bot.config import settings, load_blogger_config
from bot.llm_client import ask_llm, analyze_profile, update_profile_via_llm
from bot.db import (
    upsert_user, save_onboarding_response, update_onboarding_state,
    get_user_state, get_or_create_session, save_chat_message,
    get_onboarding_responses, clear_onboarding_responses,
    get_session_history, get_long_term_profile,
    update_long_term_profile, get_closed_session_for_update,
    mark_session_summarized,
)
from bot.onboarding import (
    get_step, get_first_step_id,
    build_step_message, get_lead_magnet_text,
)

logger = structlog.get_logger()
router = Router()

_multi_select: dict[int, dict[str, list[str]]] = {}


async def _try_update_profile(telegram_id: int):
    """Background: check if a session was recently closed → update long-term profile."""
    try:
        closed = await get_closed_session_for_update(telegram_id)
        if not closed or not closed["messages"]:
            return

        current_profile = await get_long_term_profile(telegram_id)
        user_state = await get_user_state(telegram_id)
        name = None
        if user_state and current_profile:
            name = current_profile.get("name")

        result = await update_profile_via_llm(
            messages=closed["messages"],
            current_profile=current_profile,
            blogger_id=settings.BLOGGER_ID,
            user_name=name,
        )

        if result.get("profile"):
            await update_long_term_profile(telegram_id, result["profile"])
        if result.get("summary"):
            await mark_session_summarized(closed["session_id"], result["summary"])

        logger.info("profile_updated", telegram_id=telegram_id)
    except Exception as e:
        logger.warning("profile_update_failed", telegram_id=telegram_id, error=str(e))


def _cfg():
    return load_blogger_config()


async def _send_step(message_or_cb, step_id: str, user_name: str = "друг"):
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


async def _run_analysis(target_message: Message, telegram_id: int, name: str):
    """Generate problem zones + hypotheses from onboarding answers."""
    responses = await get_onboarding_responses(telegram_id)
    if not responses:
        return

    thinking = await target_message.answer("🧠 Анализирую твои ответы…")

    try:
        await target_message.bot.send_chat_action(target_message.chat.id, ChatAction.TYPING)
        result = await analyze_profile(
            onboarding_responses=[
                {"step_id": r["step_id"], "answer_value": r["answer_value"]}
                for r in responses
            ],
            blogger_id=settings.BLOGGER_ID,
            user_name=name,
        )

        analysis = result.get("analysis", "")
        if len(analysis) > 4000:
            parts = [analysis[i:i+4000] for i in range(0, len(analysis), 4000)]
            await thinking.edit_text(parts[0])
            for part in parts[1:]:
                await target_message.answer(part)
        else:
            await thinking.edit_text(analysis)

        await target_message.answer(
            "💬 Теперь ты можешь задать мне любой вопрос — я подберу ответ из базы знаний Юрия.\n\n"
            "Команды:\n"
            "/profile — твой профиль и ответы\n"
            "/reset — пройти онбординг заново\n"
            "/help — все команды"
        )

        logger.info("analysis_complete", telegram_id=telegram_id)
    except Exception as e:
        logger.error("analysis_error", error=str(e), telegram_id=telegram_id)
        await thinking.edit_text(
            "📋 Спасибо за ответы! Анализ временно недоступен.\n\n"
            "Ты можешь задать любой вопрос — я подберу ответ из базы знаний Юрия. 💬"
        )


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
    status = str(user_info.get("onboarding_status", "not_started"))

    if user_info["is_new"] or status == "not_started":
        logger.info("onboarding_start", telegram_id=message.from_user.id)
        first_step = get_first_step_id()
        await update_onboarding_state(message.from_user.id, "in_progress", first_step)
        await _send_step(message, first_step, name)
    else:
        cfg = _cfg()
        await message.answer(
            f"С возвращением, {name}! 👋\n\n"
            f"Задай мне любой вопрос — я подберу ответ из базы знаний {cfg.get('display_name', 'эксперта')}.\n\n"
            f"/help — список команд"
        )


# ── /reset — clear and redo onboarding ─────────────────────────────────────

@router.message(Command("reset"))
async def cmd_reset(message: Message):
    name = message.from_user.first_name or "друг"
    await clear_onboarding_responses(message.from_user.id)
    _multi_select.pop(message.from_user.id, None)
    first_step = get_first_step_id()
    await update_onboarding_state(message.from_user.id, "in_progress", first_step)
    await message.answer("🔄 Начинаем заново!")
    await _send_step(message, first_step, name)


# ── /restart — alias for /reset ────────────────────────────────────────────

@router.message(Command("restart"))
async def cmd_restart(message: Message):
    await cmd_reset(message)


# ── /profile — show user's onboarding answers ──────────────────────────────

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    responses = await get_onboarding_responses(message.from_user.id)
    user_state = await get_user_state(message.from_user.id)

    if not responses:
        await message.answer("У тебя пока нет данных. Пройди онбординг: /reset")
        return

    name = message.from_user.first_name or "друг"
    status = str(user_state.get("onboarding_status", "?")) if user_state else "?"
    lines = [f"👤 <b>Профиль: {name}</b>\n", f"Статус: {status}\n"]

    label_map = {
        "symptoms": "Беспокоит", "duration": "Длительность", "tried": "Пробовал(а)",
        "lifestyle": "Ритм жизни", "blocker": "Мешает", "expert_experience": "Знакомство с Юрием",
    }

    for r in responses:
        step = r.get("step_id", "")
        if step in ("legal",):
            continue
        label = label_map.get(step, step)
        val = r.get("answer_value", "")
        lines.append(f"📌 <b>{label}:</b> {val}")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ── /analyze — re-run analysis on current answers ──────────────────────────

@router.message(Command("analyze"))
async def cmd_analyze(message: Message):
    name = message.from_user.first_name or "друг"
    await _run_analysis(message, message.from_user.id, name)


# ── /help ───────────────────────────────────────────────────────────────────

@router.message(Command("help"))
async def cmd_help(message: Message):
    cfg = _cfg()
    name = cfg.get("display_name", "эксперт")
    await message.answer(
        f"🤖 <b>Цифровой помощник {name}</b>\n\n"
        f"💬 <b>Задай вопрос</b> — напиши текстом\n\n"
        f"<b>Команды:</b>\n"
        f"▪️ /start — начало\n"
        f"▪️ /reset — пройти онбординг заново\n"
        f"▪️ /profile — твои ответы из анкеты\n"
        f"▪️ /analyze — повторный анализ проблемных зон\n"
        f"▪️ /about — о боте\n"
        f"▪️ /help — эта справка\n\n"
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
        f"Экспертные роли: врач, остеопат, клинический психолог, "
        f"гипнотерапевт, специалист по биологическому декодированию.\n\n"
        f"Я не заменяю врача и не ставлю диагнозы.\n\n"
        f"Версия: 0.3.0",
        parse_mode="HTML",
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
            await callback.answer("Убрано ❌")
        else:
            max_choices = step.get("max_choices", 10)
            if len(selections) >= max_choices:
                await callback.answer(f"Максимум {max_choices} варианта")
                return
            selections.append(value)
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
            await callback.answer()

            lead = get_lead_magnet_text(final_value.split(","))
            if lead.strip():
                await callback.message.answer(lead)

            await _run_analysis(callback.message, telegram_id, name)
        else:
            await update_onboarding_state(telegram_id, "in_progress", next_step_id)
            await _send_step(callback, next_step_id, name)
            await callback.answer()
    else:
        await update_onboarding_state(telegram_id, "completed", step_id)
        await callback.answer()


# ── Text messages → RAG Q&A ─────────────────────────────────────────────────

@router.message(F.text)
async def handle_text(message: Message):
    query = message.text.strip()
    if not query or query.startswith("/"):
        return

    user_state = await get_user_state(message.from_user.id)
    if user_state and str(user_state.get("onboarding_status")) == "in_progress":
        await message.answer(
            "Сначала давай закончим знакомство! Нажми на одну из кнопок выше ☝️\n"
            "Или /reset чтобы начать заново."
        )
        return

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    thinking_msg = await message.answer("🔍 Изучаю вашу ситуацию…")

    try:
        session_id = await get_or_create_session(message.from_user.id, settings.BLOGGER_ID)
        if session_id and user_state:
            await save_chat_message(user_state["id"], session_id, "user", query)

        chat_history = await get_session_history(message.from_user.id, max_messages=16)
        user_profile = await get_long_term_profile(message.from_user.id)

        await asyncio.sleep(0.3)
        await thinking_msg.edit_text("📚 Подбираю релевантный опыт…")
        await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

        result = await ask_llm(
            query=query,
            blogger_id=settings.BLOGGER_ID,
            chat_history=chat_history,
            user_profile=user_profile,
        )
        answer = result.get("answer", "Не удалось получить ответ.")
        if len(answer) > 4000:
            answer = answer[:4000] + "…"
        await thinking_msg.edit_text(answer)

        if session_id and user_state:
            token_count = result.get("usage", {}).get("completion_tokens")
            await save_chat_message(user_state["id"], session_id, "assistant", answer, token_count)

        asyncio.create_task(_try_update_profile(message.from_user.id))

        logger.info("question_answered",
                     telegram_id=message.from_user.id,
                     query_len=len(query), answer_len=len(answer))
    except Exception as e:
        logger.error("llm_error", error=str(e), telegram_id=message.from_user.id)
        await thinking_msg.edit_text(
            "😔 Произошла ошибка при обработке вопроса. Попробуй ещё раз через минуту."
        )
