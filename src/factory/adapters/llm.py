"""LLM adapters: Vertex AI (Gemini + text embeddings), any OpenAI-compatible server (LM Studio, Ollama, vLLM)
for local testing, and a deterministic offline stand-in."""

from __future__ import annotations

import logging
import re
import time
import zlib
from typing import Literal

import numpy as np

from factory.config import AdapterConfig

log = logging.getLogger("factory.llm")
EmbedTask = Literal["document", "query"]

RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


class VertexLLM:
    """Vertex AI through the google-genai SDK. Auth = Application Default Credentials
    (gcloud auth application-default login locally; the service account on Cloud Run).

    Profile keys: project, location, model, embedding_model, embedding_dim,
    optional max_output_tokens (2048), max_retries (5), embed_batch (100)."""

    def __init__(self, cfg: AdapterConfig) -> None:
        from google import genai  # imported lazily: only this adapter needs it

        self.model = cfg.get("model")
        self.embedding_model = cfg.get("embedding_model")
        self.embedding_dim = int(cfg.get("embedding_dim", 256))
        self.max_output_tokens = int(cfg.get("max_output_tokens", 2048))
        self.max_retries = int(cfg.get("max_retries", 5))
        self.embed_batch = int(cfg.get("embed_batch", 100))
        self._project = cfg.get("project")
        self._location = cfg.get("location", "us-central1")
        if not self._project or self._project == "your-gcp-project-id":
            raise ValueError("set llm.project in the profile yaml to your GCP project id")
        self._client = genai.Client(vertexai=True, project=self._project, location=self._location)

    def _call(self, fn, what: str):
        """Retry transient failures (rate limits, server errors) with exponential backoff."""
        from google.genai import errors

        delay = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn()
            except errors.APIError as exc:
                code = getattr(exc, "code", None)
                if code not in RETRYABLE_CODES or attempt == self.max_retries:
                    raise LLMError(f"Vertex AI {what} failed ({code}): {exc}") from exc
                log.warning("Vertex AI %s: HTTP %s, retry %d/%d in %.0fs", what, code, attempt, self.max_retries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise LLMError(f"Vertex AI {what} failed")

    def generate(self, prompt: str, system: str = "") -> str:
        from google.genai import types

        cfg = types.GenerateContentConfig(
            temperature=0, seed=0, candidate_count=1, max_output_tokens=self.max_output_tokens,
            system_instruction=system or None,
        )
        resp = self._call(lambda: self._client.models.generate_content(model=self.model, contents=prompt, config=cfg), "generate")
        text = (resp.text or "").strip()
        if not text:
            reason = None
            if resp.candidates:
                reason = getattr(resp.candidates[0], "finish_reason", None)
            raise LLMError(f"Vertex AI returned no text (finish_reason={reason}); the prompt may have been blocked.")
        return text

    def embed(self, texts: list[str], task: EmbedTask = "document") -> np.ndarray:
        from google.genai import types

        cfg = types.EmbedContentConfig(
            output_dimensionality=self.embedding_dim,
            task_type="RETRIEVAL_DOCUMENT" if task == "document" else "RETRIEVAL_QUERY",
            auto_truncate=True,
        )
        out: list[list[float]] = []
        for i in range(0, len(texts), self.embed_batch):
            batch = texts[i : i + self.embed_batch]
            resp = self._call(lambda b=batch: self._client.models.embed_content(model=self.embedding_model, contents=b, config=cfg), "embed")
            out += [e.values for e in resp.embeddings]
        if len(out) != len(texts):
            raise LLMError(f"Vertex AI returned {len(out)} embeddings for {len(texts)} texts")
        return np.asarray(out, dtype=np.float32)

    def health(self) -> str:
        self.embed(["health check"], task="query")
        return f"ok (Vertex AI {self._project}/{self._location}, {self.model}, {self.embedding_model})"


class OpenAICompatibleLLM:
    """Any server with the OpenAI REST contract: LM Studio (default http://localhost:1234/v1), Ollama, vLLM.

    Profile keys: base_url, model, optional api_key (LM Studio ignores it), embedding_model (empty: offline hashed
    embeddings, for servers with only a chat model loaded), embedding_dim (offline fallback only),
    max_output_tokens (2048), timeout_s (300: local models can be slow), max_retries (3), embed_batch (32).

    Calls use temperature 0 and a fixed seed. Reasoning models' <think>...</think> blocks are removed from replies.
    """

    def __init__(self, cfg: AdapterConfig, transport=None) -> None:
        import httpx  # imported lazily, like the Vertex SDK

        self.base_url = str(cfg.get("base_url", "http://localhost:1234/v1")).rstrip("/")
        self.model = cfg.get("model")
        if not self.model:
            raise ValueError("set llm.model in the profile yaml to the model id loaded in the server (GET /v1/models)")
        self.embedding_model = cfg.get("embedding_model") or None
        self.max_output_tokens = int(cfg.get("max_output_tokens", 2048))
        self.max_retries = int(cfg.get("max_retries", 3))
        self.embed_batch = int(cfg.get("embed_batch", 32))
        self._fallback = None
        if not self.embedding_model:
            self._fallback = OfflineLLM(AdapterConfig(kind="offline", embedding_dim=cfg.get("embedding_dim", 256)))
            self.embedding_model = self._fallback.embedding_model
        headers = {"Authorization": f"Bearer {cfg.get('api_key')}"} if cfg.get("api_key") else {}
        self._http = httpx.Client(base_url=self.base_url, headers=headers, timeout=float(cfg.get("timeout_s", 300)),
                                  transport=transport)

    def _post(self, path: str, body: dict, what: str) -> dict:
        """POST with retries on connection errors, timeouts, 408/429/5xx."""
        import httpx

        delay = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._http.post(path, json=body)
                if resp.status_code not in RETRYABLE_CODES:
                    if resp.status_code >= 400:
                        raise LLMError(f"{self.base_url}{path} {what} failed ({resp.status_code}): {resp.text[:300]}")
                    return resp.json()
                problem = f"HTTP {resp.status_code}"
            except httpx.TransportError as exc:  # server not running, connection reset, timeout
                problem = repr(exc)
            if attempt == self.max_retries:
                raise LLMError(f"{self.base_url}{path} {what} failed after {attempt} attempts: {problem}. "
                               "Is the server running and the model loaded?")
            log.warning("%s %s: %s, retry %d/%d in %.0fs", self.base_url, what, problem, attempt, self.max_retries, delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        raise LLMError(f"{what} failed")

    def generate(self, prompt: str, system: str = "") -> str:
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        data = self._post("/chat/completions", {"model": self.model, "messages": messages, "temperature": 0, "seed": 0,
                                                "max_tokens": self.max_output_tokens, "stream": False}, "generate")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected chat completion response: {str(data)[:300]}") from exc
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
        if not text:
            raise LLMError(f"{self.model} returned no text (finish_reason={data['choices'][0].get('finish_reason')})")
        return text

    def embed(self, texts: list[str], task: EmbedTask = "document") -> np.ndarray:
        if self._fallback is not None:
            return self._fallback.embed(texts, task)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.embed_batch):
            data = self._post("/embeddings", {"model": self.embedding_model, "input": texts[i : i + self.embed_batch]}, "embed")
            out += [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        if len(out) != len(texts):
            raise LLMError(f"{self.base_url} returned {len(out)} embeddings for {len(texts)} texts")
        return np.asarray(out, dtype=np.float32)

    def health(self) -> str:
        resp = self._http.get("/models")
        resp.raise_for_status()
        loaded = [m.get("id") for m in resp.json().get("data", [])]
        wanted = [self.model] + ([] if self._fallback else [self.embedding_model])
        missing = [m for m in wanted if m not in loaded]
        if missing:
            return f"WARN {self.base_url}: {missing} not loaded; the server lists {loaded}"
        emb = "offline hashed embeddings" if self._fallback else self.embedding_model
        return f"ok ({self.base_url}, {self.model}, {emb})"


class OfflineLLM:
    """Deterministic stand-in. Turns the FACTS block of a prompt into plain paragraphs and
    embeds text as hashed bag-of-words vectors. No network access."""

    def __init__(self, cfg: AdapterConfig) -> None:
        self.model = cfg.get("model", "offline-template-v1")
        self.embedding_model = cfg.get("embedding_model", "hashed-bow-v1")
        self.embedding_dim = int(cfg.get("embedding_dim", 256))

    def generate(self, prompt: str, system: str = "") -> str:
        title = re.search(r"^TITLE: (.+)$", prompt, re.M)
        facts = re.findall(r"^- (.+)$", prompt, re.M)
        head = title.group(1) if title else "Document"
        body = "\n\n".join(f"{f.rstrip('.')}." for f in facts) or "No facts provided."
        return f"{head}\n\n{body}"

    def embed(self, texts: list[str], task: EmbedTask = "document") -> np.ndarray:
        out = np.zeros((len(texts), self.embedding_dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok in re.findall(r"[a-z0-9]+", text.lower()):
                out[row, zlib.crc32(tok.encode()) % self.embedding_dim] += 1.0
        return out

    def health(self) -> str:
        return "ok (offline deterministic LLM)"
