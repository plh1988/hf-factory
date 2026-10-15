"""LLM adapters: Vertex AI (Gemini + text embeddings) and a deterministic offline stand-in."""

from __future__ import annotations

import re
import zlib

import numpy as np

from factory.config import AdapterConfig


class VertexLLM:
    """Vertex AI through the google-genai SDK. Auth = Application Default Credentials."""

    def __init__(self, cfg: AdapterConfig) -> None:
        from google import genai  # imported lazily: only this adapter needs it

        self.model = cfg.get("model")
        self.embedding_model = cfg.get("embedding_model")
        self.embedding_dim = int(cfg.get("embedding_dim", 256))
        self._project = cfg.get("project")
        self._location = cfg.get("location", "us-central1")
        if not self._project or self._project == "your-gcp-project-id":
            raise ValueError("set llm.project in the profile yaml to your GCP project id")
        self._client = genai.Client(vertexai=True, project=self._project, location=self._location)

    def generate(self, prompt: str, system: str = "") -> str:
        from google.genai import types

        cfg = types.GenerateContentConfig(temperature=0, seed=0, system_instruction=system or None)
        resp = self._client.models.generate_content(model=self.model, contents=prompt, config=cfg)
        return (resp.text or "").strip()

    def embed(self, texts: list[str]) -> np.ndarray:
        from google.genai import types

        out: list[list[float]] = []
        cfg = types.EmbedContentConfig(output_dimensionality=self.embedding_dim)
        for i in range(0, len(texts), 100):
            resp = self._client.models.embed_content(model=self.embedding_model, contents=texts[i : i + 100], config=cfg)
            out += [e.values for e in resp.embeddings]
        return np.asarray(out, dtype=np.float32)

    def health(self) -> str:
        self.embed(["health check"])
        return f"ok (Vertex AI {self._project}/{self._location}, {self.model})"


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

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.embedding_dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for tok in re.findall(r"[a-z0-9]+", text.lower()):
                out[row, zlib.crc32(tok.encode()) % self.embedding_dim] += 1.0
        return out

    def health(self) -> str:
        return "ok (offline deterministic LLM)"
