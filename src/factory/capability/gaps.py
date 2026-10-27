"""Gap detection (no LLM) and capability builds: what in a stage payload the engines cannot serve yet.

A build tracks a set of gaps until active packs cover them all. Phase 1 has no agents: a build waits for packs to be
published (POST /factory/capabilities/packs). Phase 2 plugs an agent builder into Builds.start().
Builds are keyed by their gap signature, so the same missing capability requested twice shares one build.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from factory.data_engine import catalog
from factory.data_engine.scenarios import SCENARIOS

NOTE_PHASE1 = "Phase 1: publish the packs with POST /factory/capabilities/packs; phase 2 agents will build them."


@dataclass(frozen=True)
class Gap:
    kind: str  # entity | scenario | use_case | endpoint
    name: str
    stage: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"


def payload_gaps(stage: str, payload: dict[str, Any]) -> list[Gap]:
    """Items of one stage payload that no core capability or active pack covers."""
    out: list[Gap] = []
    if stage == "s8":
        from factory.api_engine.endpoints import ENDPOINT_CATALOG, ENDPOINTS

        base = payload.get("base_path", "/v1")
        for uc in payload.get("use_cases") or []:
            if uc not in ENDPOINTS:
                out.append(Gap("use_case", uc, stage))
        for ep in payload.get("endpoints") or []:
            uc = ep.get("use_case")
            if uc in ENDPOINTS or uc == "COMMON":
                path = str(ep.get("path", ""))
                rel = path[len(base):] if path.startswith(base) else path
                method = str(ep.get("method", "")).upper()
                if (method, rel) not in ENDPOINT_CATALOG.get(uc, []):
                    out.append(Gap("endpoint", f"{method} {rel}", stage))
        return out
    for e in payload.get("entities") or []:
        if e not in catalog.PRIMARY_KEY:
            out.append(Gap("entity", e, stage))
    for s in payload.get("scenario_mix") or []:
        sid = s.get("scenario_id") if isinstance(s, dict) else getattr(s, "scenario_id", None)
        if sid and sid not in SCENARIOS:
            out.append(Gap("scenario", sid, stage))
    return out


def covered(gap: Gap) -> bool:
    """Whether an active capability now covers the gap."""
    from factory.api_engine.endpoints import ENDPOINT_CATALOG, ENDPOINTS

    if gap.kind == "entity":
        return gap.name in catalog.PRIMARY_KEY
    if gap.kind == "scenario":
        return gap.name in SCENARIOS
    if gap.kind == "use_case":
        return gap.name in ENDPOINTS
    method, rel = gap.name.split(" ", 1)
    return any((method, rel) in eps for eps in ENDPOINT_CATALOG.values())


@dataclass
class Build:
    build_id: str
    gaps: list[Gap]
    created_at: float = field(default_factory=time.time)
    source: str = "preflight"  # preflight | s3..s8 (202 fallback)
    status: str = "PENDING_PACKS"
    remaining: list[Gap] = field(default_factory=list)
    note: str = NOTE_PHASE1

    def refresh(self) -> "Build":
        self.remaining = [g for g in self.gaps if not covered(g)]
        self.status = "READY" if not self.remaining else "PENDING_PACKS"
        return self

    def view(self) -> dict[str, Any]:
        self.refresh()
        return {"build_id": self.build_id, "status": self.status, "source": self.source,
                "gaps": [asdict(g) for g in self.gaps], "remaining": [asdict(g) for g in self.remaining],
                "note": None if self.status == "READY" else self.note}


class Builds:
    """Open builds, keyed by gap signature. Phase 2 replaces start() with an agent builder."""

    def __init__(self) -> None:
        self._builds: dict[str, Build] = {}
        self._lock = threading.Lock()

    def start(self, gaps: list[Gap], source: str) -> Build:
        sig = "|".join(sorted({g.key for g in gaps}))
        build_id = "bld_" + hashlib.sha256(sig.encode()).hexdigest()[:12]
        with self._lock:
            build = self._builds.setdefault(build_id, Build(build_id, sorted(set(gaps), key=lambda g: g.key), source=source))
        return build.refresh()

    def get(self, build_id: str) -> Build | None:
        b = self._builds.get(build_id)
        return b.refresh() if b else None


def preflight(builds: Builds, payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Gaps across all stage payloads; READY, or a build that tracks them."""
    gaps = [g for st, p in sorted(payloads.items()) for g in payload_gaps(st, p)]
    if not gaps:
        return {"status": "READY", "gaps": [], "build_id": None}
    return builds.start(gaps, "preflight").view()
