"""Capability packs: entities declared as data and run by a generic generator (agentic capability layer, phase 0).

  model.py    the pack format (Pydantic): entity, columns, building blocks
  engine.py   runs a pack over anchor rows and writes the entity
  core/       built-in packs shipped with the engine (core: true)

Phase 0 ships one core pack, sdoh_indicator, which replaced its coded generator with identical output.
See docs/agentic-capability-layer.md.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

from factory.data_engine.packs.engine import AnchorFrame, generate
from factory.data_engine.packs.model import Pack

CORE_DIR = Path(__file__).parent / "core"

__all__ = ["AnchorFrame", "Pack", "core_packs", "for_anchor", "generate", "load_pack"]


def load_pack(path: Path) -> Pack:
    """Read and validate one pack file (YAML or JSON)."""
    try:
        return Pack.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


@cache
def core_packs() -> tuple[Pack, ...]:
    """Built-in packs, sorted by id so generation order is stable."""
    packs = [load_pack(p) for p in sorted(CORE_DIR.glob("*.yaml"))]
    for p in packs:
        if not p.core:
            raise ValueError(f"{p.id}: packs in {CORE_DIR.name}/ must set core: true")
    return tuple(sorted(packs, key=lambda p: p.id))


def for_anchor(anchor: str) -> list[Pack]:
    """Active packs whose entity hangs off this anchor (phase 0: the core packs)."""
    return [p for p in core_packs() if p.entity.anchor == anchor]
