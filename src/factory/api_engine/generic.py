"""API for use-case capability packs: a router, response models and contract tests built from the pack.

Endpoints are member-scoped reads (phase 1): GET <base>/members/{member_id}/<resource>[/{item}]. Each one checks the
member and caregiver scope exactly like the built-in member endpoints (member_access: MEMBER_NOT_FOUND,
CAREGIVER_NOT_VERIFIED, CONSENT_EXPIRED, SCOPE_NOT_AUTHORIZED), reads through Repository (validated identifiers,
bound values) and returns typed JSON. register() adds the use case to the endpoint catalog and contract-test
suites, so s8 treats it like a built-in one.
"""

from __future__ import annotations

import inspect
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field, create_model

from factory.api_engine import contract_tests as CT
from factory.api_engine import endpoints as EP
from factory.api_engine.errors import ErrorCode, error
from factory.api_engine.member_routes import ON_BEHALF, as_of, member_access
from factory.api_engine.repository import Repository
from factory.api_engine.routes import repo
from factory.api_engine.security import require_client
from factory.data_engine.packs.model import EndpointSpec, EntitySpec, UseCasePack

PY_TYPES = {"string": str, "bool": bool, "int": int, "float": float, "date": date, "money": Decimal}


def _models(code: str, ep: EndpointSpec, spec: EntitySpec | None) -> tuple[type[BaseModel], type[BaseModel]]:
    """Item and response models: typed from the entity pack when the table is a pack entity, else Any."""
    types = {c.name: PY_TYPES[c.type] for c in spec.columns} if spec else {}
    item = create_model(f"{code}_{ep.operation_id}_item",
                        **{f.name: (types.get(f.column, Any) | None, None) for f in ep.fields})
    if ep.item is not None:
        resp = create_model(f"{code}_{ep.operation_id}", member_id=(str, ...), item=(item, ...),
                            is_synthetic=(bool, True))
    else:
        summary = create_model(f"{code}_{ep.operation_id}_summary",
                               **{a.name: (int | None if a.fn == "count" else Any, None) for a in ep.aggregates})
        resp = create_model(f"{code}_{ep.operation_id}", member_id=(str, ...), count=(int, ...),
                            items=(list[item], Field(default_factory=list)), summary=(summary | None, None),
                            is_synthetic=(bool, True))
    return item, resp


def _handler(code: str, ep: EndpointSpec, spec: EntitySpec | None, resp_model: type[BaseModel]):
    columns = [f.column for f in ep.fields]
    order = [(o.column, o.desc) for o in ep.order_by] + ([(spec.primary_key, False)] if spec else [])

    def handler(request: Request, **kw: Any) -> Any:
        r: Repository = kw["r"]
        m = member_access(r, kw["member_id"], kw.get("on_behalf_of"), code, as_of(request))
        filters = {}
        for qp in ep.query_params:
            value = kw.get(qp.name)
            if value is None:
                continue
            if qp.values is not None and value.upper() not in {v.upper() for v in qp.values}:
                raise error(ErrorCode.INVALID_PARAMETER, f"{qp.name} must be one of {', '.join(qp.values)}.")
            filters[qp.column] = value
        item = (ep.item.column, kw[ep.item.param]) if ep.item else None
        rows = r.member_rows(ep.entity, ep.member_column, m["member_id"], columns, filters, item, order,
                             None if ep.item else kw["limit"])
        out = [{f.name: row[f.column] for f in ep.fields} for row in rows]
        if ep.item is not None:
            if not out:
                raise error(ErrorCode.RESOURCE_NOT_FOUND, f"No {ep.item.param} {kw[ep.item.param]} for member {kw['member_id']}.")
            return resp_model(member_id=m["member_number"], item=out[0])
        summary = None
        if ep.aggregates:
            summary = r.member_aggregates(ep.entity, ep.member_column, m["member_id"], filters,
                                          [(a.name, a.fn, a.column, a.where) for a in ep.aggregates])
        return resp_model(member_id=m["member_number"], count=len(out), items=out, summary=summary)

    params = [inspect.Parameter("request", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Request),
              inspect.Parameter("member_id", inspect.Parameter.KEYWORD_ONLY, annotation=str,
                                default=Path(..., description="MBR-... member number (or member_id)"))]
    if ep.item:
        params.append(inspect.Parameter(ep.item.param, inspect.Parameter.KEYWORD_ONLY, annotation=str, default=Path(...)))
    for qp in ep.query_params:
        desc = qp.description or f"filter on {qp.column}" + (f": {', '.join(qp.values)}" if qp.values else "")
        params.append(inspect.Parameter(qp.name, inspect.Parameter.KEYWORD_ONLY, annotation=str | None,
                                        default=Query(None, description=desc)))
    if not ep.item:
        params.append(inspect.Parameter("limit", inspect.Parameter.KEYWORD_ONLY, annotation=int,
                                        default=Query(ep.limit.default, ge=1, le=ep.limit.max)))
    params += [inspect.Parameter("on_behalf_of", inspect.Parameter.KEYWORD_ONLY, annotation=str | None, default=ON_BEHALF),
               inspect.Parameter("r", inspect.Parameter.KEYWORD_ONLY, annotation=Repository, default=Depends(repo)),
               inspect.Parameter("client", inspect.Parameter.KEYWORD_ONLY, annotation=str, default=Depends(require_client))]
    handler.__signature__ = inspect.Signature(params, return_annotation=resp_model)
    handler.__name__ = ep.operation_id
    return handler


def build_router(pack: UseCasePack, entities: dict[str, EntitySpec]) -> APIRouter:
    """Router for the pack's endpoints (mounted under the s8 base path like the built-in routers)."""
    uc = pack.use_case
    router = APIRouter(tags=[f"{uc.code} {uc.name}"])
    for ep in uc.endpoints:
        spec = entities.get(ep.entity)
        _, resp = _models(uc.code, ep, spec)
        router.add_api_route(ep.path, _handler(uc.code, ep, spec, resp), methods=[ep.method], response_model=resp,
                             operation_id=ep.operation_id, summary=ep.summary, description=ep.description or ep.summary)
    return router


# ------------------------------------------------------------------ contract tests (four per endpoint)
def suite(pack: UseCasePack):
    uc = pack.use_case

    def run(t: CT.Suite) -> None:
        for ep in uc.endpoints:
            _endpoint_tests(t, ep)

    return run


def _render(path: str, member: str, item: str | None = None) -> str:
    out = path.replace("{member_id}", member)
    return out.rsplit("/", 1)[0] + f"/{item}" if item is not None else out


def _endpoint_tests(t: CT.Suite, ep: EndpointSpec) -> None:
    name = f"{ep.method} {t.base}{ep.path}"
    item_col = ep.item.column if ep.item else ep.member_column
    sample = t.one(f"SELECT m.member_number, x.{item_col} AS item FROM {ep.entity} x "
                   f"JOIN member_demographics m ON m.member_id = x.{ep.member_column} ORDER BY m.member_number, x.{item_col}")
    fields = [f.name for f in ep.fields]
    if sample is None:  # no rows generated: the happy case becomes "a member without rows gets nothing"
        any_member = t.one("SELECT member_number FROM member_demographics ORDER BY member_number")
        mid = any_member["member_number"] if any_member else "MBR-0000001"
        if ep.item:
            t.add(name, "no rows: item lookup is 404", t.get(_render(ep.path, mid, "none")), CT._error(ErrorCode.RESOURCE_NOT_FOUND))
        else:
            t.add(name, "no rows: empty list", t.get(_render(ep.path, mid)), lambda r: r.status_code == 200 and r.json()["count"] == 0)
    else:
        mid = sample["member_number"]
        if ep.item:
            t.add(name, "returns the item with its fields", t.get(_render(ep.path, mid, str(sample["item"]))),
                  lambda r: r.status_code == 200 and set(fields) <= set(r.json()["item"]))
        else:
            t.add(name, "returns the member's rows with their fields", t.get(_render(ep.path, mid)),
                  lambda r: r.status_code == 200 and r.json()["count"] >= 1 and set(fields) <= set(r.json()["items"][0]))
    t.add(name, "unknown member is 404", t.get(_render(ep.path, "MBR-9999999", "none" if ep.item else None)),
          CT._error(ErrorCode.MEMBER_NOT_FOUND))
    t.add(name, "no token is 401", t.get(_render(ep.path, mid, "none" if ep.item else None), auth=False), CT._status(401))
    if ep.item:
        t.add(name, "unknown item is 404", t.get(_render(ep.path, mid, "UNKNOWN-ITEM")), CT._error(ErrorCode.RESOURCE_NOT_FOUND))
        return
    qp = next((q for q in ep.query_params if q.values), None)
    if qp is not None:
        out_name = next((f.name for f in ep.fields if f.column == qp.column), None)
        value = qp.values[0]
        t.add(name, f"filter {qp.name}={value} keeps only matching rows", t.get(_render(ep.path, mid) + f"?{qp.name}={value}"),
              lambda r: r.status_code == 200 and (out_name is None or all(str(i[out_name]).upper() == value.upper() for i in r.json()["items"])))
    else:
        t.add(name, "limit=1 returns at most one row", t.get(_render(ep.path, mid) + "?limit=1"),
              lambda r: r.status_code == 200 and len(r.json()["items"]) <= 1)


# ------------------------------------------------------------------ registration
def register(pack: UseCasePack, entities: dict[str, EntitySpec]) -> None:
    """Make the use case available to s8: router, endpoint catalog entry and contract-test suite."""
    code = pack.use_case.code
    router = build_router(pack, entities)
    EP.ENDPOINTS[code] = router
    EP.ENDPOINT_CATALOG[code] = sorted((m, r.path) for r in router.routes for m in r.methods)
    CT.SUITES[code] = suite(pack)


def unregister(code: str) -> None:
    EP.ENDPOINTS.pop(code, None)
    EP.ENDPOINT_CATALOG.pop(code, None)
    CT.SUITES.pop(code, None)
