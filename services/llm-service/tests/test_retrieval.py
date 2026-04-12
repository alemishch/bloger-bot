from llm_service.retrieval import format_ref
from llm_service import retrieval as r


def test_format_ref():
    assert format_ref("yuri", {"item_id": "abc", "chunk_index": 2}) == "ref:yuri:abc:2"
    assert format_ref("yuri", {}) is None


def test_dedupe_key_stable():
    k1 = r._dedupe_key({"item_id": 1, "chunk_index": 0}, "text", 0)
    k2 = r._dedupe_key({"item_id": 1, "chunk_index": 0}, "text", 99)
    assert k1 == k2
