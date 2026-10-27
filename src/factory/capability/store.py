"""Where published capability packs are kept. Packs are global (shared by all tenants), so this is not a per-run
store. Each published version is kept; the registry activates the highest version per pack id.

Profile key `packs`:  {kind: memory} | {kind: local, root: out/packs} | {kind: gcs, bucket: ..., prefix: packs}
The implementations are adapters (adapters/memory.py, local_fs.py, gcp.py); this module holds the interface.
"""

from __future__ import annotations

from typing import Any, Protocol

from factory.adapters import build_pack_store
from factory.config import AdapterConfig

__all__ = ["AdapterConfig", "PackStore", "build_pack_store"]


class PackStore(Protocol):
    def put(self, doc: dict[str, Any]) -> str: ...
    def all(self) -> list[dict[str, Any]]: ...
    def delete(self, pack_id: str) -> None: ...
