from llm_service.context_pack import build_anti_repetition_block, count_user_turns


def test_count_user_turns():
    h = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert count_user_turns(h) == 2


def test_build_anti_repetition_empty():
    assert build_anti_repetition_block([]) == ""
    assert build_anti_repetition_block([{"role": "user", "content": "x"}]) == ""


def test_build_anti_repetition_flags_markers():
    h = [
        {"role": "assistant", "content": "Скажем так — раз.\nДальше текст."},
        {"role": "user", "content": "ок"},
        {"role": "assistant", "content": "Скажем так — два.\nЕщё."},
    ]
    block = build_anti_repetition_block(h, max_assistant_messages=4)
    assert "Скажем так" in block or "скажем" in block.lower()
    assert "ИЗБЕГАЙ" in block
