"""The capability registry: validates, gates, publishes and activates capability packs (process-wide: packs are
shared by all tenants, so activation changes the catalog the engines read).

publish(): G1 static checks -> baseline sandbox run -> activate -> G2-G5 gates -> keep (and store) or roll back.
Approval is automatic: a pack that passes the gates stays active.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Any

from factory.capability import gates
from factory.capability.store import PackStore
from factory.data_engine import catalog, packs
from factory.data_engine import rules as R
from factory.data_engine.packs.model import EntityPack, UseCasePack, parse_pack, sql_tables


class PackRejected(ValueError):
    """A pack that failed validation (G1) or a gate; `results` lists the gates run so far."""

    def __init__(self, message: str, results: list[gates.GateResult] | None = None) -> None:
        super().__init__(message)
        self.results = results or []


@dataclass
class Published:
    ref: str
    digest: str
    stored_at: str | None
    gates: list[dict[str, Any]] = field(default_factory=list)


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version.split("."))


class CapabilityRegistry:
    def __init__(self, store: PackStore) -> None:
        self.store = store
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ activation (engines see the pack)
    def _activate(self, pack: EntityPack | UseCasePack) -> None:
        packs.activate(pack)
        if isinstance(pack, UseCasePack):
            from factory.api_engine import generic
            from factory.control.validation import USE_CASE_TABLES

            entity_specs = {p.entity.name: p.entity for p in packs.active() if isinstance(p, EntityPack)}
            generic.register(pack, entity_specs)
            USE_CASE_TABLES[pack.use_case.code] = sorted({*pack.use_case.needs, "member_demographics"})

    def _deactivate(self, pack_id: str) -> None:
        pack = next((p for p in packs.active() if p.id == pack_id), None)
        if pack is None or pack.core:
            return
        if isinstance(pack, UseCasePack):
            from factory.api_engine import generic
            from factory.control.validation import USE_CASE_TABLES

            generic.unregister(pack.use_case.code)
            USE_CASE_TABLES.pop(pack.use_case.code, None)
        packs.deactivate(pack_id)

    def load(self) -> list[str]:
        """Activate the highest stored version of every pack (they passed their gates when published)."""
        latest: dict[str, EntityPack | UseCasePack] = {}
        for doc in self.store.all():
            p = parse_pack(doc)
            if p.id not in latest or _version_key(p.version) > _version_key(latest[p.id].version):
                latest[p.id] = p
        ordered = sorted(latest.values(), key=lambda p: (p.kind != "entity", p.id))  # entities before use cases
        with self.lock:
            for p in ordered:
                self.check(p)
                self._activate(p)
        return [p.ref for p in ordered]

    def reset(self) -> None:
        """Deactivate every non-core pack (tests)."""
        with self.lock:
            for p in [p for p in packs.active() if not p.core and p.kind == "use_case"]:
                self._deactivate(p.id)
            for p in [p for p in packs.active() if not p.core]:
                self._deactivate(p.id)

    # ------------------------------------------------------------------ G1: static checks across packs and the catalog
    def check(self, pack: EntityPack | UseCasePack) -> None:
        current = next((p for p in packs.active() if p.id == pack.id), None)
        if current is not None and current.core:
            raise PackRejected(f"{pack.id} is a core pack; it ships with the engine")
        if isinstance(pack, EntityPack):
            self._check_entity(pack)
        else:
            self._check_use_case(pack)

    def _check_entity(self, pack: EntityPack) -> None:
        spec = pack.entity
        if spec.name in catalog.CORE_ENTITIES:
            raise PackRejected(f"{spec.name} is a core entity")
        known = set(catalog.PRIMARY_KEY) | {spec.name}
        for c in spec.columns:
            ref = c.references
            if ref is not None:
                if ref.entity not in known:
                    raise PackRejected(f"column {c.name} references unknown entity {ref.entity}")
                if catalog.PRIMARY_KEY.get(ref.entity) != ref.column:
                    raise PackRejected(f"column {c.name} must reference the primary key of {ref.entity} "
                                       f"({catalog.PRIMARY_KEY.get(ref.entity)}), not {ref.column}")
        core_ids = {r.id for r in R.domain_rules("2000-01-01", "t")}
        other_ids = {r.id for p in packs.active() if isinstance(p, EntityPack) and p.id != pack.id for r in p.rules}
        for r in pack.rules:
            if r.id in core_ids | other_ids:
                raise PackRejected(f"rule id {r.id} is already used")
            unknown = (sql_tables(r.sql) | set(r.needs)) - known
            if unknown:
                raise PackRejected(f"rule {r.id} reads unknown tables {sorted(unknown)}")

    def _check_use_case(self, pack: UseCasePack) -> None:
        uc = pack.use_case
        if uc.code in catalog.CORE_USE_CASES:
            raise PackRejected(f"{uc.code} is a built-in use case")
        missing = [e for e in uc.needs if e not in catalog.PRIMARY_KEY]
        if missing:
            raise PackRejected(f"use case {uc.code} needs entities that are neither core nor active packs: {missing}")
        taken = {c: s for c, s in catalog.USE_CASE_SLOTS.items() if s == uc.slot and c != uc.code}
        if taken:
            raise PackRejected(f"knowledge slot {uc.slot} is taken by {sorted(taken)}; set knowledge_slot")
        for ep in uc.endpoints:
            ent = packs.entity_pack(ep.entity)
            if ent is not None:  # pack table: columns are known (core tables are checked by the contract-test gate)
                cols = {c.name for c in ent.entity.columns}
                unknown = sorted(ep.columns() - cols)
                if unknown:
                    raise PackRejected(f"{ep.operation_id}: {ep.entity} has no columns {unknown}")

    # ------------------------------------------------------------------ publish
    def publish(self, doc: dict[str, Any], run_gates: bool = True) -> Published:
        try:
            pack = parse_pack(doc)
        except ValueError as exc:
            raise PackRejected(str(exc)) from exc
        with self.lock:
            self.check(pack)
            previous = next((p for p in packs.active() if p.id == pack.id), None)
            results: list[gates.GateResult] = [gates.GateResult("G1 static checks", True)]
            if run_gates:
                if previous is not None:
                    self._deactivate(pack.id)
                before = gates.baseline(pack)
                self._activate(pack)
                try:
                    results += gates.run_gates(pack, before)
                except Exception as exc:  # a gate crashing is a failed gate
                    results.append(gates.GateResult("gates", False, repr(exc)[:400]))
                if not all(r.passed for r in results):
                    self._deactivate(pack.id)
                    if previous is not None:
                        self._activate(previous)
                    raise PackRejected(f"{pack.ref} failed its gates", results)
            else:
                self._activate(pack)
            stored = self.store.put(pack.model_dump(mode="json", by_alias=True))
        return Published(pack.ref, pack.digest(), stored, [asdict(r) for r in results])

    def remove(self, pack_id: str) -> bool:
        """Deactivate a published pack and delete it from the store (core packs cannot be removed)."""
        with self.lock:
            pack = next((p for p in packs.active() if p.id == pack_id), None)
            if pack is None:
                return False
            if pack.core:
                raise PackRejected(f"{pack_id} is a core pack")
            dependents = [p.id for p in packs.active() if isinstance(p, UseCasePack) and isinstance(pack, EntityPack)
                          and pack.entity.name in p.use_case.needs]
            if dependents:
                raise PackRejected(f"{pack_id} is needed by {dependents}; remove those first")
            self._deactivate(pack_id)
            self.store.delete(pack_id)
            return True

    def describe(self) -> list[dict[str, Any]]:
        return [{"id": p.id, "version": p.version, "kind": p.kind, "core": p.core, "digest": p.digest(),
                 "description": p.description} for p in packs.active()]
