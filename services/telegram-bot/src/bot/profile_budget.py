"""Long-term profile size budgeting (TASK.md §14.4: ≤4000 chars) without corrupting JSON."""

from __future__ import annotations

import copy
import json
from typing import Any

MAX_PROFILE_JSON_CHARS = 4000


def merge_profile_delta(base: dict | None, delta: dict | None) -> dict[str, Any]:
    """Shallow merge: delta keys overwrite base (phase_log etc. replace as a whole)."""
    out = dict(base or {})
    for k, v in (delta or {}).items():
        out[k] = v
    return out


def _json_len(profile: dict) -> int:
    return len(json.dumps(profile, ensure_ascii=False))


def _truncate_str(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def fit_profile_to_budget(profile: dict | None, max_chars: int = MAX_PROFILE_JSON_CHARS) -> dict[str, Any]:
    """
    Trim lowest-priority fields until JSON serializes under max_chars.
    Never slices the whole blob mid-string.
    """
    p: dict[str, Any] = copy.deepcopy(profile) if profile else {}

    def shrink_once() -> bool:
        """Return True if something was reduced."""
        pl = p.get("phase_log")
        if isinstance(pl, list) and len(pl) > 3:
            p["phase_log"] = pl[-3:]
            return True
        if isinstance(pl, list) and len(pl) > 1:
            p["phase_log"] = pl[-1:]
            return True

        ck = p.get("covered_keywords")
        if isinstance(ck, list) and len(ck) > 12:
            p["covered_keywords"] = ck[-12:]
            return True
        if isinstance(ck, list) and len(ck) > 1:
            p["covered_keywords"] = ck[: len(ck) // 2]
            return True

        al = p.get("assistant_lexical_used")
        if isinstance(al, list) and len(al) > 10:
            p["assistant_lexical_used"] = al[-10:]
            return True
        if isinstance(al, list) and len(al) > 1:
            p["assistant_lexical_used"] = al[: len(al) // 2]
            return True

        ch = p.get("covered_hypotheses")
        if isinstance(ch, list) and len(ch) > 3:
            p["covered_hypotheses"] = ch[-3:]
            return True
        if isinstance(ch, list) and ch:
            p["covered_hypotheses"] = [
                _truncate_str(str(x), 120) if not isinstance(x, dict) else x for x in ch[:2]
            ]
            return True

        for key, maxlen in (
            ("pattern_summary", 500),
            ("last_session_summary", 450),
            ("previous_session_summary", 450),
            ("goals", 350),
            ("topics_of_interest", 400),
            ("reactions", 350),
            ("communication_style", 200),
        ):
            val = p.get(key)
            if isinstance(val, str) and len(val) > maxlen:
                p[key] = _truncate_str(val, maxlen)
                return True

        ph = p.get("previous_hypotheses")
        if isinstance(ph, list) and len(ph) > 2:
            p["previous_hypotheses"] = ph[-2:]
            return True

        if isinstance(pl, list) and pl:
            p.pop("phase_log", None)
            return True

        return False

    guard = 0
    while _json_len(p) > max_chars and guard < 80:
        guard += 1
        if not shrink_once():
            break

    if _json_len(p) > max_chars:
        # Last resort: drop bulky optional fields
        for k in (
            "phase_log",
            "covered_keywords",
            "covered_hypotheses",
            "assistant_lexical_used",
            "previous_hypotheses",
            "reactions",
            "topics_of_interest",
        ):
            p.pop(k, None)
            if _json_len(p) <= max_chars:
                break

    if _json_len(p) > max_chars:
        p = {
            "dialogue_phase": p.get("dialogue_phase"),
            "phase_started_at": p.get("phase_started_at"),
            "name": p.get("name"),
            "goals": _truncate_str(str(p.get("goals") or ""), 200) if p.get("goals") else None,
            "last_session_summary": _truncate_str(str(p.get("last_session_summary") or ""), 400),
        }
        p = {k: v for k, v in p.items() if v is not None}

    return p
