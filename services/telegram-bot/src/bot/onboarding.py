"""Onboarding state machine driven by YAML config.

The scenario YAML defines steps. Each step has a type:
  - message: send text, auto-advance to next
  - choice: send question with inline buttons, wait for callback

The engine stores current step in DB (users.onboarding_step) and
saves each answer to onboarding_responses.
"""
import os
import yaml
import structlog
from pathlib import Path
from typing import Optional

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.config import settings

logger = structlog.get_logger()

_scenario_cache: dict[str, dict] = {}


def load_scenario(blogger_id: str | None = None) -> dict:
    bid = blogger_id or settings.BLOGGER_ID
    if bid in _scenario_cache:
        return _scenario_cache[bid]

    config_dir = Path(settings.CONFIG_DIR).parent / "onboarding"
    path = config_dir / f"{bid}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Onboarding scenario not found: {path}")

    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    _scenario_cache[bid] = data
    return data


def get_step(step_id: str, blogger_id: str | None = None) -> Optional[dict]:
    scenario = load_scenario(blogger_id)
    for step in scenario.get("steps", []):
        if step["id"] == step_id:
            return step
    return None


def get_first_step_id(blogger_id: str | None = None) -> str:
    scenario = load_scenario(blogger_id)
    steps = scenario.get("steps", [])
    return steps[0]["id"] if steps else "welcome"


def build_step_message(step: dict, user_name: str = "друг") -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Return (text, keyboard) for a given step."""
    step_type = step.get("type", "message")
    text = step.get("text", step.get("question", ""))
    text = text.replace("{name}", user_name)
    parse_mode = step.get("parse_mode")

    kb = None
    if step_type == "message" and "buttons" in step:
        buttons = []
        for btn in step["buttons"]:
            buttons.append([InlineKeyboardButton(
                text=btn["text"],
                callback_data=f"ob:{step['id']}:{btn['value']}",
            )])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    elif step_type == "choice":
        options = step.get("options", [])
        is_multiple = step.get("multiple", False)
        buttons = []
        for opt in options:
            buttons.append([InlineKeyboardButton(
                text=opt["text"],
                callback_data=f"ob:{step['id']}:{opt['value']}",
            )])
        if is_multiple:
            buttons.append([InlineKeyboardButton(
                text="✅ Готово",
                callback_data=f"ob:{step['id']}:__done__",
            )])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    return text, kb, parse_mode


def get_lead_magnet_text(symptoms: list[str], blogger_id: str | None = None) -> str:
    scenario = load_scenario(blogger_id)
    lead_magnets = scenario.get("lead_magnet", {})
    for symptom in symptoms:
        if symptom in lead_magnets:
            return f"📎 {lead_magnets[symptom]}"
    return f"📎 {lead_magnets.get('default', '')}"
