"""Authorizations for one shard as an AuthBatch, then one writer per auth entity.

Two kinds: claim-linked auths (one APPROVED auth per claim with a PA-required code, received before and valid
across the date of service: R-03, T-04) and standalone auths drawn at the D-06 outcome mix. Auth scenarios
(PAS-01..04) force status, urgency, service type and missing documents.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from factory.codes import AuthLineStatus, AuthStatus, ClaimType, DenialCategory, ProviderRole, ServiceType
from factory.data_engine import catalog, keys
from factory.data_engine.context import GenContext, dates, stamps
from factory.data_engine.knowledge_catalog import pa_policy_asset_id
from factory.data_engine.rng import RowRandom
from factory.data_engine.stages.s5.claims import ClaimBatch
from factory.data_engine.stages.s5.inputs import ANCILLARY, CPT, DAY, HOUR, NAT, Inputs, pick_provider

_ = None  # placeholder default: a step fills the field
FACILITY_SERVICES = [ServiceType.INPATIENT, ServiceType.SNF, ServiceType.HOME_HEALTH]


@dataclass(eq=False)
class AuthBatch:
    """Parallel arrays, one entry per auth. Claim-linked auths come first (positions 0..len(lk)-1)."""

    lk: np.ndarray  # claim positions that need an auth (linked auths, in order)
    a_member: np.ndarray  # member position in the shard
    a_key: np.ndarray  # linked: the claim key; standalone: member*64 + 32 + j
    linked: np.ndarray
    arr: RowRandom  # auth_request row streams
    scen: np.ndarray
    # ---- outcomes and clocks
    stype: np.ndarray = _
    urgent: np.ndarray = _
    recv_day: np.ndarray = _
    received: np.ndarray = _  # timestamps
    due: np.ndarray = _
    status: np.ndarray = _
    decided: np.ndarray = _
    det_dt: np.ndarray = _
    # ---- codes and parties
    cpt0: np.ndarray = _  # code of auth line 0
    plan_ids: np.ndarray = _
    member_ids: np.ndarray = _
    requesting: np.ndarray = _
    servicing: np.ndarray = _
    facility: np.ndarray = _
    persona: np.ndarray = _
    auth_ids: np.ndarray = _

    @property
    def n(self) -> int:
        return len(self.a_key)


def gen_auths(ctx: GenContext, inp: Inputs, c: ClaimBatch) -> np.ndarray:
    """Generate and write the shard's auth entities. Returns the auth id per claim (None when not linked)."""
    a = _auth_rows(ctx, inp, c)
    _outcomes_and_clocks(ctx, c, a)
    _codes_and_parties(ctx, inp, c, a)
    _write_requests(ctx, a)
    _write_lines(ctx, a)
    _write_determinations(ctx, c, a)
    _write_review_queue(ctx, a)
    _write_documentation(ctx, a)
    claim_auth = np.full(c.n, None, dtype=object)
    claim_auth[a.lk] = a.auth_ids[:len(a.lk)]
    return claim_auth


def _auth_rows(ctx: GenContext, inp: Inputs, c: ClaimBatch) -> AuthBatch:
    """One linked auth per claim that needs one (it reuses the claim key), plus Poisson standalone auths per member."""
    midx = c.midx
    lk = np.where(c.needs_pa)[0]
    rr = ctx.rr("auth_request:count", midx)
    k = np.minimum(rr.poisson("n_auths", ctx.spec.volumes.get("authorizations", 0) / max(len(inp.members["member_id"]), 1) * 0.5), 5)
    si = np.repeat(np.arange(len(midx)), k)
    sj = np.concatenate([np.arange(x) for x in k]) if len(si) else np.array([], np.int64)
    a_key = np.concatenate([c.ckey[lk], keys.standalone_auth_key(midx[si], sj)])
    linked = np.concatenate([np.ones(len(lk), bool), np.zeros(len(si), bool)])
    arr = ctx.rr("auth_request", a_key)
    scen = np.where(linked, "BASE", ctx.scenarios("auth", arr)).astype(object)
    return AuthBatch(lk=lk, a_member=np.concatenate([c.ci[lk], si]), a_key=a_key, linked=linked, arr=arr, scen=scen)


def _outcomes_and_clocks(ctx: GenContext, c: ClaimBatch, a: AuthBatch) -> None:
    """Outcome mix (D-06) and scenario overrides, service type, urgency, received/due/decision times (T-03)."""
    arr, scen, linked, lk = a.arr, a.scen, a.linked, a.lk
    # happy path PAS-01 = a routine standard review; other baseline rows keep the D-06 outcome mix
    outcome = arr.choice("outcome", *catalog.AUTH_OUTCOMES).astype(object)
    scen[(scen == "PAS-01") & (outcome != AuthStatus.IN_REVIEW)] = "BASE"

    stype = arr.choice("stype", [ServiceType.OUTPATIENT, ServiceType.INPATIENT, ServiceType.DME, ServiceType.HOME_HEALTH,
                                 ServiceType.SNF, ServiceType.PHARMACY, ServiceType.BH], [45, 10, 10, 10, 10, 10, 5]).astype(object)
    stype[linked] = np.where(c.ctype[lk] == ClaimType.FACILITY, ServiceType.INPATIENT, ServiceType.OUTPATIENT)
    stype[scen == "PAS-02"] = ServiceType.SNF
    stype[scen == "PAS-03"] = ServiceType.OUTPATIENT
    urgent = arr.bernoulli("urgent", 0.15) & ~linked
    urgent[scen == "PAS-02"] = True
    urgent[np.isin(scen, ["PAS-01", "PAS-03"])] = False

    recv_day = ctx.as_of - arr.integers("recv_back", 0, 60).astype("timedelta64[D]")
    recv_day[linked] = c.s_from[lk] - arr.integers("lead", 5, 26)[linked].astype("timedelta64[D]")
    recv_day[scen == "PAS-01"] = ctx.as_of - 5 * DAY
    recv_day[scen == "PAS-02"] = ctx.as_of - 1 * DAY
    recv_day[scen == "PAS-03"] = ctx.as_of - 3 * DAY
    received = recv_day.astype("datetime64[s]") + (8 + arr.integers("recv_hour", 0, 9)).astype("timedelta64[h]")
    due = received + np.where(urgent, 72, 7 * 24).astype("timedelta64[h]")

    outcome[linked] = AuthStatus.APPROVED
    outcome[np.isin(scen, ["PAS-02", "PAS-03"])] = AuthStatus.PENDED_INFO
    outcome[scen == "PAS-04"] = AuthStatus.DENIED
    decided = np.isin(outcome, [AuthStatus.APPROVED, AuthStatus.PARTIAL, AuthStatus.DENIED])
    det_dt = received + (arr.u("det_at") * 0.95 * (due - received).astype(np.int64)).astype("timedelta64[s]")
    det_dt[linked] = received[linked] + arr.integers("det_lag", 1, 4)[linked].astype("timedelta64[D]")
    late = decided & (det_dt > ctx.as_of.astype("datetime64[s]") + np.timedelta64(23, "h"))
    outcome[late] = AuthStatus.IN_REVIEW
    decided &= ~late
    status = outcome
    # PAS-01 tags every row that satisfies its recipe: standard review still open, no documents pending
    routine = ~linked & (status == AuthStatus.IN_REVIEW) & ~urgent & np.isin(scen, ["BASE", "PAS-01"])
    a.scen = np.where(routine, "PAS-01", np.where(scen == "PAS-01", "BASE", scen))
    a.stype, a.urgent, a.recv_day, a.received, a.due = stype, urgent, recv_day, received, due
    a.status, a.decided, a.det_dt = status, decided, det_dt


def _codes_and_parties(ctx: GenContext, inp: Inputs, c: ClaimBatch, a: AuthBatch) -> None:
    """Line-0 code (the claim's code for linked auths), requesting/servicing/facility providers, submitter persona."""
    arr, linked, lk, stype, n = a.arr, a.linked, a.lk, a.stype, a.n
    cpt0 = arr.choice("cpt", catalog.PA_CODES).astype(object)
    claim_line0 = {int(ck): cp for ck, cp, l in zip(c.ckey[c.li], c.cpt, c.ln) if l == 0}
    cpt0[linked] = [claim_line0[int(k)] for k in c.ckey[lk]]
    cpt0[a.scen == "PAS-03"] = "97161"

    plan_ids = c.m["plan_id"][a.a_member]
    member_ids = c.m["member_id"][a.a_member]
    requesting = np.array([inp.pcp.get(mid) for mid in member_ids], dtype=object)
    fallback = pick_provider(inp, plan_ids, ProviderRole.PCP, np.ones(n, bool), arr.u("req"))
    requesting = np.where(requesting == None, fallback, requesting)  # noqa: E711
    servicing = pick_provider(inp, plan_ids, ProviderRole.SPECIALIST, np.ones(n, bool), arr.u("svc"))
    servicing[linked] = c.billing[lk]
    fac_role = np.where(stype == ServiceType.INPATIENT, ProviderRole.HOSPITAL, ProviderRole.POSTACUTE)
    facility = np.where(np.isin(stype, FACILITY_SERVICES), None, None).astype(object)
    for r in (ProviderRole.HOSPITAL, ProviderRole.POSTACUTE):
        mask = np.isin(stype, FACILITY_SERVICES) & (fac_role == r)
        facility[mask] = pick_provider(inp, plan_ids[mask], r, np.ones(mask.sum(), bool), arr.u(f"fac:{r}")[mask])
    a.persona = np.where(np.isin(stype, [ServiceType.SNF, ServiceType.INPATIENT, ServiceType.HOME_HEALTH]), "DISCHARGE",
                         arr.choice("persona", ["OFFICE", "REFERRAL"], [50, 50]))
    a.cpt0, a.plan_ids, a.member_ids = cpt0, plan_ids, member_ids
    a.requesting, a.servicing, a.facility = requesting, servicing, facility
    a.auth_ids = np.asarray(ctx.ids("auth_request", a.a_key), dtype=object)


# ------------------------------------------------------------------ writers
def _write_requests(ctx: GenContext, a: AuthBatch) -> None:
    arr, n = a.arr, a.n
    ctx.write("auth_request", {
        "auth_id": pa.array(a.auth_ids.tolist()),
        "auth_number": pa.array(keys.business_numbers("AUTH", a.a_key)),
        "member_id": pa.array(a.member_ids.tolist()),
        "requesting_npi": pa.array(a.requesting.tolist()),
        "servicing_npi": pa.array(a.servicing.tolist()),
        "facility_npi": pa.array(a.facility.tolist(), pa.string()),
        "plan_id": pa.array(a.plan_ids.tolist()),
        "auth_type": pa.array(["PRE_SERVICE"] * n),
        "urgency": pa.array(np.where(a.urgent, "URGENT", "STANDARD").tolist()),
        "service_type": pa.array(a.stype.tolist()),
        "primary_dx": pa.array(arr.choice("dx", catalog.ICD10).tolist()),
        "received_dt": stamps(a.received),
        "decision_due_dt": stamps(a.due),
        "status": pa.array(a.status.tolist()),
        "channel_received": pa.array(arr.choice("chan", ["PORTAL", "FAX", "PHONE", "CHAT", "EDI"], [50, 25, 10, 5, 10]).tolist()),
        "submitted_by_persona": pa.array(a.persona.tolist()),
    }, scenario=a.scen)


def _write_lines(ctx: GenContext, a: AuthBatch) -> None:
    """One or two lines per auth; approved units follow the outcome (PARTIAL = half, at least 1)."""
    if not ctx.wants("auth_line"):
        return
    n = a.n
    n_al = 1 + a.arr.bernoulli("second_line", 0.3).astype(int)
    ai = np.repeat(np.arange(n), n_al)
    aj = np.concatenate([np.arange(x) for x in n_al]) if n else np.array([], np.int64)
    al_key = keys.auth_line_key(a.a_key[ai], aj)
    alrr = ctx.rr("auth_line", al_key)
    req_units = alrr.integers("units", 1, 13)
    out_i = a.status[ai]
    appr = np.where(out_i == AuthStatus.APPROVED, req_units, np.where(out_i == AuthStatus.PARTIAL, np.maximum(req_units // 2, 1),
                    np.where(out_i == AuthStatus.DENIED, 0, -1)))
    l_status = np.where(out_i == AuthStatus.APPROVED, AuthLineStatus.APPROVED, np.where(out_i == AuthStatus.PARTIAL, AuthLineStatus.MODIFIED,
                        np.where(out_i == AuthStatus.DENIED, AuthLineStatus.DENIED, None)))
    stype = a.stype[ai]
    ctx.write("auth_line", {
        "auth_line_id": pa.array(ctx.ids("auth_line", al_key)),
        "auth_id": pa.array(a.auth_ids[ai].tolist()),
        "cpt_hcpcs_or_ndc": pa.array(np.where(aj == 0, a.cpt0[ai], alrr.choice("cpt2", ANCILLARY)).tolist()),
        "units_requested": pa.array(req_units),
        "units_approved": pa.array([None if x < 0 else int(x) for x in appr], pa.int64()),
        "site_of_service": pa.array(np.where(np.isin(stype, [ServiceType.INPATIENT]), "21", np.where(stype == ServiceType.SNF, "31", "22")).tolist()),
        "line_status": pa.array(l_status.tolist(), pa.string()),
    }, scenario=a.scen[ai])


def _write_determinations(ctx: GenContext, c: ClaimBatch, a: AuthBatch) -> None:
    """Decided auths: validity window (linked: across the date of service), reviewer role, denial rationale."""
    if not ctx.wants("auth_determination"):
        return
    d = np.where(a.decided)[0]
    drr = ctx.rr("auth_determination", a.a_key[d])
    d_out = a.status[d]
    denied = d_out == AuthStatus.DENIED
    valid_from = a.recv_day[d]
    valid_to = valid_from + 90 * DAY
    valid_to[a.linked[d]] = c.s_from[a.lk] + 60 * DAY
    role = np.where(denied, drr.choice("md", ["MEDICAL_DIRECTOR", "SAME_SPECIALTY_PEER"], [70, 30]),
                    drr.choice("rn", ["NURSE", "MEDICAL_DIRECTOR"], [70, 30]))
    rationale = [f"Clinical documentation does not meet criteria for {CPT[cp][1].lower()} at this time." if den else None
                 for den, cp in zip(denied, a.cpt0[d])]
    ctx.write("auth_determination", {
        "determination_id": pa.array(ctx.ids("auth_determination", a.a_key[d])),
        "auth_id": pa.array(a.auth_ids[d].tolist()),
        "outcome": pa.array(d_out.tolist()),
        "valid_from": dates(np.where(denied, NAT, valid_from)),
        "valid_to": dates(np.where(denied, NAT, valid_to)),
        "denial_category": pa.array(np.where(denied, DenialCategory.MED_NECESSITY, None).tolist(), pa.string()),
        "denial_rationale": pa.array(rationale, pa.string()),
        "criteria_asset_id": pa.array([pa_policy_asset_id(ctx, cp) for cp in a.cpt0[d]]),
        "decided_by_role": pa.array(role.tolist()),
        "determination_dt": stamps(a.det_dt[d]),
        "appeal_rights_sent_dt": stamps(np.where(denied, a.det_dt[d], np.datetime64("NaT", "s"))),
    }, scenario=a.scen[d])


def _write_review_queue(ctx: GenContext, a: AuthBatch) -> None:
    """Open reviews: auths in review, waiting for documents, or just submitted."""
    if not ctx.wants("clinical_review_queue"):
        return
    o = np.where(np.isin(a.status, [AuthStatus.IN_REVIEW, AuthStatus.PENDED_INFO, AuthStatus.SUBMITTED]))[0]
    orr = ctx.rr("clinical_review_queue", a.a_key[o])
    ctx.write("clinical_review_queue", {
        "queue_id": pa.array(ctx.ids("clinical_review_queue", a.a_key[o])),
        "auth_id": pa.array(a.auth_ids[o].tolist()),
        "review_level": pa.array(orr.choice("level", ["NURSE", "MEDICAL_DIRECTOR"], [80, 20]).tolist()),
        "assigned_dt": stamps(a.received[o] + 2 * HOUR),
        "sla_due_dt": stamps(a.due[o]),
        "status": pa.array(np.where(a.status[o] == AuthStatus.PENDED_INFO, "WAITING", orr.choice("qs", ["WAITING", "IN_REVIEW"])).tolist()),
        "p2p_available_until": stamps(np.full(len(o), np.datetime64("NaT", "s"))),
    }, scenario=a.scen[o])


def _write_documentation(ctx: GenContext, a: AuthBatch) -> None:
    """Documents: REQUESTED (clock paused) for pended auths; RECEIVED for 30% of decided standalone ones."""
    if not ctx.wants("documentation_requirement"):
        return
    doc_req = (a.status == AuthStatus.PENDED_INFO) | (a.decided & a.arr.bernoulli("had_docs", 0.3) & ~a.linked)
    q = np.where(doc_req)[0]
    qrr = ctx.rr("documentation_requirement", a.a_key[q])
    doc_type = qrr.choice("doc", ["CLINICAL_NOTES", "IMAGING", "LABS", "PT_EVAL", "PRESCRIBER_STATEMENT"]).astype(object)
    doc_type[a.scen[q] == "PAS-02"] = "DISCHARGE_SUMMARY"
    doc_type[a.scen[q] == "PAS-03"] = "PT_EVAL"
    pended = a.status[q] == AuthStatus.PENDED_INFO
    req_dt = a.recv_day[q] + DAY
    ctx.write("documentation_requirement", {
        "req_id": pa.array(ctx.ids("documentation_requirement", a.a_key[q])),
        "auth_id": pa.array(a.auth_ids[q].tolist()),
        "doc_type": pa.array(doc_type.tolist()),
        "state": pa.array(np.where(pended, "REQUESTED", "RECEIVED").tolist()),
        "requested_dt": dates(req_dt),
        "due_dt": dates(req_dt + 14 * DAY),
        "clock_paused": pa.array(pended),
    }, scenario=a.scen[q])
