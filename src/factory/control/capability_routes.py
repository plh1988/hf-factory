"""/factory/capabilities: publish capability packs, preflight stage payloads, follow builds (agentic layer)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from factory.capability import Builds, CapabilityRegistry, PackRejected, preflight
from factory.control.deps import get_registry
from factory.control.errors import JobRejected, RejectCode
from factory.control.registry import Registry
from factory.data_engine import packs

router = APIRouter(tags=["Capabilities"])


def get_capabilities(request: Request) -> CapabilityRegistry:
    return request.app.state.capabilities


def get_builds(request: Request) -> Builds:
    return request.app.state.builds


@router.post("/factory/capabilities/packs", status_code=201, summary="Publish a capability pack (gated, then active)")
def publish_pack(doc: dict[str, Any] = Body(..., description="pack document (entity or use_case), as JSON"),
                 gates: bool = Query(True, description="run the sandbox gates (G2-G5); static checks always run"),
                 registry: Registry = Depends(get_registry), caps: CapabilityRegistry = Depends(get_capabilities)) -> Any:
    with registry.lock:  # no stage runs while the catalog changes
        try:
            pub = caps.publish(doc, run_gates=gates)
        except PackRejected as exc:
            code = RejectCode.CAPABILITY_GATES_FAILED if exc.results else RejectCode.CAPABILITY_INVALID
            raise JobRejected(code, str(exc), [r.__dict__ for r in exc.results] or None) from None
    return {"ref": pub.ref, "digest": pub.digest, "stored_at": pub.stored_at, "status": "ACTIVE", "gates": pub.gates}


@router.get("/factory/capabilities/packs", summary="Active capability packs (core and published)")
def list_packs(caps: CapabilityRegistry = Depends(get_capabilities)) -> Any:
    return {"packs": caps.describe()}


@router.get("/factory/capabilities/packs/{pack_id}", summary="One active pack, as published")
def get_pack(pack_id: str) -> Any:
    pack = next((p for p in packs.active() if p.id == pack_id), None)
    if pack is None:
        raise JobRejected(RejectCode.NO_PACK, f"No active pack {pack_id!r}.")
    return {"digest": pack.digest(), "pack": pack.model_dump(mode="json", by_alias=True)}


@router.delete("/factory/capabilities/packs/{pack_id}", summary="Deactivate and delete a published pack")
def delete_pack(pack_id: str, registry: Registry = Depends(get_registry), caps: CapabilityRegistry = Depends(get_capabilities)) -> Any:
    with registry.lock:
        try:
            removed = caps.remove(pack_id)
        except PackRejected as exc:
            raise JobRejected(RejectCode.PACK_IN_USE, str(exc)) from None
    if not removed:
        raise JobRejected(RejectCode.NO_PACK, f"No active pack {pack_id!r}.")
    return {"pack_id": pack_id, "removed": True}


@router.post("/factory/capabilities/preflight", summary="Check stage payloads for capabilities the engines lack")
def run_preflight(body: dict[str, Any] = Body(..., examples=[{"payloads": {"s4": {"stage": "s4", "entities": ["premium_invoice"]}}}]),
                  builds: Builds = Depends(get_builds)) -> Any:
    payloads = body.get("payloads")
    if not isinstance(payloads, dict) or not payloads:
        raise JobRejected(RejectCode.CAPABILITY_INVALID, "Send {\"payloads\": {\"s3\": {...}, ..., \"s8\": {...}}}.")
    return preflight(builds, payloads)


@router.post("/factory/capabilities/builds/{build_id}/retry", summary="Run the agents again for a FAILED or UNDERSPECIFIED build")
def retry_build(build_id: str, builds: Builds = Depends(get_builds)) -> Any:
    build = builds.retry(build_id)
    if build is None:
        raise JobRejected(RejectCode.NO_BUILD, f"No build {build_id!r}.")
    return build.view()


@router.get("/factory/capabilities/builds/{build_id}/log", summary="Every agent step of a build, with prompts and responses")
def build_log(build_id: str, builds: Builds = Depends(get_builds)) -> Any:
    build = builds.get(build_id)
    if build is None:
        raise JobRejected(RejectCode.NO_BUILD, f"No build {build_id!r}.")
    return {"build_id": build_id, "status": build.status, "plan": build.plan,
            "steps": [{**s.summary(), "prompt": s.prompt, "response": s.response} for s in build.steps]}


@router.get("/factory/capabilities/builds/{build_id}", summary="Build status: READY once active packs cover every gap")
def get_build(build_id: str, builds: Builds = Depends(get_builds)) -> Any:
    build = builds.get(build_id)
    if build is None:
        raise JobRejected(RejectCode.NO_BUILD, f"No build {build_id!r}.")
    return build.view()


def build_started(builds: Builds, stage: str, payload: dict[str, Any]) -> JSONResponse | None:
    """202 fallback for a stage payload with gaps (capability layer on); None when the payload has no gaps."""
    from factory.capability import payload_context, payload_gaps

    gaps = payload_gaps(stage, payload)
    if not gaps:
        return None
    view = builds.start(gaps, stage, payload_context({stage: payload})).view()
    return JSONResponse({"error": "CAPABILITY_BUILD_STARTED", "message": f"{stage} asks for capabilities the engines do not have yet. "
                         "Follow the build; resend this payload when it is READY. Preflight all payloads first to build them together.",
                         **view}, status_code=202)
