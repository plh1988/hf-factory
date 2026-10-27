"""OpenAI-compatible LLM adapter (LM Studio, Ollama), tested against a fake server: no network needed."""

import json

import httpx
import numpy as np
import pytest

from factory.adapters import build_stores
from factory.adapters.llm import LLMError, OpenAICompatibleLLM
from factory.config import AdapterConfig, load_profile


def fake_server(calls: list, fail_first: int = 0, models=("chat-model", "embed-model")):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) <= fail_first:
            return httpx.Response(503, text="model loading")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        body = json.loads(request.content)
        if request.url.path.endswith("/chat/completions"):
            text = "<think>planning the answer</think>\n\nPlan summary: " + body["messages"][-1]["content"][:20]
            return httpx.Response(200, json={"choices": [{"message": {"content": text}, "finish_reason": "stop"}]})
        if request.url.path.endswith("/embeddings"):  # reversed order: the adapter must sort by index
            data = [{"index": i, "embedding": [float(i), 1.0, 0.0]} for i in range(len(body["input"]))]
            return httpx.Response(200, json={"data": list(reversed(data))})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def llm(calls, embedding_model="embed-model", **kw):
    cfg = AdapterConfig(kind="openai_compatible", base_url="http://localhost:1234/v1", model="chat-model",
                        embedding_model=embedding_model, api_key="lm-studio", embed_batch=2, max_retries=3)
    return OpenAICompatibleLLM(cfg, transport=fake_server(calls, **kw))


def test_generate_is_deterministic_and_strips_reasoning():
    calls = []
    text = llm(calls).generate("TITLE: Plan summary\n- fact one", system="You write plan documents.")
    assert text.startswith("Plan summary:") and "<think>" not in text
    body = json.loads(calls[0].content)
    assert body["temperature"] == 0 and body["seed"] == 0 and body["model"] == "chat-model"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert calls[0].headers["authorization"] == "Bearer lm-studio"


def test_embeddings_are_batched_and_ordered():
    calls = []
    vecs = llm(calls).embed(["a", "b", "c"], task="query")
    assert vecs.shape == (3, 3) and vecs.dtype == np.float32
    assert vecs[:, 0].tolist() == [0.0, 1.0, 0.0]  # batches of 2: indexes 0, 1 then 0
    assert len(calls) == 2


def test_without_embedding_model_uses_offline_embeddings():
    calls = []
    vecs = llm(calls, embedding_model="").embed(["prior authorization"], task="document")
    assert vecs.shape == (1, 256) and not calls


def test_retries_transient_errors_then_fails_clearly():
    calls = []
    assert llm(calls, fail_first=2).generate("hello")
    calls = []
    with pytest.raises(LLMError, match="Is the server running"):
        llm(calls, fail_first=99).generate("hello")


def test_health_warns_when_model_not_loaded():
    assert llm([]).health().startswith("ok")
    assert "not loaded" in llm([], models=("other",)).health()


def test_memory_local_profile_builds():
    stores = build_stores(load_profile("memory-local"))
    assert type(stores.llm).__name__ == "OpenAICompatibleLLM" and stores.llm.base_url == "http://localhost:1235/v1"
