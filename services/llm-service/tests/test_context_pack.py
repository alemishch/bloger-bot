from llm_service.context_pack import pack_context


def test_pack_context_empty():
    p = pack_context(None, None)
    assert "working_memory_text" in p
    assert "profile_text" in p
    assert "long_memory_text" in p
    assert p["dialogue_phase"] == "free_chat"


def test_pack_context_profile_phase_override():
    p = pack_context(
        [{"role": "user", "content": "hi"}],
        {"dialogue_phase": "deepening", "goals": "sleep better"},
        dialogue_phase="free_chat",
    )
    assert p["dialogue_phase"] == "deepening"
    assert "sleep" in p["profile_text"]
