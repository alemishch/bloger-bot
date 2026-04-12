import pytest

pytest.importorskip("fastapi")

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from llm_service.main import app


@patch("llm_service.main.load_blogger_config")
@patch("llm_service.main.rag_answer", new_callable=AsyncMock)
def test_ask_uses_legacy_when_pipeline_disabled(mock_rag, mock_cfg):
    mock_cfg.return_value = {"conversation_pipeline_v2": False}
    mock_rag.return_value = {
        "answer": "legacy",
        "sources": [{"chunk": "a", "similarity": 0.9}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }
    client = TestClient(app)
    r = client.post(
        "/api/v1/ask",
        json={"query": "hello", "blogger_id": "yuri"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "legacy"
    assert body["quality_warning"] is False
    mock_rag.assert_awaited_once()


@patch("llm_service.main.load_blogger_config")
@patch("llm_service.pipeline.conversation_pipeline_answer", new_callable=AsyncMock)
def test_ask_uses_pipeline_when_enabled(mock_pipe, mock_cfg):
    mock_cfg.return_value = {"conversation_pipeline_v2": True}
    mock_pipe.return_value = {
        "answer": "pipe",
        "sources": [],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        "quality_warning": True,
    }
    client = TestClient(app)
    r = client.post(
        "/api/v1/ask",
        json={"query": "hello", "blogger_id": "yuri"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "pipe"
    assert body["quality_warning"] is True
    mock_pipe.assert_awaited_once()
