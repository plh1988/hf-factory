"""Member-facing endpoints: MBN benefits, MCI claims inquiry, MPS provider search, MPC PCP change, MAS scheduling.

The caller's identity is verified first (POST /v1/identity/verify). A caregiver calling for a member passes
on_behalf_of=<relationship_id>; the relationship must be verified, unexpired and cover the use case (rule R-06).
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from factory.api_engine.repository import Repository
from factory.api_engine.routes import Synthetic, error, repo
from factory.api_engine.security import require_client
from factory.data_engine import catalog


def as_of(request: Request) -> date:
    return request.app.state.as_of


def member_access(r: Repository, member_ref: str, on_behalf_of: str | None, use_case: str, today: date) -> dict:
    """Find the member; enforce caregiver scope (R-06, C-01 minimum necessary)."""
    m = r.member(member_ref)
    if m is None:
        raise error(404, "MEMBER_NOT_FOUND", f"No member {member_ref}.")
    if on_behalf_of:
        if not r.exists("family_relationship"):
            raise error(403, "SCOPE_NOT_AUTHORIZED", "No caregiver authorizations exist for this tenant.")
        cg = r.caregiver(m["member_id"], on_behalf_of)
        needed = catalog.CAREGIVER_SCOPE.get(use_case, use_case)
        if cg is None or not cg["verified"]:
            raise error(403, "CAREGIVER_NOT_VERIFIED", "No verified caregiver authorization on file for this member.")
        if cg["consent_expiry_dt"] and cg["consent_expiry_dt"] < today:
            raise error(403, "CONSENT_EXPIRED", f"The caregiver authorization expired on {cg['consent_expiry_dt']}. "
                        "The member can renew it.")
        scope = cg["authorization_scope"] or []
        if "ALL" not in scope and needed not in scope:
            raise error(403, "SCOPE_NOT_AUTHORIZED", f"The caregiver authorization covers {', '.join(scope) or 'nothing'}, "
                        f"not {needed}. The member can add it, or join the call.")
    return m


def current_enrollment(r: Repository, member_id: str, today: date) -> dict | None:
    return r.enrollment_on(member_id, today) or r.latest_enrollment(member_id)


def money0(x: Any) -> Decimal:
    return Decimal(str(x or 0)).quantize(Decimal("0.01"))


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


ON_BEHALF = Query(None, description="relationship_id of a verified caregiver calling for the member")

# =============================================================== MBN - benefits navigation
mbn = APIRouter(tags=["MBN Benefits navigation"])


class CostShare(BaseModel):
    deductible: Decimal
    deductible_met: Decimal
    deductible_remaining: Decimal
    oop_max: Decimal
    oop_met: Decimal
    oop_remaining: Decimal
    copay_pcp: Decimal
    copay_specialist: Decimal
    coinsurance_pct: int


class Benefits(Synthetic):
    member_id: str
    plan: dict[str, Any]
    coverage: dict[str, Any]
    cost_share: CostShare
    specialist_visit_estimate: str
    allowances: dict[str, Any]
    transportation: dict[str, Any]
    prior_authorization_required_codes: list[str]
    citations: list[dict[str, Any]]


@mbn.get("/members/{member_id}/benefits", response_model=Benefits, operation_id="get_member_benefits",
         summary="Plan benefits, cost share and allowances for a member",
         description="Plain-language cost share, deductible status, allowances and transport benefit, with plan-document citations.")
def get_member_benefits(member_id: str, request: Request, on_behalf_of: str | None = ON_BEHALF,
                        r: Repository = Depends(repo), _: str = Depends(require_client)) -> Benefits:
    today = as_of(request)
    m = member_access(r, member_id, on_behalf_of, "MBN", today)
    e = current_enrollment(r, m["member_id"], today)
    if e is None:
        raise error(404, "NO_COVERAGE", "No enrollment on file.")
    p = r.plan(e["plan_id"])
    acc = {a["network"]: a for a in r.accumulators(m["member_id"], today.year)}.get("IN", {})
    ded, ded_met = money0(p["deductible"]), money0(acc.get("deductible_met"))
    oop, oop_met = money0(p["oop_max"]), money0(acc.get("oop_met"))
    allowances = json.loads(p["allowances"] or "{}")
    rides = allowances.get("transport_rides_annual", 0)
    transport_need = any(s["domain"] == "TRANSPORT" and s["positive"] for s in r.sdoh(m["member_id"]))
    remaining = max(ded - ded_met, Decimal("0.00"))
    estimate = (f"After the deductible ({remaining} USD remaining), a specialist visit costs {money0(p['copay_specialist'])} USD."
                if remaining > 0 else f"A specialist visit costs {money0(p['copay_specialist'])} USD.")
    return Benefits(
        member_id=m["member_number"],
        plan={"plan_code": p["plan_code"], "plan_name": p["plan_name"], "lob": p["lob"], "network_type": p["network_type"]},
        coverage={"status": e["status"], "effective_dt": e["effective_dt"], "term_dt": e["term_dt"]},
        cost_share=CostShare(deductible=ded, deductible_met=ded_met, deductible_remaining=remaining, oop_max=oop, oop_met=oop_met,
                             oop_remaining=max(oop - oop_met, Decimal("0.00")), copay_pcp=money0(p["copay_pcp"]),
                             copay_specialist=money0(p["copay_specialist"]), coinsurance_pct=p["coinsurance_pct"]),
        specialist_visit_estimate=estimate,
        allowances={k: v for k, v in allowances.items() if k != "transport_rides_annual"},
        transportation={"covered": rides > 0, "rides_per_year": rides, "member_has_transport_need": transport_need,
                        "next_step": "Offer to book a ride for the next appointment." if rides and transport_need else None},
        prior_authorization_required_codes=p["pa_required_codes"] or [],
        citations=r.plan_documents(p["plan_id"]), scenario_id=m["scenario_id"],
    )


class Accumulators(Synthetic):
    member_id: str
    plan_year: int
    in_network: dict[str, Decimal]
    out_of_network: dict[str, Decimal]


@mbn.get("/members/{member_id}/accumulators", response_model=Accumulators, operation_id="get_member_accumulators",
         summary="Deductible and out-of-pocket progress", description="Met and remaining amounts, in and out of network, derived from paid claims.")
def get_member_accumulators(member_id: str, request: Request, plan_year: int | None = None, on_behalf_of: str | None = ON_BEHALF,
                            r: Repository = Depends(repo), _: str = Depends(require_client)) -> Accumulators:
    today = as_of(request)
    m = member_access(r, member_id, on_behalf_of, "MBN", today)
    e = current_enrollment(r, m["member_id"], today)
    if e is None:
        raise error(404, "NO_COVERAGE", "No enrollment on file.")
    p = r.plan(e["plan_id"])
    year = plan_year or today.year
    acc = {a["network"]: a for a in r.accumulators(m["member_id"], year)}

    def block(net: str) -> dict[str, Decimal]:
        a = acc.get(net, {})
        ded, oop = money0(p["deductible"]), money0(p["oop_max"])
        met_d, met_o = money0(a.get("deductible_met")), money0(a.get("oop_met"))
        return {"deductible": ded, "deductible_met": met_d, "deductible_remaining": max(ded - met_d, Decimal("0.00")),
                "oop_max": oop, "oop_met": met_o, "oop_remaining": max(oop - met_o, Decimal("0.00"))}

    return Accumulators(member_id=m["member_number"], plan_year=year, in_network=block("IN"), out_of_network=block("OON"),
                        scenario_id=m["scenario_id"])


# =============================================================== MCI - claims inquiry
mci = APIRouter(tags=["MCI Claims inquiry"])


class MemberClaimSummary(BaseModel):
    claim_id: str
    claim_type: str
    status: str
    service_from_dt: date
    total_billed: Decimal
    total_allowed: Decimal
    total_paid: Decimal
    member_resp: Decimal
    denial: dict[str, Any] | None = None
    appealable: bool = False
    version: str
    scenario_id: str | None = None


class MemberClaims(Synthetic):
    member_id: str
    claims: list[MemberClaimSummary]


def _denial_view(d: dict | None) -> dict | None:
    if not d:
        return None
    return {"category": d["denial_category"], "carc": d["carc"], "reason_text": d["reason_text"], "liable_party": d["liable_party"],
            "member_owes": "Nothing for the denied amount: group code CO means the provider is responsible."
            if d["carc"].startswith("CO") else "See your EOB."}


@mci.get("/members/{member_id}/claims", response_model=MemberClaims, operation_id="list_member_claims",
         summary="A member's claims", description="Status, amounts, member responsibility and denial reasons, newest first.")
def list_member_claims(member_id: str, request: Request, status: str | None = Query(None, description="e.g. denied, paid, pended"),
                       limit: int = Query(20, ge=1, le=100), on_behalf_of: str | None = ON_BEHALF,
                       r: Repository = Depends(repo), _: str = Depends(require_client)) -> MemberClaims:
    m = member_access(r, member_id, on_behalf_of, "MCI", as_of(request))
    where, params = "h.member_id = ?", [m["member_id"]]
    if status:
        where += " AND h.status = ?"
        params.append(status.upper())
    rows = r.rows(f"SELECT h.* FROM claim_header h WHERE {where} ORDER BY h.service_from_dt DESC, h.claim_number LIMIT {limit}", params)
    out = []
    for c in rows:
        d = r.claim_detail(c["claim_id"])["denial"] if c["status"] in ("DENIED", "REPROCESSED") and r.exists("claim_denial") else None
        version = "replacement" if c["claim_frequency_code"] == "7" else ("superseded" if c["status"] in ("ADJUSTED", "REPROCESSED") else "current")
        out.append(MemberClaimSummary(claim_id=c["claim_number"], claim_type=c["claim_type"], status=c["status"],
                                      service_from_dt=c["service_from_dt"], total_billed=c["total_billed"], total_allowed=c["total_allowed"],
                                      total_paid=c["total_paid"], member_resp=c["member_resp"], denial=_denial_view(d),
                                      appealable=bool(d and d["appealable"]), version=version, scenario_id=c["scenario_id"]))
    return MemberClaims(member_id=m["member_number"], claims=out, scenario_id=m["scenario_id"])


class ClaimDetail(Synthetic):
    claim_id: str
    claim_type: str
    status: str
    service_from_dt: date
    service_to_dt: date
    totals: dict[str, Decimal]
    member_owes_explained: str
    lines: list[dict[str, Any]]
    denial: dict[str, Any] | None = None
    pend: dict[str, Any] | None = None
    payment: dict[str, Any] | None = None
    versions: dict[str, Any]


@mci.get("/claims/{claim_id}", response_model=ClaimDetail, operation_id="get_claim_detail",
         summary="One claim with EOB math and versions (member-facing)",
         description="Requires member_id: a member sees only their own claims. Shows the EOB math line by line and any original/replacement versions.")
def get_claim_detail(claim_id: str, request: Request, member_id: str = Query(..., description="MBR-... of the member asking"),
                     on_behalf_of: str | None = ON_BEHALF, r: Repository = Depends(repo), _: str = Depends(require_client)) -> ClaimDetail:
    m = member_access(r, member_id, on_behalf_of, "MCI", as_of(request))
    c = r.claim(claim_id)
    if c is None or c["member_id"] != m["member_id"]:
        raise error(404, "CLAIM_NOT_FOUND", f"No claim {claim_id} for member {member_id}.")
    d = r.claim_detail(c["claim_id"])
    lines = r.rows("SELECT line_no, cpt_hcpcs, units, billed, allowed, deductible, copay, coinsurance, paid, line_status "
                   "FROM claim_line WHERE claim_id = ? ORDER BY line_no", [c["claim_id"]])
    ded = sum(money0(x["deductible"]) for x in lines)
    cop = sum(money0(x["copay"]) for x in lines)
    coi = sum(money0(x["coinsurance"]) for x in lines)
    versions: dict[str, Any] = {"is_replacement": c["claim_frequency_code"] == "7", "original_claim_id": None, "replaced_by": None,
                                "reprocessing": None}
    if c["original_claim_id"]:
        o = r.one("SELECT claim_number FROM claim_header WHERE claim_id = ?", [c["original_claim_id"]])
        versions["original_claim_id"] = o and o["claim_number"]
    if r.exists("reprocessing_history"):
        h = r.one("SELECT r.*, n.claim_number AS new_number FROM reprocessing_history r JOIN claim_header n ON n.claim_id = r.new_claim_id "
                  "WHERE r.claim_id = ? OR r.new_claim_id = ?", [c["claim_id"], c["claim_id"]])
        if h:
            versions["replaced_by"] = h["new_number"] if h["claim_id"] == c["claim_id"] else None
            versions["reprocessing"] = {"trigger": h["trigger"], "reprocess_dt": h["reprocess_dt"], "paid_before": h["paid_before"],
                                        "paid_after": h["paid_after"]}
    if c["status"] in ("ADJUSTED", "REPROCESSED"):
        note = f" This version was replaced by {versions['replaced_by']}; the replacement is the current one."
    else:
        note = ""
    return ClaimDetail(
        claim_id=c["claim_number"], claim_type=c["claim_type"], status=c["status"], service_from_dt=c["service_from_dt"],
        service_to_dt=c["service_to_dt"],
        totals={"billed": c["total_billed"], "allowed": c["total_allowed"], "paid": c["total_paid"], "member_resp": c["member_resp"]},
        member_owes_explained=f"You owe {money0(c['member_resp'])} USD: deductible {ded} + copay {cop} + coinsurance {coi}. "
                              f"The plan paid {money0(c['total_paid'])} USD of the {money0(c['total_allowed'])} USD allowed.{note}",
        lines=lines, denial=_denial_view(d["denial"]), pend=d["pend"], payment=d["payment"], versions=versions,
        scenario_id=c["scenario_id"],
    )


# =============================================================== MPS - provider search
mps = APIRouter(tags=["MPS Provider search"])
SPECIALTY_WORDS = {"pcp": "PCP", "primary": "PCP", "family": "Family Medicine", "internal": "Internal Medicine", "pediatric": "Pediatrics",
                   "cardio": "Cardiovascular Disease", "ortho": "Orthopaedic Surgery", "radiolog": "Diagnostic Radiology",
                   "physical therap": "Physical Therapist", "hospital": "General Acute Care Hospital", "snf": "Skilled Nursing Facility",
                   "nursing": "Skilled Nursing Facility", "home health": "Home Health", "pharmac": "Pharmacy"}
ACCESS_KEY = {"PCP": "PCP", "Family Medicine": "PCP", "Internal Medicine": "PCP", "Pediatrics": "PEDIATRICS",
              "Cardiovascular Disease": "CARDIOLOGY", "Orthopaedic Surgery": "ORTHOPAEDICS", "Diagnostic Radiology": "RADIOLOGY",
              "Physical Therapist": "PHYSICAL_THERAPY", "General Acute Care Hospital": "HOSPITAL", "Skilled Nursing Facility": "SNF",
              "Home Health": "HOME_HEALTH", "Pharmacy": "PHARMACY"}
LANG = {"es": "ES", "spanish": "ES", "en": "EN", "english": "EN", "zh": "ZH", "chinese": "ZH"}


def _specialty(text: str) -> str:
    t = text.lower()
    for word, name in SPECIALTY_WORDS.items():
        if word in t:
            return name
    raise error(422, "UNKNOWN_SPECIALTY", f"Unknown specialty {text!r}. Try: primary care, pediatrics, cardiology, orthopedics, radiology.")


def _nearest_geo(lat: float, lon: float) -> str:
    return min(catalog.PLACES, key=lambda pl: haversine_mi(lat, lon, pl[3], pl[4]))[5]


class ProviderHit(BaseModel):
    npi: str
    name: str
    specialty: str
    distance_mi: float
    address: str
    languages: list[str]
    accepting_new_patients: bool
    accessibility: list[str]
    telehealth: bool
    rating: float | None
    next_available_dt: datetime | None
    directory_verified: bool


class ProviderSearch(Synthetic):
    query: dict[str, Any]
    results_within_radius: int
    results: list[ProviderHit]
    access_standard: dict[str, Any] | None
    access_gap_logged: bool
    widened_search: dict[str, Any] | None
    telehealth: dict[str, Any]


@mps.get("/providers/search", response_model=ProviderSearch, operation_id="search_providers",
         summary="Search in-network providers",
         description="By specialty, distance, language and accessibility. Only PARTICIPATING providers are returned. "
                     "When nothing is within the radius, the search widens 1.5x, logs an access gap and offers telehealth.")
def search_providers(request: Request, specialty: str = Query(..., min_length=2), member_id: str | None = None,
                     lat: float | None = Query(None, ge=-90, le=90), lon: float | None = Query(None, ge=-180, le=180),
                     radius_mi: float | None = Query(None, gt=0, le=200), language: str | None = None,
                     accessibility: Literal["WHEELCHAIR", "ASL", "TTY"] | None = None, limit: int = Query(10, ge=1, le=50),
                     r: Repository = Depends(repo), _: str = Depends(require_client)) -> ProviderSearch:
    today = as_of(request)
    spec = _specialty(specialty)
    plan_id, geo, scen = None, None, None
    if member_id:
        m = r.member(member_id)
        if m is None:
            raise error(404, "MEMBER_NOT_FOUND", f"No member {member_id}.")
        e = current_enrollment(r, m["member_id"], today)
        plan_id = e and e["plan_id"]
        lat = m["lat"] if lat is None else lat
        lon = m["lon"] if lon is None else lon
        geo, scen = m["geo_type"], m["scenario_id"]
    if lat is None or lon is None:
        raise error(422, "LOCATION_REQUIRED", "Send lat and lon, or member_id.")
    geo = geo or _nearest_geo(lat, lon)
    std = None
    if r.exists("access_standard"):
        std = r.one("SELECT max_distance_miles, max_wait_days FROM access_standard WHERE provider_type_or_specialty = ? AND geo_type = ? "
                    + ("AND plan_id = ? " if plan_id else "") + "ORDER BY plan_id LIMIT 1",
                    [ACCESS_KEY.get(spec, "PCP"), geo] + ([plan_id] if plan_id else []))
    radius = radius_mi or (std["max_distance_miles"] if std else 25)
    spec_sql = "p.provider_type = 'PCP'" if spec == "PCP" else "p.specialty = ?"
    params: list[Any] = [] if spec == "PCP" else [spec]
    plan_sql = ""
    if plan_id:
        plan_sql, params = " AND n.plan_id = ?", params + [plan_id]
    rows = r.rows(
        "SELECT DISTINCT p.npi, p.first_name, p.last_name, p.org_name, p.specialty, p.languages, l.location_id, l.address, l.city, l.state, "
        "l.lat, l.lon, l.accepting_new_patients, l.accessibility, l.telehealth, l.last_verified_dt "
        "FROM provider p JOIN network_participation n ON n.npi = p.npi JOIN practice_location l ON l.npi = p.npi "
        f"WHERE p.status = 'ACTIVE' AND n.status = 'PARTICIPATING' AND n.term_dt IS NULL AND {spec_sql}{plan_sql}", params)
    rating = {x["npi"]: x["quality_score"] for x in r.rows("SELECT npi, quality_score, rating_count FROM provider_rating WHERE rating_count >= 20")} \
        if r.exists("provider_rating") else {}
    nxt = {x["location_id"]: x["nxt"] for x in r.rows("SELECT location_id, min(next_available_dt) AS nxt FROM availability_schedule GROUP BY 1")} \
        if r.exists("availability_schedule") else {}
    lang = LANG.get((language or "").lower(), (language or "").upper()) if language else None
    hits = []
    for x in rows:
        if lang and lang not in (x["languages"] or []):
            continue
        if accessibility and accessibility not in (x["accessibility"] or []):
            continue
        name = x["org_name"] or f"Dr. {x['first_name'][0]}. {x['last_name']}"
        hits.append(ProviderHit(npi=x["npi"], name=name, specialty=x["specialty"], distance_mi=round(haversine_mi(lat, lon, x["lat"], x["lon"]), 1),
                                address=f"{x['address']}, {x['city']}, {x['state']}", languages=x["languages"] or [],
                                accepting_new_patients=x["accepting_new_patients"], accessibility=x["accessibility"] or [],
                                telehealth=x["telehealth"], rating=rating.get(x["npi"]), next_available_dt=nxt.get(x["location_id"]),
                                directory_verified=(today - x["last_verified_dt"]).days <= 90))
    hits.sort(key=lambda h: (h.distance_mi, -(h.rating or 0)))
    within = [h for h in hits if h.distance_mi <= radius]
    widened = None
    gap = False
    if not within:
        gap = True
        wider = [h for h in hits if h.distance_mi <= radius * 1.5]
        widened = {"radius_mi": round(radius * 1.5, 1), "results": [h.model_dump(mode="json") for h in wider[:limit]]}
    tele = [h for h in hits if h.telehealth]
    return ProviderSearch(
        query={"specialty": spec, "language": lang, "accessibility": accessibility, "lat": lat, "lon": lon, "radius_mi": radius, "plan_filtered": bool(plan_id)},
        results_within_radius=len(within), results=within[:limit],
        access_standard={"geo_type": geo, "max_distance_miles": std["max_distance_miles"], "max_wait_days": std["max_wait_days"]} if std else None,
        access_gap_logged=gap, widened_search=widened,
        telehealth={"available": bool(tele), "count": len({h.npi for h in tele})}, scenario_id=scen,
    )


# =============================================================== MPC - PCP change
mpc = APIRouter(tags=["MPC PCP change"])


class PcpChangeRequest(BaseModel):
    npi: str = Field(..., min_length=10, max_length=10, description="NPI of the new primary care provider")
    reason: Literal["PREFERENCE", "MOVED", "PROVIDER_TERMED", "ACCESS"] = "PREFERENCE"


class PcpChange(Synthetic):
    change_id: str
    member_id: str
    previous_pcp_npi: str | None
    new_pcp_npi: str
    new_pcp_name: str
    effective_dt: date
    status: str
    confirmation: str


@mpc.put("/members/{member_id}/pcp", response_model=PcpChange, operation_id="change_member_pcp",
         summary="Change a member's PCP",
         description="Validates plan participation, open panel and access standard (R-05), then schedules the change for the first of next month.")
def change_member_pcp(member_id: str, body: PcpChangeRequest, request: Request, on_behalf_of: str | None = ON_BEHALF,
                      dry_run: bool = Query(False, description="Validate only; nothing is written"), r: Repository = Depends(repo), _: str = Depends(require_client)) -> PcpChange:
    today = as_of(request)
    m = member_access(r, member_id, on_behalf_of, "MPC", today)
    e = r.enrollment_on(m["member_id"], today)
    if e is None or e["status"] not in ("ACTIVE", "PENDING"):
        raise error(409, "NO_ACTIVE_COVERAGE", "The member has no active coverage today.")
    prov = r.one("SELECT p.npi, p.provider_type, p.first_name, p.last_name, p.status, n.status AS part, n.panel_status "
                 "FROM provider p LEFT JOIN network_participation n ON n.npi = p.npi AND n.plan_id = ? WHERE p.npi = ?", [e["plan_id"], body.npi])
    if prov is None:
        raise error(404, "PROVIDER_NOT_FOUND", f"No provider {body.npi}.")
    if prov["provider_type"] != "PCP":
        raise error(409, "NOT_A_PCP", "This provider is not a primary care provider.")
    if prov["status"] != "ACTIVE" or prov["part"] != "PARTICIPATING":
        raise error(409, "NOT_PARTICIPATING", "This provider is not participating on the member's plan.")
    if prov["panel_status"] == "CLOSED":
        raise error(409, "PANEL_CLOSED", "This provider's panel is closed to new patients.")
    locs = r.rows("SELECT lat, lon FROM practice_location WHERE npi = ?", [body.npi])
    std = r.one("SELECT max_distance_miles FROM access_standard WHERE plan_id = ? AND provider_type_or_specialty = 'PCP' AND geo_type = ?",
                [e["plan_id"], m["geo_type"]]) if r.exists("access_standard") else None
    if std and locs and min(haversine_mi(m["lat"], m["lon"], x["lat"], x["lon"]) for x in locs) > std["max_distance_miles"]:
        raise error(409, "OUTSIDE_ACCESS_STANDARD", f"No office of this PCP is within {std['max_distance_miles']} miles of the member "
                    "(e.g. after a move). Search in-area PCPs with GET /v1/providers/search.")
    cur = r.one("SELECT * FROM pcp_assignment WHERE member_id = ? AND status = 'ACTIVE'", [m["member_id"]])
    if cur and cur["pcp_npi"] == body.npi:
        raise error(409, "ALREADY_PCP", "This provider is already the member's PCP.")
    eff = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    change_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pcp-change:{m['member_id']}:{body.npi}:{eff}"))
    if cur and not dry_run:
        r.insert_row("pcp_assignment", "assignment_id", {**cur, "term_dt": eff - timedelta(days=1)})
    if not dry_run:
      r.insert_row("pcp_assignment", "assignment_id", {
        "assignment_id": change_id, "member_id": m["member_id"], "pcp_npi": body.npi, "plan_id": e["plan_id"], "effective_dt": eff,
        "term_dt": None, "assignment_type": "MEMBER_SELECTED", "status": "PENDING_CHANGE", "change_reason": body.reason,
        "is_synthetic": True, "tenant_id": m["tenant_id"], "scenario_id": m["scenario_id"]})
    return PcpChange(change_id=change_id, member_id=m["member_number"], previous_pcp_npi=cur and cur["pcp_npi"], new_pcp_npi=body.npi,
                     new_pcp_name=f"Dr. {prov['first_name']} {prov['last_name']}", effective_dt=eff,
                     status="VALIDATED" if dry_run else "SCHEDULED",
                     confirmation="Dry run: nothing was changed." if dry_run else "Confirmation sent to the member's preferred channel (simulated).",
                     scenario_id=m["scenario_id"])


# =============================================================== MAS - appointment scheduling
mas = APIRouter(tags=["MAS Appointment scheduling"])
DAY_INDEX = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _slots(sched: list[dict], count: int = 6) -> tuple[list[dict], bool]:
    """Next free slots from each schedule row's next_available_dt, skipping blackout dates. Returns (slots, stale_flag)."""
    out, stale = [], False
    for s in sched:
        start = s["next_available_dt"]
        blackout = set(s["blackout_dates"] or [])
        if str(start.date()) in blackout:
            stale = True  # the schedule offers a slot on a blackout day (MAS-04)
        day = start
        hours = (int(s["open_time"][:2]), int(s["close_time"][:2]))
        n = 0
        for _ in range(60):
            if day.weekday() == DAY_INDEX[s["day_of_week"]] and str(day.date()) not in blackout:
                t = max(day, day.replace(hour=hours[0], minute=0, second=0))
                while t.hour < hours[1] and n < 2:
                    out.append({"start": t, "location_id": s["location_id"], "slot_minutes": s["slot_minutes"],
                                "appointment_types": s["appointment_types"]})
                    t += timedelta(minutes=s["slot_minutes"] * 4)
                    n += 1
            if n >= 2:
                break
            day = (day + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    out.sort(key=lambda x: x["start"])
    return out[:count], stale


class Availability(Synthetic):
    npi: str
    provider_name: str
    slots: list[dict[str, Any]]
    first_available: datetime | None
    wait_standard_days: int | None
    meets_wait_standard: bool | None
    access_gap_logged: bool
    schedule_flag: str | None
    alternatives: list[dict[str, Any]]


@mas.get("/providers/{npi}/availability", response_model=Availability, operation_id="get_provider_availability",
         summary="Next available appointment slots",
         description="Slots skip blackout dates. Compared with the plan's wait standard; offers telehealth or other participating providers when it is not met.")
def get_provider_availability(npi: str, request: Request, member_id: str | None = None, visit_type: str | None = None,
                              r: Repository = Depends(repo), _: str = Depends(require_client)) -> Availability:
    today = as_of(request)
    p = r.one("SELECT npi, provider_type, specialty, first_name, last_name, org_name FROM provider WHERE npi = ?", [npi])
    if p is None:
        raise error(404, "PROVIDER_NOT_FOUND", f"No provider {npi}.")
    sched = r.rows("SELECT * FROM availability_schedule WHERE npi = ? ORDER BY location_id, day_of_week", [npi])
    if visit_type:
        sched = [s for s in sched if visit_type.upper() in (s["appointment_types"] or [])]
    slots, stale = _slots(sched)
    first = slots[0]["start"] if slots else None
    std_wait = None
    scen = None
    if member_id:
        m = r.member(member_id)
        if m is None:
            raise error(404, "MEMBER_NOT_FOUND", f"No member {member_id}.")
        scen = m["scenario_id"]
        e = current_enrollment(r, m["member_id"], today)
        if e and r.exists("access_standard"):
            key = ACCESS_KEY.get(p["specialty"], "PCP")
            row = r.one("SELECT max_wait_days FROM access_standard WHERE plan_id = ? AND provider_type_or_specialty = ? AND geo_type = ?",
                        [e["plan_id"], key, m["geo_type"]])
            std_wait = row and row["max_wait_days"]
    meets = None if not std_wait else bool(first and (first.date() - today).days <= std_wait)
    alternatives = []
    if meets is False or not slots:
        alternatives = r.rows(
            "SELECT s.npi, min(s.next_available_dt) AS next_available_dt, max(CAST(list_contains(s.appointment_types, 'TELEHEALTH') AS INTEGER)) AS telehealth "
            "FROM availability_schedule s JOIN provider p ON p.npi = s.npi WHERE p.specialty = ? AND s.npi <> ? "
            "GROUP BY 1 ORDER BY 2 LIMIT 3", [p["specialty"], npi])
    return Availability(npi=npi, provider_name=p["org_name"] or f"Dr. {p['first_name']} {p['last_name']}",
                        slots=[{**s, "start": s["start"]} for s in slots], first_available=first, wait_standard_days=std_wait,
                        meets_wait_standard=meets, access_gap_logged=meets is False,
                        schedule_flag="STALE_SCHEDULE_DATA: a listed slot falls on a blackout date; rechecked and skipped." if stale else None,
                        alternatives=alternatives, scenario_id=scen)


class AppointmentRequest(BaseModel):
    member_id: str
    npi: str = Field(..., min_length=10, max_length=10)
    location_id: str
    start: datetime
    visit_type: Literal["NEW_PATIENT", "FOLLOW_UP", "SICK", "WELLNESS", "TELEHEALTH"]


class Appointment(Synthetic):
    appointment_id: str
    status: str
    member_id: str
    npi: str
    location_id: str
    start: datetime
    visit_type: str
    care_gaps_to_address: list[str]
    transportation: str | None
    confirmation: str


@mas.post("/appointments", response_model=Appointment, operation_id="book_appointment", status_code=201,
          summary="Book an appointment",
          description="Only with a provider participating on the member's plan, on a real open slot. Notes open care gaps and transport benefit. No symptom triage.")
def book_appointment(body: AppointmentRequest, request: Request, on_behalf_of: str | None = ON_BEHALF,
                     dry_run: bool = Query(False, description="Validate only; nothing is written"), r: Repository = Depends(repo), _: str = Depends(require_client)) -> Appointment:
    today = as_of(request)
    m = member_access(r, body.member_id, on_behalf_of, "MAS", today)
    e = r.enrollment_on(m["member_id"], today)
    if e is None:
        raise error(409, "NO_ACTIVE_COVERAGE", "The member has no active coverage today.")
    part = r.one("SELECT status FROM network_participation WHERE npi = ? AND plan_id = ? AND term_dt IS NULL", [body.npi, e["plan_id"]])
    if not part or part["status"] != "PARTICIPATING":
        raise error(409, "NOT_PARTICIPATING", "This provider does not participate on the member's plan; the agent cannot book it.")
    sched = r.rows("SELECT * FROM availability_schedule WHERE npi = ? AND location_id = ?", [body.npi, body.location_id])
    slots, _ = _slots(sched, count=200)
    starts = {s["start"].replace(tzinfo=None) for s in slots}
    if body.start.replace(tzinfo=None) not in starts:
        raise error(409, "SLOT_UNAVAILABLE", "That time is not an open slot. Get open slots from GET /v1/providers/{npi}/availability.")
    appt_id = "APT-" + hashlib.sha256(f"{m['member_id']}|{body.npi}|{body.start.isoformat()}".encode()).hexdigest()[:10].upper()
    blobs = request.app.state.stores.blobs
    if f"appointments/{appt_id}.json" in blobs.list("appointments/"):
        raise error(409, "ALREADY_BOOKED", f"Appointment {appt_id} already exists.")
    gaps = [g["measure"] for g in r.open_gaps(m["member_id"])]
    allowances = json.loads((r.plan(e["plan_id"]) or {}).get("allowances") or "{}")
    transport_need = any(s["domain"] == "TRANSPORT" and s["positive"] for s in r.sdoh(m["member_id"]))
    record = {"appointment_id": appt_id, "member_id": m["member_id"], "npi": body.npi, "location_id": body.location_id,
              "start": body.start.isoformat(), "visit_type": body.visit_type, "booked_on": str(today), "is_synthetic": True}
    if not dry_run:
        blobs.put(f"appointments/{appt_id}.json", json.dumps(record).encode(), "application/json")
    return Appointment(appointment_id=appt_id, status="VALIDATED" if dry_run else "BOOKED", member_id=m["member_number"], npi=body.npi, location_id=body.location_id,
                       start=body.start, visit_type=body.visit_type, care_gaps_to_address=gaps,
                       transportation="Transport benefit available; offer to book a ride." if allowances.get("transport_rides_annual") and transport_need else None,
                       confirmation="Dry run: nothing was booked." if dry_run else "Confirmation sent by text (simulated).",
                       scenario_id=m["scenario_id"])
