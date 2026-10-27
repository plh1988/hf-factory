"""REST control plane: trigger Data Engine stages s3-s7 and API Engine stage s8, one call each.

  POST /factory/data-engine/s3 .. /s7    body = the stage job payload (architecture spec, scenario S1)
  POST /factory/api-engine/s8            body = the s8 job payload
  GET  /factory/runs[/{tenant}]          stage status per tenant
  GET  /factory/runs/{tenant}/manifest   manifest after s7
  GET  /factory/runs/{tenant}/rule-report
  GET  /factory/runs/{tenant}/tables/{entity}?limit=20&scenario_id=PCS-02   peek at generated rows
  GET  /factory/runs/{tenant}/credentials   local test credentials for the tenant API (memory profiles)
  DELETE /factory/runs/{tenant}          drop the tenant's in-memory run and start over

The engines use only their payloads; there is no blueprint. s3 starts a run (tenant from consumes.alloydb).
After s8 succeeds, the tenant's business API (/oauth/token, /v1/..., /docs) is served by this same server.
Control-plane docs: /factory/docs. Local only: bind to 127.0.0.1 (no auth on /factory in v1).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from factory.capability import Builds, CapabilityRegistry, build_pack_store
from factory.control import api_engine_routes, capability_routes, data_engine_routes, run_routes
from factory.control.deps import TenantApiMount, get_registry, rejected_handler
from factory.control.errors import JobRejected
from factory.control.registry import Registry


def create_control_app(registry: Registry, capability_layer: bool = False, load_packs: bool = True) -> FastAPI:
    """The control-plane FastAPI app: the data-engine, API-engine and runs routers over one Registry.

    After a successful s8 the tenant API is mounted at / on this same app (TenantApiMount), so one server serves
    both. Handlers raise JobRejected; the exception handler turns it into the JSON error body.
    """
    app = FastAPI(title="Healthcare AI Platform Factory - engine control plane", version="0.3.0",
                  description=__doc__, docs_url="/factory/docs", openapi_url="/factory/openapi.json", redoc_url=None)
    app.state.registry = registry
    app.state.tenant_api = TenantApiMount(app)
    # capability layer: published packs are global; load them from the profile's pack store at start
    app.state.capabilities = CapabilityRegistry(build_pack_store(registry.profile.packs))
    if load_packs:
        app.state.capabilities.load()
    app.state.builds = Builds()
    app.state.capability_layer = capability_layer  # `factory serve` passes FACTORY_CAPABILITY_LAYER
    app.add_exception_handler(JobRejected, rejected_handler)
    app.include_router(data_engine_routes.router)
    app.include_router(api_engine_routes.router)
    app.include_router(run_routes.router)
    app.include_router(capability_routes.router)

    @app.get("/factory/healthz", tags=["Ops"], include_in_schema=False)
    def healthz(reg: Registry = Depends(get_registry)) -> dict[str, Any]:
        return {"status": "ok", "profile": reg.profile.name, "runs": sorted(reg.runs)}

    return app
