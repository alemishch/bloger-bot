"""Tests for profile merge and JSON size budgeting."""

from __future__ import annotations

import json
import sys
from pathlib import Path

# src layout: services/telegram-bot/src/bot
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bot.profile_budget import fit_profile_to_budget, merge_profile_delta


def test_merge_profile_delta_shallow_overwrite():
    base = {"a": 1, "dialogue_phase": "alpha", "phase_log": [{"x": 1}]}
    delta = {"a": 2, "phase_log": [{"y": 2}]}
    m = merge_profile_delta(base, delta)
    assert m["a"] == 2
    assert m["phase_log"] == [{"y": 2}]


def test_fit_profile_to_budget_trims_lists():
    big = {
        "dialogue_phase": "contact_mapping",
        "phase_started_at": "2026-01-01",
        "phase_log": [{"i": str(j)} for j in range(30)],
        "covered_keywords": [f"k{i}" for i in range(40)],
    }
    out = fit_profile_to_budget(big, max_chars=800)
    assert len(json.dumps(out, ensure_ascii=False)) <= 800
    assert out.get("dialogue_phase") == "contact_mapping"
