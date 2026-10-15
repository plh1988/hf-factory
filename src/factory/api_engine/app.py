"""create_app(): one pre-built backend; the blueprint switches endpoints on (API engine design)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import JSONResponse

from factory import __version__
from factory.adapters.ports import Stores
from factory.api_engine.repository import Repository
from factory.api_engine.routes import ENDPOINTS, common
from factory.api_engine.security import ClientRegistry, FaultInjection, RateLimiter
from factory.contracts import Blueprint


class ApiEngineError(RuntimeError):
    pass


def create_app(bp: Blueprint, stores: Stores, fault_injection: bool = True) -> FastAPI:
    missing = [uc for uc in bp.scope.use_cases if uc not in ENDPOINTS]
    if missing:
        raise ApiEngineError(f"use cases without v1 endpoints: {missing}")
    base = bp.api_spec.base_path
    app = FastAPI(
        title=f"{bp.tenant.id} healthcare API (synthetic)",
        version=__version__,
        description=f"Generated from blueprint {bp.blueprint_id}. All data is synthetic. Use cases: {', '.join(bp.scope.use_cases)}.",
    )
    app.state.stores = stores
    app.state.repo = Repository(stores)
    app.state.clients = ClientRegistry(bp, stores)
    app.state.limiter = RateLimiter(per_minute=int(bp.infra_spec.sizing.get("concurrent_sessions", 30)) * 20)
    app.state.lookups = {}
    fi = bp.api_spec.fault_injection
    app.add_middleware(FaultInjection, error_rate_pct=fi.error_rate_pct, latency_p95_ms=fi.latency_p95_ms,
                       seed=bp.repeatability.root_seed, enabled=fault_injection)

    for uc in bp.scope.use_cases:
        app.include_router(ENDPOINTS[uc], prefix=base)
    app.include_router(common, prefix=base)

    @app.exception_handler(HTTPException)
    async def _http_error(_, exc: HTTPException):
        body = exc.detail if isinstance(exc.detail, dict) else {"error": "HTTP_ERROR", "message": str(exc.detail)}
        return JSONResponse(body, status_code=exc.status_code)

    @app.post("/oauth/token", tags=["Auth"], operation_id="oauth_token", include_in_schema=True,
              summary="OAuth2 client credentials token")
    def token(grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(...)) -> dict[str, Any]:
        if grant_type != "client_credentials":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        return {"access_token": app.state.clients.issue(client_id, client_secret), "token_type": "Bearer", "expires_in": 3600}

    @app.get("/healthz", tags=["Ops"], include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok", "tenant": bp.tenant.id}

    return app


def tool_manifest(openapi: dict[str, Any], base_path: str) -> list[dict[str, Any]]:
    """Agent tool definitions derived from the OpenAPI document (one tool per business operation)."""
    schemas = openapi.get("components", {}).get("schemas", {})
    tools = []
    for path, ops in openapi["paths"].items():
        if not path.startswith(base_path):
            continue
        for method, op in ops.items():
            props: dict[str, Any] = {}
            required: list[str] = []
            for p in op.get("parameters", []):
                props[p["name"]] = {**p.get("schema", {}), "description": p.get("description", p["in"] + " parameter")}
                if p.get("required"):
                    required.append(p["name"])
            body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            if body and "$ref" in body:
                b = schemas[body["$ref"].split("/")[-1]]
                props.update(b.get("properties", {}))
                required += b.get("required", [])
            tools.append({
                "name": op["operationId"],
                "description": f"{op.get('summary', '')}. {op.get('description', '')}".strip(),
                "http": {"method": method.upper(), "path": path},
                "parameters": {"type": "object", "properties": props, "required": required},
            })
    return tools
