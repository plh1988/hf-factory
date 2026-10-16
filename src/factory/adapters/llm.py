"""LLM adapters: Vertex AI (Gemini + text embeddings) and a deterministic offline stand-in."""

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
