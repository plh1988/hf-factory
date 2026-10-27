"""Agentic capability layer: capability packs extend what the engines can generate and serve.

  store.py     where published packs are kept (memory, local folder, Cloud Storage)
  registry.py  validate (G1), gate (G2-G5 in a sandbox), publish and activate packs
  gates.py     the sandbox gates
  sandbox.py   small stage payloads for gate runs
  gaps.py      gap detection in stage payloads, preflight, builds

Phase 1: packs are written by hand and published through the control plane; builds wait for them.
Phase 2: agents (with the capability skill) write the packs. See docs/agentic-capability-layer.md.
"""

from __future__ import annotations

from factory.capability.gaps import Builds, Gap, payload_gaps, preflight
from factory.capability.registry import CapabilityRegistry, PackRejected, Published
from factory.capability.store import build_pack_store

__all__ = ["Builds", "CapabilityRegistry", "Gap", "PackRejected", "Published", "build_pack_store", "payload_gaps",
           "preflight", "reset_packs"]


def reset_packs() -> None:
    """Deactivate every non-core pack in this process (tests, demos)."""
    CapabilityRegistry(build_pack_store(None)).reset()
