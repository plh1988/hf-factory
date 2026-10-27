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
from typing import Any, Callable

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
    context: dict[str, Any] = field(default_factory=dict)  # payload facts the agents read (names, paths)
    status: str = "PENDING_PACKS"
    remaining: list[Gap] = field(default_factory=list)
    note: str = NOTE_PHASE1
    # agent builds (phase 2)
    running: bool = False
    agent_status: str | None = None  # READY | UNDERSPECIFIED | FAILED, from the last agent run
    steps: list[Any] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    error: str | None = None
    attempts: int = 0
    finished_at: float | None = None

    def refresh(self) -> "Build":
        self.remaining = [g for g in self.gaps if not covered(g)]
        if self.running:  # until the agent run has recorded its outcome, even if its packs already cover the gaps
            self.status = "BUILDING"
        elif not self.remaining:
            self.status = "READY"
        elif self.agent_status in ("UNDERSPECIFIED", "FAILED"):
            self.status = self.agent_status
        elif self.agent_status == "READY":  # agents finished but a gap is still open (e.g. unsupported)
            self.status = "FAILED"
        else:
            self.status = "PENDING_PACKS"
        return self

    def view(self) -> dict[str, Any]:
        self.refresh()
        out = {"build_id": self.build_id, "status": self.status, "source": self.source,
               "gaps": [asdict(g) for g in self.gaps], "remaining": [asdict(g) for g in self.remaining]}
        if self.attempts:
            out.update({"attempts": self.attempts, "published": self.published, "questions": self.questions,
                        "error": self.error, "steps": [s.summary() for s in self.steps],
                        "assumptions": _assumptions(self.plan)})
        elif self.status != "READY":
            out["note"] = self.note
        return out


def _assumptions(plan: dict[str, Any] | None) -> list[str]:
    if not plan:
        return []
    items = plan.get("entities", []) + plan.get("use_cases", [])
    return [f"{i.get('name') or i.get('code')}: {a}" for i in items for a in i.get("assumptions", [])]


class Builds:
    """Open builds, keyed by gap signature. With a builder (phase 2 agents), a new build runs it in the background;
    one build runs at a time (a local model serves one request well)."""

    def __init__(self, builder: Callable[[Build], None] | None = None) -> None:
        self._builds: dict[str, Build] = {}
        self._lock = threading.Lock()
        self._one_at_a_time = threading.Lock()
        self.builder = builder

    def start(self, gaps: list[Gap], source: str, context: dict[str, Any] | None = None) -> Build:
        sig = "|".join(sorted({g.key for g in gaps}))
        build_id = "bld_" + hashlib.sha256(sig.encode()).hexdigest()[:12]
        with self._lock:
            build = self._builds.get(build_id)
            if build is None:
                build = Build(build_id, sorted(set(gaps), key=lambda g: g.key), source=source, context=context or {})
                self._builds[build_id] = build
                self._launch(build)
        return build.refresh()

    def retry(self, build_id: str) -> Build | None:
        with self._lock:
            build = self._builds.get(build_id)
            if build is not None and not build.running and build.refresh().status != "READY":
                self._launch(build)
        return build.refresh() if build else None

    def _launch(self, build: Build) -> None:
        if self.builder is None or not build.refresh().remaining:
            return
        build.running, build.attempts = True, build.attempts + 1
        threading.Thread(target=self._run, args=(build,), name=build.build_id, daemon=True).start()

    def _run(self, build: Build) -> None:
        try:
            with self._one_at_a_time:
                self.builder(build)
        except Exception as exc:  # never lose a build to an unexpected error
            build.agent_status, build.error = "FAILED", repr(exc)[:600]
        finally:
            build.running, build.finished_at = False, time.time()

    def get(self, build_id: str) -> Build | None:
        b = self._builds.get(build_id)
        return b.refresh() if b else None


def payload_context(payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """What the agents may read from the payloads: names and paths only (no blueprint, no descriptions)."""
    ctx: dict[str, Any] = {}
    for st, p in sorted(payloads.items()):
        keep = {k: p[k] for k in ("entities", "use_cases", "endpoints", "base_path") if p.get(k)}
        mix = p.get("scenario_mix")
        if mix:
            keep["scenario_ids"] = [m.get("scenario_id") if isinstance(m, dict) else m for m in mix]
        if keep:
            ctx[st] = keep
    return ctx


def preflight(builds: Builds, payloads: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Gaps across all stage payloads; READY, or a build that tracks (and, with agents, builds) them."""
    gaps = [g for st, p in sorted(payloads.items()) for g in payload_gaps(st, p)]
    if not gaps:
        return {"status": "READY", "gaps": [], "build_id": None}
    return builds.start(gaps, "preflight", payload_context(payloads)).view()
