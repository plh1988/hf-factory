"""Local-folder adapters: blobs and the LLM text cache as plain files, for local runs you want to open and keep.

Layout (root is relative to the repository unless absolute; `out/` is git-ignored):

  <blobs root>/<tenant>/<key>        e.g. out/local-llm/docs/pa_policy/<asset_id>.md, manifest/<seed>.json,
                                          reports/rule-report.json, jobs/s4.json
  <text cache root>/<key>.txt        generated LLM text by content hash; shared by all tenants, since the key
                                     already covers model, prompt version, prompt and seed

Same contract as the in-memory and Cloud Storage adapters: get() raises KeyError for a missing blob, list() returns
sorted keys relative to the tenant folder. Files survive a server restart; deleting a run does not delete them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from factory.config import REPO_ROOT, AdapterConfig

_SAFE_PART = re.compile(r"^[A-Za-z0-9._\-]+$")


def _root(cfg: AdapterConfig, default: str) -> Path:
    root = Path(cfg.get("root", default))
    return root if root.is_absolute() else REPO_ROOT / root


def _ns(namespace: str | None) -> str:
    return re.sub(r"[^a-z0-9_\-]", "_", (namespace or "default").lower())


def _path(base: Path, key: str) -> Path:
    """Key -> file under base. Rejects absolute keys and any part that could leave the folder ('..', drive, etc.)."""
    parts = key.split("/")
    if not key or any(p in ("", ".", "..") or not _SAFE_PART.match(p) for p in parts):
        raise ValueError(f"unsafe blob key {key!r}")
    return base.joinpath(*parts)


class FileBlobStore:
    """Blobs as files under <root>/<tenant>/. Profile keys: root (default out)."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.base = _root(cfg, "out") / _ns(namespace)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = _path(self.base, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # atomic: readers never see a half-written file
        return path.as_uri()

    def get(self, key: str) -> bytes:
        try:
            return _path(self.base, key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None

    def list(self, prefix: str = "") -> list[str]:
        if not self.base.exists():
            return []
        keys = (p.relative_to(self.base).as_posix() for p in self.base.rglob("*") if p.is_file() and not p.name.endswith(".tmp"))
        return sorted(k for k in keys if k.startswith(prefix))

    def health(self) -> str:
        self.base.mkdir(parents=True, exist_ok=True)
        return f"ok (folder {self.base})"


class FileTextCache:
    """LLM text by content key as <root>/<key>.txt. Profile keys: root (default out/cache/text)."""

    def __init__(self, cfg: AdapterConfig, namespace: str | None = None) -> None:
        self.base = _root(cfg, "out/cache/text")

    def get(self, key: str) -> str | None:
        try:
            return _path(self.base, f"{key}.txt").read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def put(self, key: str, text: str) -> None:
        path = _path(self.base, f"{key}.txt")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def health(self) -> str:
        self.base.mkdir(parents=True, exist_ok=True)
        n = sum(1 for _ in self.base.glob("*.txt"))
        return f"ok (folder {self.base}, {n} cached texts)"


def _pack_file(doc: dict) -> str:
    return f"{doc['id']}@{doc['version']}.json"


class LocalPackStore:
    """<root>/<id>@<version>.json (root relative to the repository unless absolute)."""

    def __init__(self, cfg: AdapterConfig) -> None:
        root = Path(cfg.get("root", "out/packs"))
        self.root = root if root.is_absolute() else REPO_ROOT / root

    def put(self, doc: dict[str, Any]) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / _pack_file(doc)
        path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")
        return path.as_uri()

    def all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*@*.json"))]

    def delete(self, pack_id: str) -> None:
        for p in self.root.glob(f"{pack_id}@*.json"):
            p.unlink()
