"""Endpoint catalog: use case -> router. The s8 payload's use_cases decide which routers are mounted."""

from __future__ import annotations

from fastapi import APIRouter

from factory.api_engine.member_routes import mas, mbn, mci, mpc, mps
from factory.api_engine.provider_routes import pap, ppi
from factory.api_engine.routes import common, pas, pcs, pev

ENDPOINTS: dict[str, APIRouter] = {
    "PCS": pcs, "PAS": pas, "PEV": pev,
    "MBN": mbn, "MCI": mci, "MPS": mps, "MPC": mpc, "MAS": mas,
    "PAP": pap, "PPI": ppi,
}

# (method, path under base_path) per use case; COMMON is mounted for every tenant.
ENDPOINT_CATALOG: dict[str, list[tuple[str, str]]] = {
    uc: sorted((m, r.path) for r in router.routes for m in r.methods)
    for uc, router in {**ENDPOINTS, "COMMON": common}.items()
}
