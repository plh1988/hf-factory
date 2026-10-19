"""Provider-facing endpoints added for S3/S4: PAP appeals and PPI payment inquiry. NPI + TIN verification on every call."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from factory.api_engine.repository import Repository
from factory import codes
from factory.api_engine.errors import ErrorCode, error
from factory.api_engine.routes import Synthetic, repo, verify_provider
from factory.api_engine.security import require_client

NPI = Query(..., min_length=10, max_length=10)
TIN = Query(..., min_length=9, max_length=9)

# =============================================================== PAP - appeals
pap = APIRouter(tags=["PAP Appeals"])


class AppealRequest(BaseModel):
    level: Literal["L1", "L2", "EXTERNAL_REVIEW"] = "L1"
    reason: Literal["MED_NECESSITY", "CODING", "TIMELY_FILING", "COB", "NON_COVERED", "MISSING_INFO", "OTHER"]
    documents: list[str] = Field(default_factory=list, description="References to uploaded documents (synthetic)")
    npi: str = Field(..., min_length=10, max_length=10)
    tin: str = Field(..., min_length=9, max_length=9)


class AppealCreated(Synthetic):
    appeal_id: str
    claim_id: str
    status: str
    level: str
    filed_dt: date
    deadline_dt: date
    documents_required: list[str]
    next_step: str


@pap.post("/claims/{claim_id}/appeals", response_model=AppealCreated, operation_id="file_claim_appeal", status_code=201,
          summary="Open an appeal on a denied claim",
          description="Checks that the denial is appealable and within the deadline. Never predicts the outcome or waives deadlines.")
def file_claim_appeal(claim_id: str, body: AppealRequest, request: Request, dry_run: bool = Query(False, description="Validate only; nothing is written"),
                      r: Repository = Depends(repo), _: str = Depends(require_client)) -> AppealCreated:
    today: date = request.app.state.as_of
    c = r.claim(claim_id)
    if c is None:
        raise error(ErrorCode.CLAIM_NOT_FOUND, f"No claim {claim_id}.")
    verify_provider(r, body.npi, body.tin, [c["billing_npi"], c["rendering_npi"], c["facility_npi"]])
    d = r.claim_detail(c["claim_id"])["denial"]
    if c["status"] != codes.ClaimStatus.DENIED or d is None:
        raise error(ErrorCode.NOT_DENIED, f"Claim {claim_id} is {c['status']}; only denied claims can be appealed.")
    if not d["appealable"]:
        raise error(ErrorCode.NOT_APPEALABLE, f"Denial {d['carc']} ({d['denial_category']}) is not appealable; submit a corrected claim instead.")
    existing = r.one("SELECT appeal_number, status FROM appeal_record WHERE claim_id = ? AND status <> 'DECIDED'", [c["claim_id"]])
    if existing:
        raise error(ErrorCode.APPEAL_EXISTS, f"Appeal {existing['appeal_number']} is already {existing['status']} for this claim.")
    if d["appeal_deadline_dt"] and today > d["appeal_deadline_dt"]:
        raise error(ErrorCode.DEADLINE_PASSED, f"The appeal window closed on {d['appeal_deadline_dt']}. A late appeal can be routed to "
                    "exception review with a good-cause reason; the outcome is not guaranteed.")
    required = ["clinical_notes"] if body.reason == codes.DenialCategory.MED_NECESSITY and not body.documents else []
    appeal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"appeal:{c['claim_id']}:{body.level}:{today}"))
    number = "APL-" + c["claim_number"][4:] + f"-{body.level}"
    if not dry_run:
      r.insert_row("appeal_record", "appeal_id", {
        "appeal_id": appeal_id, "appeal_number": number, "claim_id": c["claim_id"], "filed_by": "PROVIDER", "level": body.level,
        "reason": body.reason, "filed_dt": today, "deadline_dt": d["appeal_deadline_dt"], "status": codes.AppealStatus.OPEN, "outcome": None,
        "decision_dt": None, "documents_received": body.documents, "late_filing_reason": None,
        "is_synthetic": True, "tenant_id": c["tenant_id"], "scenario_id": c["scenario_id"]})
    step = (f"Submit {', '.join(required)} before {d['appeal_deadline_dt']}." if required
            else f"A decision is due by {today + timedelta(days=30)}.")
    return AppealCreated(appeal_id=number, claim_id=c["claim_number"], status="VALIDATED" if dry_run else codes.AppealStatus.OPEN, level=body.level, filed_dt=today,
                         deadline_dt=d["appeal_deadline_dt"], documents_required=required, next_step=step, scenario_id=c["scenario_id"])


class AppealStatus(Synthetic):
    appeal_id: str
    claim_id: str
    level: str
    status: str
    filed_dt: date
    deadline_dt: date
    filed_late: bool
    outcome: str | None
    decision_dt: date | None
    decision_due_dt: date | None
    documents_received: list[str]
    reprocessing: dict[str, Any] | None
    next_step: str


@pap.get("/appeals/{appeal_id}", response_model=AppealStatus, operation_id="get_appeal",
         summary="Appeal status, decision and reprocessing",
         description="For overturned appeals, shows whether the claim has been reprocessed and the replacement claim.")
def get_appeal(appeal_id: str, request: Request, npi: str = NPI, tin: str = TIN, r: Repository = Depends(repo),
               _: str = Depends(require_client)) -> AppealStatus:
    a = r.one("SELECT * FROM appeal_record WHERE appeal_number = ? OR appeal_id = ?", [appeal_id, appeal_id])
    if a is None:
        raise error(ErrorCode.APPEAL_NOT_FOUND, f"No appeal {appeal_id}.")
    c = r.one("SELECT * FROM claim_header WHERE claim_id = ?", [a["claim_id"]])
    verify_provider(r, npi, tin, [c["billing_npi"], c["rendering_npi"], c["facility_npi"]])
    late = a["filed_dt"] > a["deadline_dt"]
    rp = None
    if a["outcome"] == codes.AppealOutcome.OVERTURNED:
        h = r.one("SELECT r.*, n.claim_number AS new_number FROM reprocessing_history r JOIN claim_header n ON n.claim_id = r.new_claim_id "
                  "WHERE r.claim_id = ?", [a["claim_id"]]) if r.exists("reprocessing_history") else None
        rp = ({"status": codes.ClaimStatus.REPROCESSED, "replacement_claim_id": h["new_number"], "reprocess_dt": h["reprocess_dt"],
               "paid_before": h["paid_before"], "paid_after": h["paid_after"]} if h else
              {"status": "AWAITING_REPROCESSING", "expected_by": a["decision_dt"] + timedelta(days=30)})
    if a["status"] == codes.AppealStatus.DECIDED:
        step = ("No action needed; the claim was reprocessed." if rp and rp["status"] == codes.ClaimStatus.REPROCESSED else
                f"The claim will be reprocessed by {rp['expected_by']}." if rp else
                "The denial was upheld. A level 2 appeal or external review is available.")
    elif late:
        step = "Filed after the deadline: routed to exception review for the good-cause reason. No outcome is promised."
    else:
        step = f"Decision due by {a['filed_dt'] + timedelta(days=30)}."
    return AppealStatus(appeal_id=a["appeal_number"], claim_id=c["claim_number"], level=a["level"], status=a["status"], filed_dt=a["filed_dt"],
                        deadline_dt=a["deadline_dt"], filed_late=late, outcome=a["outcome"], decision_dt=a["decision_dt"],
                        decision_due_dt=None if a["status"] == codes.AppealStatus.DECIDED else a["filed_dt"] + timedelta(days=30),
                        documents_received=a["documents_received"] or [], reprocessing=rp, next_step=step, scenario_id=a["scenario_id"])


# =============================================================== PPI - payment inquiry
ppi = APIRouter(tags=["PPI Payment inquiry"])


class PaymentView(BaseModel):
    claim_id: str
    payment_dt: date
    paid_amount: Decimal
    method: str
    trace_no: str
    payee_tin_last4: str
    payee_matches_billing_tin: bool
    recoupment: dict[str, Any] | None


class PaymentInquiry(Synthetic):
    claim_id: str
    status: str
    total_billed: Decimal
    total_allowed: Decimal
    total_paid: Decimal
    adjustments: dict[str, Any]
    payments: list[PaymentView]
    warnings: list[str]


@ppi.get("/claims/{claim_id}/payments", response_model=PaymentInquiry, operation_id="get_claim_payments",
         summary="Payments, adjustments and offsets for a claim",
         description="Payment date, amount, method and trace; CO write-off vs PR member share; recoupments. Never changes payment instructions.")
def get_claim_payments(claim_id: str, npi: str = NPI, tin: str = TIN, r: Repository = Depends(repo),
                       _: str = Depends(require_client)) -> PaymentInquiry:
    c = r.claim(claim_id)
    if c is None:
        raise error(ErrorCode.CLAIM_NOT_FOUND, f"No claim {claim_id}.")
    verify_provider(r, npi, tin, [c["billing_npi"], c["rendering_npi"], c["facility_npi"]])
    billing_tin = r.provider(c["billing_npi"])["tin"]
    ids = [c["claim_id"]]
    if r.exists("reprocessing_history"):
        ids += [x["new_claim_id"] for x in r.rows("SELECT new_claim_id FROM reprocessing_history WHERE claim_id = ?", [c["claim_id"]])]
    pays = r.rows(f"SELECT p.*, h.claim_number FROM claim_payment p JOIN claim_header h ON h.claim_id = p.claim_id "
                  f"WHERE p.claim_id IN ({', '.join('?' * len(ids))}) ORDER BY p.payment_dt", ids) if r.exists("claim_payment") else []
    adj = r.rows("SELECT a.group_code, a.carc, sum(a.amount) AS amount FROM claim_adjustment a JOIN claim_line l ON l.line_id = a.line_id "
                 "WHERE l.claim_id = ? GROUP BY 1, 2 ORDER BY 1, 2", [c["claim_id"]]) if r.exists("claim_adjustment") else []
    co = sum(Decimal(str(a["amount"])) for a in adj if a["group_code"] == "CO")
    pr = sum(Decimal(str(a["amount"])) for a in adj if a["group_code"] == "PR")
    warnings, views = [], []
    for p in pays:
        match = p["payee_tin"] == billing_tin
        if not match:
            warnings.append(f"Payment {p['trace_no']} went to TIN ending {p['payee_tin'][-4:]}, not the billing TIN on file. "
                            "A correction ticket can be opened; the agent cannot change payment instructions.")
        rec = None
        if p["recoup_flag"]:
            rec = {"offset_ref": p["offset_ref"], "offset_amount": p.get("offset_amount"),
                   "explanation": "An overpayment on an earlier claim was recovered from this remittance."}
        views.append(PaymentView(claim_id=p["claim_number"], payment_dt=p["payment_dt"], paid_amount=p["paid_amount"], method=p["method"],
                                 trace_no=p["trace_no"], payee_tin_last4=p["payee_tin"][-4:], payee_matches_billing_tin=match, recoupment=rec))
    if not pays:
        warnings.append(f"No payment on file; the claim is {c['status']}.")
    return PaymentInquiry(
        claim_id=c["claim_number"], status=c["status"], total_billed=c["total_billed"], total_allowed=c["total_allowed"],
        total_paid=c["total_paid"],
        adjustments={"provider_write_off_CO": co, "member_share_PR": pr, "detail": adj,
                     "explained": f"Billed {c['total_billed']} - contractual write-off and denials {co} - member share {pr} = paid {c['total_paid']}."},
        payments=views, warnings=warnings, scenario_id=c["scenario_id"])
