"""Endpoint catalog. One router per use case; the blueprint decides which routers are mounted."""

from __future__ import annotations

import hashlib
import time
from collections import deque
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from factory.api_engine.repository import Repository
from factory.api_engine.security import error, require_client


def repo(request: Request) -> Repository:
    return request.app.state.repo


class Synthetic(BaseModel):
    is_synthetic: bool = True
    scenario_id: str | None = None


def verify_provider(r: Repository, npi: str, tin: str, allowed_npis: list[str | None]) -> None:
    """C-01 / PCS-04: the caller's NPI must be on the record and its TIN must match the TIN on file."""
    prov = r.provider(npi)
    if prov is None or prov["tin"] != tin or npi not in [x for x in allowed_npis if x]:
        raise error(403, "VERIFICATION_FAILED",
                    "NPI and TIN could not be verified for this record. Nothing can be disclosed. "
                    "Confirm the billing NPI and TIN on file, or use the provider portal.")


# =============================================================== PCS - claim status
pcs = APIRouter(tags=["PCS Claim status"])


class Denial(BaseModel):
    category: str
    carc: str
    reason_text: str
    liable_party: str
    appealable: bool
    appeal_deadline_dt: date | None


class Payment(BaseModel):
    payment_dt: date
    method: str
    paid_amount: Decimal
    trace_no: str


class ClaimStatus(Synthetic):
    claim_id: str
    claim_type: str
    status: str
    service_from_dt: date
    service_to_dt: date
    received_dt: date
    adjudicated_dt: date | None = None
    total_billed: Decimal
    total_allowed: Decimal
    total_paid: Decimal
    member_resp: Decimal
    network_status: str
    pend_reason: str | None = None
    info_needed_from: str | None = None
    expected_resolution_dt: date | None = None
    denial: Denial | None = None
    payment: Payment | None = None
    next_step: str
    last_updated: date


@pcs.get("/claims/{claim_id}/status", response_model=ClaimStatus, operation_id="get_claim_status",
         summary="Claim status for a provider office",
         description="Status, pend reason, denial reason and payment for one claim. Requires the caller's NPI and TIN.")
def get_claim_status(claim_id: str, npi: str = Query(..., min_length=10, max_length=10), tin: str = Query(..., min_length=9, max_length=9),
                     r: Repository = Depends(repo), _: str = Depends(require_client)) -> ClaimStatus:
    c = r.claim(claim_id)
    if c is None:
        raise error(404, "CLAIM_NOT_FOUND", f"No claim {claim_id}.")
    verify_provider(r, npi, tin, [c["billing_npi"], c["rendering_npi"], c["facility_npi"]])
    d = r.claim_detail(c["claim_id"])
    den, pend, pay = d["denial"], d["pend"], d["payment"]
    if c["status"] == "PENDED" and pend:
        step = f"Send the requested information ({pend['pend_reason'].replace('_', ' ').lower()}); expected resolution {pend['expected_resolution_dt']}."
    elif c["status"] == "DENIED" and den:
        step = ("Submit a corrected claim (frequency code 7) with the missing information." if den["denial_category"] == "MISSING_INFO"
                else f"Appeal by {den['appeal_deadline_dt']} if you disagree." if den["appealable"] else "Not appealable (duplicate).")
    elif c["status"] == "PAID":
        step = "No action needed."
    else:
        step = "Claim is being processed; check again after adjudication."
    return ClaimStatus(
        claim_id=c["claim_number"], claim_type=c["claim_type"], status=c["status"],
        service_from_dt=c["service_from_dt"], service_to_dt=c["service_to_dt"], received_dt=c["received_dt"],
        adjudicated_dt=c["adjudicated_dt"], total_billed=c["total_billed"], total_allowed=c["total_allowed"],
        total_paid=c["total_paid"], member_resp=c["member_resp"], network_status=c["network_status"],
        pend_reason=pend and pend["pend_reason"], info_needed_from=pend and pend["info_needed_from"],
        expected_resolution_dt=pend and pend["expected_resolution_dt"],
        denial=Denial(category=den["denial_category"], carc=den["carc"], reason_text=den["reason_text"],
                      liable_party=den["liable_party"], appealable=den["appealable"],
                      appeal_deadline_dt=den["appeal_deadline_dt"]) if den else None,
        payment=Payment(**pay) if pay else None, next_step=step,
        last_updated=c["paid_dt"] or c["adjudicated_dt"] or c["received_dt"], scenario_id=c["scenario_id"],
    )


# =============================================================== PAS - authorization status
pas = APIRouter(tags=["PAS Authorization status"])


class DocumentRequirement(BaseModel):
    req_id: str
    doc_type: str
    state: str
    requested_dt: date
    due_dt: date


class Determination(BaseModel):
    outcome: str
    valid_from: date | None
    valid_to: date | None
    denial_category: str | None
    denial_rationale: str | None
    decided_by_role: str
    determination_dt: datetime
    appeal_rights_sent_dt: datetime | None


class AuthStatus(Synthetic):
    auth_id: str
    status: str
    urgency: str
    service_type: str
    received_dt: datetime
    decision_due_dt: datetime
    clock_paused: bool
    services: list[dict[str, Any]]
    documents_required: list[DocumentRequirement]
    review: dict[str, Any] | None = None
    determination: Determination | None = None
    next_step: str


@pas.get("/authorizations/{auth_id}/status", response_model=AuthStatus, operation_id="get_authorization_status",
         summary="Authorization status for a provider office",
         description="Status, due time, missing documents and determination. Never approves or denies.")
def get_authorization_status(auth_id: str, npi: str = Query(..., min_length=10, max_length=10), tin: str = Query(..., min_length=9, max_length=9),
                             r: Repository = Depends(repo), _: str = Depends(require_client)) -> AuthStatus:
    a = r.auth(auth_id)
    if a is None:
        raise error(404, "AUTH_NOT_FOUND", f"No authorization {auth_id}.")
    verify_provider(r, npi, tin, [a["requesting_npi"], a["servicing_npi"], a["facility_npi"]])
    d = r.auth_detail(a["auth_id"])
    pending_docs = [x for x in d["documents"] if x["state"] in ("REQUIRED", "REQUESTED")]
    det = d["determination"]
    if pending_docs:
        step = f"Upload {', '.join(x['doc_type'].replace('_', ' ').lower() for x in pending_docs)}; the clock resumes on receipt."
    elif det and det["outcome"] == "DENIED":
        step = "A peer-to-peer review or an appeal is available; appeal rights were sent with the determination."
    elif det:
        step = "No action needed."
    else:
        step = f"Decision due by {a['decision_due_dt']}." + (" Urgent review requested." if a["urgency"] == "URGENT" else "")
    return AuthStatus(
        auth_id=a["auth_number"], status=a["status"], urgency=a["urgency"], service_type=a["service_type"],
        received_dt=a["received_dt"], decision_due_dt=a["decision_due_dt"], clock_paused=bool(pending_docs),
        services=d["lines"], documents_required=[DocumentRequirement(**{k: x[k] for k in DocumentRequirement.model_fields}) for x in pending_docs],
        review=d["review"], determination=Determination(**det) if det else None, next_step=step, scenario_id=a["scenario_id"],
    )


class DocumentUpload(BaseModel):
    doc_type: Literal["CLINICAL_NOTES", "IMAGING", "LABS", "PT_EVAL", "DISCHARGE_SUMMARY", "PRESCRIBER_STATEMENT"]
    document_ref: str = Field(..., description="Reference to the uploaded file (synthetic).")
    npi: str = Field(..., min_length=10, max_length=10)
    tin: str = Field(..., min_length=9, max_length=9)


class DocumentAccepted(Synthetic):
    auth_id: str
    doc_type: str
    state: str
    clock_resumed: bool
    message: str


@pas.post("/authorizations/{auth_id}/documents", response_model=DocumentAccepted, operation_id="submit_authorization_document",
          summary="Submit a requested document for an authorization",
          description="Marks a requested document as received; the decision clock resumes. Uploaded content is treated as untrusted data.")
def submit_authorization_document(auth_id: str, body: DocumentUpload, r: Repository = Depends(repo),
                                  _: str = Depends(require_client)) -> DocumentAccepted:
    a = r.auth(auth_id)
    if a is None:
        raise error(404, "AUTH_NOT_FOUND", f"No authorization {auth_id}.")
    verify_provider(r, body.npi, body.tin, [a["requesting_npi"], a["servicing_npi"], a["facility_npi"]])
    req = next((x for x in r.auth_detail(a["auth_id"])["documents"]
                if x["doc_type"] == body.doc_type and x["state"] in ("REQUIRED", "REQUESTED")), None)
    if req is None:
        raise error(409, "DOCUMENT_NOT_REQUESTED", f"No outstanding request for {body.doc_type} on {auth_id}.")
    r.receive_document(req["req_id"])
    return DocumentAccepted(auth_id=a["auth_number"], doc_type=body.doc_type, state="RECEIVED", clock_resumed=True,
                            message="Document received. The decision clock has resumed.", scenario_id=a["scenario_id"])


# =============================================================== PEV - eligibility verification
pev = APIRouter(tags=["PEV Eligibility verification"])
MAX_DISTINCT_MEMBERS_PER_5_MIN = 20


class EligibilityRequest(BaseModel):
    npi: str = Field(..., min_length=10, max_length=10)
    tin: str = Field(..., min_length=9, max_length=9)
    date_of_service: date
    member_id: str | None = Field(None, description="Member number (MBR-...) or member UUID")
    first_name: str | None = None
    last_name: str | None = None
    dob: date | None = None


class CostShare(BaseModel):
    copay_pcp: Decimal
    copay_specialist: Decimal
    coinsurance_pct: int
    deductible: Decimal
    deductible_met: Decimal
    oop_max: Decimal
    oop_met: Decimal


class Eligibility(Synthetic):
    reference_number: str
    member_id: str
    eligible: bool
    enrollment_status: str | None
    plan_code: str | None = None
    plan_name: str | None = None
    lob: str | None = None
    coverage_effective_dt: date | None = None
    coverage_term_dt: date | None = None
    pcp: dict[str, Any] | None = None
    cost_share: CostShare | None = None
    cob_flag: bool | None = None
    pa_required_codes: list[str] = []
    warnings: list[str] = []
    disclaimer: str = "Eligibility is not a guarantee of payment."


@pev.post("/eligibility/verify", response_model=Eligibility, operation_id="verify_eligibility",
          summary="Verify member eligibility for a date of service",
          description="One member per request (minimum necessary). Requires the caller's NPI and TIN.")
def verify_eligibility(body: EligibilityRequest, request: Request, r: Repository = Depends(repo),
                       client: str = Depends(require_client)) -> Eligibility:
    prov = r.provider(body.npi)
    if prov is None or prov["tin"] != body.tin or prov["status"] != "ACTIVE":
        raise error(403, "VERIFICATION_FAILED", "NPI and TIN could not be verified.")
    if body.member_id:
        m = r.member(body.member_id)
    elif body.first_name and body.last_name and body.dob:
        m = r.member_by_demographics(body.first_name, body.last_name, body.dob)
    else:
        raise error(422, "MEMBER_IDENTIFIERS_REQUIRED", "Send member_id, or first_name + last_name + dob.")
    if m is None:
        raise error(404, "MEMBER_NOT_FOUND", "No member matches the identifiers given.")

    # PEV-04: minimum necessary - cap distinct members per client in a 5-minute window
    lookups: dict[str, deque] = request.app.state.lookups
    now, q = time.time(), lookups.setdefault(client, deque())
    while q and q[0][0] < now - 300:
        q.popleft()
    seen = {mid for _, mid in q}
    if m["member_id"] not in seen and len(seen) >= MAX_DISTINCT_MEMBERS_PER_5_MIN:
        raise error(429, "MINIMUM_NECESSARY", "Too many different members looked up in a short time. "
                    "Use the batch 270/271 EDI process for bulk eligibility.")
    q.append((now, m["member_id"]))

    ref = "ELG-" + hashlib.sha256(f"{m['member_id']}|{body.date_of_service}|{body.npi}".encode()).hexdigest()[:10].upper()
    e = r.enrollment_on(m["member_id"], body.date_of_service)
    warnings: list[str] = []
    if e is None:
        latest = r.latest_enrollment(m["member_id"])
        return Eligibility(reference_number=ref, member_id=m["member_number"], eligible=False,
                           enrollment_status=latest and latest["status"],
                           warnings=["No coverage on the date of service."], scenario_id=m["scenario_id"])
    if e["retro_change_flag"] and e["status"] == "TERMED":
        warnings.append(f"Coverage was terminated retroactively effective {e['term_dt']}. Eligibility may change.")
    if e["status"] == "PENDING":
        warnings.append("Enrollment is pending; coverage is retroactive to the application date once confirmed.")
    pcp = r.pcp_on(m["member_id"], body.date_of_service)
    acc = r.accumulator(m["member_id"], body.date_of_service.year) or {"deductible_met": Decimal("0.00"), "oop_met": Decimal("0.00")}
    return Eligibility(
        reference_number=ref, member_id=m["member_number"], eligible=e["status"] in ("ACTIVE", "PENDING", "TERMED"),
        enrollment_status=e["status"], plan_code=e["plan_code"], plan_name=e["plan_name"], lob=e["lob"],
        coverage_effective_dt=e["effective_dt"], coverage_term_dt=e["term_dt"],
        pcp={"npi": pcp["pcp_npi"], "name": f"{pcp['first_name']} {pcp['last_name']}", "specialty": pcp["specialty"]} if pcp else None,
        cost_share=CostShare(copay_pcp=e["copay_pcp"], copay_specialist=e["copay_specialist"], coinsurance_pct=e["coinsurance_pct"],
                             deductible=e["deductible"], deductible_met=acc["deductible_met"], oop_max=e["oop_max"], oop_met=acc["oop_met"]),
        cob_flag=e["cob_flag"], pa_required_codes=e["pa_required_codes"], warnings=warnings, scenario_id=m["scenario_id"],
    )


# =============================================================== common: identity and knowledge
common = APIRouter(tags=["Common"])


class IdentityRequest(BaseModel):
    caller_type: Literal["PROVIDER", "MEMBER"]
    npi: str | None = None
    tin: str | None = None
    member_id: str | None = None
    dob: date | None = None
    zip: str | None = None


class IdentityResult(BaseModel):
    verified: bool
    caller_type: str
    factors_matched: list[str]
    is_synthetic: bool = True


@common.post("/identity/verify", response_model=IdentityResult, operation_id="verify_identity",
             summary="Verify a caller before disclosing anything",
             description="Providers: NPI + TIN. Members: member ID + date of birth (+ ZIP).")
def verify_identity(body: IdentityRequest, r: Repository = Depends(repo), _: str = Depends(require_client)) -> IdentityResult:
    matched: list[str] = []
    if body.caller_type == "PROVIDER":
        p = r.provider(body.npi or "")
        if p and p["status"] == "ACTIVE":
            matched.append("NPI")
            if p["tin"] == body.tin:
                matched.append("TIN")
        return IdentityResult(verified=matched == ["NPI", "TIN"], caller_type="PROVIDER", factors_matched=matched)
    m = r.member(body.member_id or "")
    if m:
        matched.append("MEMBER_ID")
        if m["dob"] == body.dob:
            matched.append("DOB")
        if body.zip and m["zip"] == body.zip:
            matched.append("ZIP")
    return IdentityResult(verified={"MEMBER_ID", "DOB"} <= set(matched), caller_type="MEMBER", factors_matched=matched)


class KnowledgeHit(BaseModel):
    chunk_id: str
    asset_id: str
    title: str
    asset_type: str
    audience: str
    score: float
    text: str


class KnowledgeResults(BaseModel):
    query: str
    results: list[KnowledgeHit]
    is_synthetic: bool = True


@common.get("/knowledge/search", response_model=KnowledgeResults, operation_id="search_knowledge",
            summary="Search governed knowledge assets",
            description="Semantic search over ACTIVE plan documents, policies, FAQs and SOPs. Cite the returned chunk.")
def search_knowledge(request: Request, q: str = Query(..., min_length=2, max_length=500),
                     audience: Literal["MEMBER", "PROVIDER", "INTERNAL"] | None = None,
                     k: int = Query(5, ge=1, le=10), _: str = Depends(require_client)) -> KnowledgeResults:
    stores = request.app.state.stores
    vec = stores.llm.embed([q])[0]
    hits = stores.vectors.search(vec, k=k, where={"audience": audience} if audience else None)
    return KnowledgeResults(query=q, results=[KnowledgeHit(**{f: h[f] for f in KnowledgeHit.model_fields}) for h in hits])


# use case -> router. v1 implements the S1 use cases; others fail fast in create_app.
ENDPOINTS: dict[str, APIRouter] = {"PCS": pcs, "PAS": pas, "PEV": pev}

# (method, path under base_path) per use case; COMMON is mounted for every tenant.
ENDPOINT_CATALOG: dict[str, list[tuple[str, str]]] = {
    uc: sorted((m, r.path) for r in router.routes for m in r.methods)
    for uc, router in {**ENDPOINTS, "COMMON": common}.items()
}
