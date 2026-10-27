"""Capability packs: entities and use cases declared as data and run by generic code (agentic capability layer).

  model.py    the pack format (Pydantic): entity packs, use-case packs, building blocks
  engine.py   runs an entity pack over anchor rows and writes the entity
  core/       built-in packs shipped with the engine (core: true), active from import

This module holds the *active* packs the engines read. The capability registry (factory.capability) decides
what is active: it validates, gates and publishes packs, then calls activate() / deactivate() here.
See docs/agentic-capability-layer.md.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from factory.data_engine import catalog
from factory.data_engine.packs.engine import AnchorFrame, generate
from factory.data_engine.packs.model import EntityPack, Pack, UseCasePack, parse_pack

CORE_DIR = Path(__file__).parent / "core"

__all__ = ["AnchorFrame", "EntityPack", "Pack", "UseCasePack", "activate", "active", "core_packs", "deactivate",
           "entity_pack", "for_anchor", "generate", "load_pack", "parse_pack", "pins"]

_ACTIVE: dict[str, EntityPack | UseCasePack] = {}


def load_pack(path: Path) -> EntityPack | UseCasePack:
    """Read and validate one pack file (YAML or JSON), of either kind."""
    try:
        return parse_pack(yaml.safe_load(path.read_text(encoding="utf-8")))
    except ValueError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def core_packs() -> tuple[EntityPack | UseCasePack, ...]:
    """Built-in packs, sorted by id."""
    return tuple(sorted((p for p in _ACTIVE.values() if p.core), key=lambda p: p.id))


def active() -> list[EntityPack | UseCasePack]:
    """All active packs, sorted by id so generation order is stable."""
    return sorted(_ACTIVE.values(), key=lambda p: p.id)


def activate(pack: EntityPack | UseCasePack) -> None:
    """Make a pack visible to the engines: catalog entries, then the active list. Replaces an older version."""
    if isinstance(pack, EntityPack):
        catalog.register_entity(pack.entity.name, pack.entity.stage, pack.entity.primary_key)
    else:
        uc = pack.use_case
        catalog.register_use_case(uc.code, {
            "name": uc.name, "group": uc.group, "personas": uc.personas, "can": uc.can, "escalate": uc.escalate,
            "never": uc.never, "verification": uc.verification, "handoff_queue": uc.handoff_queue, "faqs": uc.faqs,
        }, uc.caregiver_scope, uc.slot)
    _ACTIVE[pack.id] = pack


def deactivate(pack_id: str) -> None:
    pack = _ACTIVE.get(pack_id)
    if pack is None or pack.core:
        return
    if isinstance(pack, EntityPack):
        catalog.unregister_entity(pack.entity.name)
    else:
        catalog.unregister_use_case(pack.use_case.code)
    del _ACTIVE[pack_id]


def entity_pack(name: str) -> EntityPack | None:
    p = _ACTIVE.get(f"entity.{name}")
    return p if isinstance(p, EntityPack) else None


def use_case_pack(code: str) -> UseCasePack | None:
    p = _ACTIVE.get(f"uc.{code}")
    return p if isinstance(p, UseCasePack) else None


def for_anchor(anchor: str) -> list[EntityPack]:
    """Active entity packs whose entity hangs off this anchor."""
    return [p for p in active() if isinstance(p, EntityPack) and p.entity.anchor == anchor]


def pins(entities: set[str] | None = None, use_cases: list[str] | None = None) -> dict[str, str]:
    """{pack ref: digest} of the active packs a run uses: recorded in the manifest so a replay uses exactly these."""
    out = {}
    for p in active():
        used = (isinstance(p, EntityPack) and entities is not None and p.entity.name in entities) or \
               (isinstance(p, UseCasePack) and use_cases is not None and p.use_case.code in use_cases)
        if used:
            out[p.ref] = p.digest()
    return out


for _path in sorted(CORE_DIR.glob("*.yaml")):
    _pack = load_pack(_path)
    if not _pack.core:
        raise ValueError(f"{_path.name}: packs in {CORE_DIR.name}/ must set core: true")
    activate(_pack)
