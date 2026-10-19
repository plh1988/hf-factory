"""Shared pieces of the control-plane routers: dependencies, the tenant-API mount, and rejection responses."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.routing import Mount

from factory.control.errors import JobRejected
from factory.control.registry import Registry


def get_registry(request: Request) -> Registry:
    """FastAPI dependency: the Registry of this control-plane app."""
    return request.app.state.registry


class TenantApiMount:
    """Serves the deployed tenant API at / on the control-plane app (local profiles: one server serves both).

    The mount is appended after the /factory routes, so those always match first.
    """

    NAME = "tenant-api"

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def mount(self, tenant_app: FastAPI) -> None:
        self.unmount()
        self.app.router.routes.append(Mount("/", app=tenant_app, name=self.NAME))

    def unmount(self) -> None:
        self.app.router.routes[:] = [r for r in self.app.router.routes if not (isinstance(r, Mount) and r.name == self.NAME)]


def get_tenant_api(request: Request) -> TenantApiMount:
    """FastAPI dependency: the tenant-API mount of this control-plane app."""
    return request.app.state.tenant_api


def rejected(exc: JobRejected) -> JSONResponse:
    """JSON error body for a rejected payload or request."""
    return JSONResponse({"error": exc.code, "message": exc.message, "details": exc.details}, status_code=exc.status)


async def rejected_handler(_: Request, exc: JobRejected) -> JSONResponse:
    """Exception handler: handlers raise JobRejected instead of building error responses themselves."""
    return rejected(exc)
