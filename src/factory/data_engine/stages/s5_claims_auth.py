"""Stage s5: claims (all claim entities), authorizations, and member accumulators derived from paid claims.

Sharded by member index: each member's claims and auths come from that member's own row streams.
Rules enforced while generating: R-02 coverage, R-03 auth for PA lines, R-04 network, R-08 pharmacy
detail, T-02 date order, T-03 auth clocks, T-04 auth covers DOS, F-01/F-02/F-05 money math, F-03 accumulators.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

from factory.data_engine import catalog
from factory.data_engine.context import GenContext, dates, stamps
from factory.data_engine.knowledge_catalog import pa_policy_asset_id
from factory.data_engine.rng import RowRandom, money

DAY = np.timedelta64(1, "D")
HOUR = np.timedelta64(1, "h")
NAT = np.datetime64("NaT", "D")

CPT = {c[0]: c for c in catalog.CPT}
OFFICE = ["99213", "99214", "99203", "G0439"]
ANCILLARY = ["93000", "71046", "80053", "85025", "36415"]
PROF_PA = ["70553", "72148", "97161"]
INPATIENT = ["99223", "99232", "99238"]
DRUGS_NO_PA = [d for d in catalog.NDC if not d[3]]
DRUG_MEDIAN_CENTS = {1: 1200, 2: 3500, 3: 25000, 4: 90000, 5: 150000}


# ------------------------------------------------------------------ inputs read from the TableStore
def load_inputs(ctx: GenContext) -> dict:
    q = ctx.stores.tables.query
    members = q(
        """SELECT m.member_id, m.member_number, m.lob, m.scenario_id, e.plan_id, e.effective_dt, e.term_dt, e.status
           FROM member_demographics m
           JOIN (SELECT member_id, plan_id, effective_dt, term_dt, status,
                        row_number() OVER (PARTITION BY member_id ORDER BY effective_dt DESC) AS rn
                 FROM plan_enrollment) e ON e.member_id = m.member_id AND e.rn = 1
           ORDER BY m.member_number"""
    )
    plans = q("SELECT plan_id, lob, deductible, oop_max, copay_pcp, copay_specialist, coinsurance_pct FROM plan")
    prov = q(
        """SELECT np.plan_id, p.npi, p.provider_type, p.facility_type
           FROM network_participation np JOIN provider p ON p.npi = np.npi
           WHERE np.status = 'PARTICIPATING' AND p.status = 'ACTIVE' AND np.term_dt IS NULL
           ORDER BY np.plan_id, p.npi"""
    )
    allp = q("SELECT npi, provider_type, facility_type, tin FROM provider WHERE status = 'ACTIVE' ORDER BY npi")
    pcp = q("SELECT member_id, pcp_npi FROM pcp_assignment WHERE status = 'ACTIVE'") if ctx.stores.tables.exists("pcp_assignment") else None

    def role(ptype: str, ftype: str | None) -> str:
        if ptype in ("PCP", "SPECIALIST"):
            return ptype
        return {"PHARMACY": "PHARMACY", "HOSPITAL": "HOSPITAL"}.get(ftype or "", "POSTACUTE")

    pools: dict[str, list[str]] = {}
    for plan_id, npi, pt, ft in zip(*(prov[c].to_pylist() for c in ["plan_id", "npi", "provider_type", "facility_type"])):
        pools.setdefault(f"{plan_id}|{role(pt, ft)}", []).append(npi)
    oon: dict[str, list[str]] = {}
    for npi, pt, ft in zip(*(allp[c].to_pylist() for c in ["npi", "provider_type", "facility_type"])):
        oon.setdefault(role(pt, ft), []).append(npi)
    return {
        "members": {c: np.array(members[c].to_pylist(), dtype=object) for c in members.column_names},
        "plans": {r["plan_id"]: r for r in plans.to_pylist()},
        "pools": pools,
        "oon": oon,
        "ptype": dict(zip(allp["npi"].to_pylist(), allp["provider_type"].to_pylist())),
        "tin": dict(zip(allp["npi"].to_pylist(), allp["tin"].to_pylist())),
        "pcp": dict(zip(pcp["member_id"].to_pylist(), pcp["pcp_npi"].to_pylist())) if pcp is not None else {},
    }


def pick_provider(inp: dict, plan_ids: np.ndarray, role: str, in_network: np.ndarray, u: np.ndarray,
                  with_network: bool = False):
    """Pick from the plan's participating pool when in network; fall back to any active provider.
    with_network=True also returns whether the chosen provider really is in network (R-04)."""
    out = np.empty(len(plan_ids), dtype=object)
    actual = np.zeros(len(plan_ids), dtype=bool)
    for i, (plan, inn, x) in enumerate(zip(plan_ids, in_network, u)):
        pool = inp["pools"].get(f"{plan}|{role}") if inn else None
        actual[i] = bool(pool)
        if not pool:
            pool = inp["oon"].get(role) or inp["oon"]["PCP"]
        out[i] = pool[min(int(x * len(pool)), len(pool) - 1)]
    return (out, actual) if with_network else out


def to_date(a) -> np.ndarray:
    return np.array([np.datetime64(x, "D") if x is not None else NAT for x in a], dtype="datetime64[D]")


# ------------------------------------------------------------------ claims
def gen_claims(ctx: GenContext, inp: dict, sl: slice) -> dict:
    m = {k: v[sl] for k, v in inp["members"].items()}
    midx = np.array([int(x[4:]) - 1 for x in m["member_number"]], dtype=np.int64)
    rr = ctx.rr("claim_header:count", midx)
    eff, term = to_date(m["effective_dt"]), to_date(m["term_dt"])
    hist_start = ctx.as_of - np.timedelta64(ctx.bp.data_spec.history_months * 31, "D")
    w_start = np.maximum(eff, hist_start)
    w_end = np.minimum(np.where(np.isnat(term), ctx.as_of, term), ctx.as_of - 3 * DAY)
    span = (w_end - w_start).astype(int)
    vol = ctx.bp.data_spec.volumes
    lam = vol.claims / max(vol.members, 1)
    k = np.where(span >= 0, np.minimum(rr.poisson("n_claims", lam), 20), 0)

    ci = np.repeat(np.arange(len(midx)), k)
    cj = np.concatenate([np.arange(c) for c in k]) if len(ci) else np.array([], np.int64)
    ckey = midx[ci] * 64 + cj
    n = len(ckey)
    crr = ctx.rr("claim_header", ckey)
    scen = ctx.scenarios("claim", crr)
    ctype = crr.choice("type", ["PHARMACY", "PROFESSIONAL", "FACILITY"], [40, 45, 15])
    ctype = np.where(np.isin(scen, ["PCS-02", "PCS-03", "PCS-04"]), "PROFESSIONAL", ctype)
    inpatient = (ctype == "FACILITY") & crr.bernoulli("inpatient", 0.6)
    los = np.where(inpatient, crr.integers("los", 2, 7), 0)
    los = np.minimum(los, np.maximum(span[ci], 0))
    s_from = w_start[ci] + np.floor(crr.u("dos") * (span[ci] - los + 1)).astype("timedelta64[D]")
    s_to = s_from + los.astype("timedelta64[D]")
    received = np.minimum(s_to + crr.integers("recv_lag", 1, 8).astype("timedelta64[D]"), ctx.as_of)
    adjud = received + crr.integers("adj_lag", 1, 6).astype("timedelta64[D]")
    in_process = adjud > ctx.as_of
    emergency = np.where(ctype == "FACILITY", inpatient & crr.bernoulli("er", 0.35), (ctype == "PROFESSIONAL") & crr.bernoulli("er_p", 0.05))

    plan_ids = m["plan_id"][ci]
    in_net = crr.bernoulli("in_network", 0.92) | np.isin(scen, ["PCS-02", "PCS-03", "PCS-04"])
    role = np.where(ctype == "PHARMACY", "PHARMACY", np.where(ctype == "FACILITY",
                    np.where(inpatient, "HOSPITAL", "POSTACUTE"), crr.choice("prof_role", ["PCP", "SPECIALIST"], [60, 40])))
    billing = np.empty(n, dtype=object)
    for r in np.unique(role) if n else []:
        mask = role == r
        billing[mask], in_net[mask] = pick_provider(inp, plan_ids[mask], r, in_net[mask], crr.u(f"billing:{r}")[mask],
                                                    with_network=True)
    rendering = np.where(ctype == "PROFESSIONAL", billing, None)
    rendering = np.where(scen == "PCS-03", None, rendering)  # CO-16: missing rendering NPI
    facility = np.where(ctype == "FACILITY", billing, None)
    attending = pick_provider(inp, plan_ids, "SPECIALIST", in_net, crr.u("attending"))
    rendering = np.where(ctype == "FACILITY", attending, rendering)
    network = np.where(in_net, "IN", np.where(emergency, "OON_EMERGENCY", "OON"))

    # status: scenario recipes first, then baseline denial/pend rates
    denial_rate = float(ctx.bp.data_spec.distributions.get("denial_rate", 0.10))
    deny = (ctype != "PHARMACY") & crr.bernoulli("deny", denial_rate)
    pend = crr.bernoulli("pend", 0.05)
    status = np.where(deny, "DENIED", np.where(pend, "PENDED", "PAID")).astype(object)
    edge = np.isin(scen, ["PCS-02", "PCS-03", "PCS-04"])
    status[scen == "PCS-02"] = "PENDED"
    status[scen == "PCS-03"] = "DENIED"
    status[scen == "PCS-04"] = "PAID"
    status[in_process] = "RECEIVED"
    scen = np.where(in_process | ((scen == "PCS-01") & (status != "PAID")), "BASE", scen)
    edge = edge & ~in_process
    denial_cat = np.where(scen == "PCS-03", "MISSING_INFO",
                          crr.choice("denial_cat", *catalog.BASELINE_DENIALS)).astype(object)
    paid_dt = np.where(status == "PAID", np.minimum(adjud + crr.integers("pay_lag", 1, 6).astype("timedelta64[D]"), ctx.as_of), NAT)
    adjud = np.where(np.isin(status, ["PAID", "DENIED"]), adjud, NAT)

    # ---- lines
    n_lines = np.where(ctype == "PHARMACY", 1, np.where(ctype == "FACILITY",
                       np.minimum(2 + crr.poisson("n_fac_lines", 1.5), 6), np.minimum(1 + crr.poisson("n_prof_lines", 1.3), 4)))
    li = np.repeat(np.arange(n), n_lines)
    ln = np.concatenate([np.arange(c) for c in n_lines]) if n else np.array([], np.int64)
    lkey = ckey[li] * 8 + ln
    lrr = ctx.rr("claim_line", lkey)
    needs_pa_prof = (ctype == "PROFESSIONAL") & ~edge & ~emergency & crr.bernoulli("pa_prof", 0.06)
    needs_pa_fac = inpatient & ~emergency & crr.bernoulli("pa_fac", 0.25)
    cpt = np.empty(len(li), dtype=object)
    lt = ctype[li]
    first = ln == 0
    cpt[(lt == "PROFESSIONAL") & first] = lrr.choice("office", OFFICE)[(lt == "PROFESSIONAL") & first]
    cpt[(lt == "PROFESSIONAL") & ~first] = lrr.choice("ancillary", ANCILLARY)[(lt == "PROFESSIONAL") & ~first]
    pa_line = first & needs_pa_prof[li]
    cpt[pa_line] = lrr.choice("pa_code", PROF_PA)[pa_line]
    fac = lt == "FACILITY"
    cpt[fac] = lrr.choice("inpatient", INPATIENT)[fac]
    cpt[fac & first & inpatient[li]] = "99223"
    cpt[fac & (ln == 1) & inpatient[li]] = "99238"
    cpt[fac & first & needs_pa_fac[li]] = "27447"
    units = np.ones(len(li), dtype=np.int64)
    units[cpt == "99232"] = np.maximum(los[li][cpt == "99232"] - 1, 1)

    # pharmacy detail inputs
    ph = np.where(ctype == "PHARMACY")[0]
    drug_i = crr.integers("drug", 0, len(DRUGS_NO_PA))
    tier = np.array([DRUGS_NO_PA[d][2] for d in drug_i], dtype=np.int64)

    rate_cents = np.array([CPT[c][2] * 100 if c else 0 for c in cpt], dtype=np.int64) * units
    drug_cents = np.round(np.array([DRUG_MEDIAN_CENTS[t] for t in tier]) * np.exp(crr.normal("drug_cost", 0, 0.3))).astype(np.int64)
    allowed_full = np.where(lt == "PHARMACY", drug_cents[li], np.where(network[li] == "IN", rate_cents, (rate_cents * 0.8).astype(np.int64)))
    billed = np.round(allowed_full * np.where(lt == "PHARMACY", 1.1, 1.4 + lrr.u("markup") * 1.2)).astype(np.int64)

    paid_line = status[li] == "PAID"
    allowed = np.where(paid_line, allowed_full, 0)

    # member liability: deductible on the member's first claim only, copay on line 0 (prof/pharmacy), coinsurance otherwise
    plan_rows = [inp["plans"][p] for p in plan_ids]
    cents = lambda key: np.array([int(round(float(r[key]) * 100)) for r in plan_rows], dtype=np.int64)  # noqa: E731
    deductible_c, copay_pcp_c, copay_spec_c = cents("deductible"), cents("copay_pcp"), cents("copay_specialist")
    coins_pct = np.array([int(r["coinsurance_pct"]) for r in plan_rows], dtype=np.int64)
    ded = np.zeros(len(li), np.int64)
    ded_line = first & (cj[li] == 0) & paid_line
    ded[ded_line] = np.minimum(allowed[ded_line], deductible_c[li][ded_line])
    is_pcp = np.array([inp["ptype"].get(b) == "PCP" for b in billing], dtype=bool) if n else np.zeros(0, bool)
    copay_amt = np.where(ctype == "PHARMACY", np.array([catalog.TIER_COPAY_USD[t] * 100 for t in tier], dtype=np.int64),
                         np.where(is_pcp, copay_pcp_c, copay_spec_c))
    copay = np.zeros(len(li), np.int64)
    cp_line = first & paid_line & np.isin(lt, ["PROFESSIONAL", "PHARMACY"])
    copay[cp_line] = np.minimum(allowed[cp_line] - ded[cp_line], copay_amt[li][cp_line])
    coins = np.zeros(len(li), np.int64)
    co_line = paid_line & ~cp_line
    coins[co_line] = ((allowed[co_line] - ded[co_line]) * coins_pct[li][co_line]) // 100
    paid = allowed - ded - copay - coins

    line_status = np.where(status[li] == "PAID", "PAID", np.where(status[li] == "DENIED", "DENIED", "PENDED"))

    def per_claim(x: np.ndarray) -> np.ndarray:
        return np.bincount(li, weights=x, minlength=n).astype(np.int64)

    tot_billed, tot_allowed, tot_paid = per_claim(billed), per_claim(allowed), per_claim(paid)
    member_resp = per_claim(ded + copay + coins)

    return dict(midx=midx, m=m, ci=ci, cj=cj, ckey=ckey, crr=crr, scen=scen, ctype=ctype, inpatient=inpatient,
                s_from=s_from, s_to=s_to, received=received, adjud=adjud, paid_dt=paid_dt, status=status,
                emergency=emergency, plan_ids=plan_ids, billing=billing, rendering=rendering, facility=facility,
                network=network, denial_cat=denial_cat, needs_pa=needs_pa_prof | needs_pa_fac,
                li=li, ln=ln, lkey=lkey, cpt=cpt, units=units, billed=billed, allowed=allowed, paid=paid,
                ded=ded, copay=copay, coins=coins, line_status=line_status, ph=ph, drug_i=drug_i, tier=tier,
                tot_billed=tot_billed, tot_allowed=tot_allowed, tot_paid=tot_paid, member_resp=member_resp)


# ------------------------------------------------------------------ authorizations
def gen_auths(ctx: GenContext, inp: dict, c: dict) -> dict:
    """Claim-linked APPROVED auths (R-03/T-04) plus standalone auths at the D-06 outcome mix."""
    midx, m = c["midx"], c["m"]
    # linked: one per claim with a PA-required line that is not an emergency
    lk = np.where(c["needs_pa"])[0]
    # standalone
    rr = ctx.rr("auth_request:count", midx)
    vol = ctx.bp.data_spec.volumes
    k = np.minimum(rr.poisson("n_auths", vol.authorizations / max(vol.members, 1) * 0.5), 5)
    si = np.repeat(np.arange(len(midx)), k)
    sj = np.concatenate([np.arange(x) for x in k]) if len(si) else np.array([], np.int64)

    a_member = np.concatenate([c["ci"][lk], si])
    a_key = np.concatenate([c["ckey"][lk], midx[si] * 64 + 32 + sj])
    linked = np.concatenate([np.ones(len(lk), bool), np.zeros(len(si), bool)])
    n = len(a_key)
    arr = ctx.rr("auth_request", a_key)
    scen = np.where(linked, "BASE", ctx.scenarios("auth", arr)).astype(object)
    # happy path PAS-01 = a routine standard review; other baseline rows keep the D-06 outcome mix
    outcome = arr.choice("outcome", *catalog.AUTH_OUTCOMES).astype(object)
    scen[(scen == "PAS-01") & (outcome != "IN_REVIEW")] = "BASE"

    stype = arr.choice("stype", ["OUTPATIENT", "INPATIENT", "DME", "HOME_HEALTH", "SNF", "PHARMACY", "BH"], [45, 10, 10, 10, 10, 10, 5]).astype(object)
    stype[linked] = np.where(c["ctype"][lk] == "FACILITY", "INPATIENT", "OUTPATIENT")
    stype[scen == "PAS-02"] = "SNF"
    stype[scen == "PAS-03"] = "OUTPATIENT"
    urgent = arr.bernoulli("urgent", 0.15) & ~linked
    urgent[scen == "PAS-02"] = True
    urgent[np.isin(scen, ["PAS-01", "PAS-03"])] = False

    recv_day = ctx.as_of - arr.integers("recv_back", 0, 60).astype("timedelta64[D]")
    recv_day[linked] = c["s_from"][lk] - arr.integers("lead", 5, 26)[linked].astype("timedelta64[D]")
    recv_day[scen == "PAS-01"] = ctx.as_of - 5 * DAY
    recv_day[scen == "PAS-02"] = ctx.as_of - 1 * DAY
    recv_day[scen == "PAS-03"] = ctx.as_of - 3 * DAY
    received = recv_day.astype("datetime64[s]") + (8 + arr.integers("recv_hour", 0, 9)).astype("timedelta64[h]")
    due = received + np.where(urgent, 72, 7 * 24).astype("timedelta64[h]")

    outcome[linked] = "APPROVED"
    outcome[np.isin(scen, ["PAS-02", "PAS-03"])] = "PENDED_INFO"
    outcome[scen == "PAS-04"] = "DENIED"
    decided = np.isin(outcome, ["APPROVED", "PARTIAL", "DENIED"])
    det_dt = received + (arr.u("det_at") * 0.95 * (due - received).astype(np.int64)).astype("timedelta64[s]")
    det_dt[linked] = received[linked] + arr.integers("det_lag", 1, 4)[linked].astype("timedelta64[D]")
    late = decided & (det_dt > ctx.as_of.astype("datetime64[s]") + np.timedelta64(23, "h"))
    outcome[late] = "IN_REVIEW"
    decided &= ~late
    status = outcome
    # PAS-01 tags every row that satisfies its recipe: standard review still open, no documents pending
    routine = ~linked & (status == "IN_REVIEW") & ~urgent & np.isin(scen, ["BASE", "PAS-01"])
    scen = np.where(routine, "PAS-01", np.where(scen == "PAS-01", "BASE", scen))

    # codes per auth (line 0)
    cpt0 = arr.choice("cpt", catalog.PA_CODES).astype(object)
    claim_line0 = {int(ck): cp for ck, cp, l in zip(c["ckey"][c["li"]], c["cpt"], c["ln"]) if l == 0}
    cpt0[linked] = [claim_line0[int(k)] for k in c["ckey"][lk]]
    cpt0[scen == "PAS-03"] = "97161"

    plan_ids = m["plan_id"][a_member]
    member_ids = m["member_id"][a_member]
    requesting = np.array([inp["pcp"].get(mid) for mid in member_ids], dtype=object)
    fallback = pick_provider(inp, plan_ids, "PCP", np.ones(n, bool), arr.u("req"))
    requesting = np.where(requesting == None, fallback, requesting)  # noqa: E711
    servicing = pick_provider(inp, plan_ids, "SPECIALIST", np.ones(n, bool), arr.u("svc"))
    servicing[linked] = c["billing"][lk]
    fac_role = np.where(stype == "INPATIENT", "HOSPITAL", "POSTACUTE")
    facility = np.where(np.isin(stype, ["INPATIENT", "SNF", "HOME_HEALTH"]), None, None).astype(object)
    for r in ("HOSPITAL", "POSTACUTE"):
        mask = np.isin(stype, ["INPATIENT", "SNF", "HOME_HEALTH"]) & (fac_role == r)
        facility[mask] = pick_provider(inp, plan_ids[mask], r, np.ones(mask.sum(), bool), arr.u(f"fac:{r}")[mask])
    persona = np.where(np.isin(stype, ["SNF", "INPATIENT", "HOME_HEALTH"]), "DISCHARGE",
                       arr.choice("persona", ["OFFICE", "REFERRAL"], [50, 50]))

    auth_ids = np.asarray(ctx.ids("auth_request", a_key), dtype=object)
    auth_no = np.array([f"AUTH-{int(k // 64) + 1:07d}-{int(k % 64):02d}" for k in a_key], dtype=object)

    ctx.write("auth_request", {
        "auth_id": pa.array(auth_ids.tolist()),
        "auth_number": pa.array(auth_no.tolist()),
        "member_id": pa.array(member_ids.tolist()),
        "requesting_npi": pa.array(requesting.tolist()),
        "servicing_npi": pa.array(servicing.tolist()),
        "facility_npi": pa.array(facility.tolist(), pa.string()),
        "plan_id": pa.array(plan_ids.tolist()),
        "auth_type": pa.array(["PRE_SERVICE"] * n),
        "urgency": pa.array(np.where(urgent, "URGENT", "STANDARD").tolist()),
        "service_type": pa.array(stype.tolist()),
        "primary_dx": pa.array(arr.choice("dx", catalog.ICD10).tolist()),
        "received_dt": stamps(received),
        "decision_due_dt": stamps(due),
        "status": pa.array(status.tolist()),
        "channel_received": pa.array(arr.choice("chan", ["PORTAL", "FAX", "PHONE", "CHAT", "EDI"], [50, 25, 10, 5, 10]).tolist()),
        "submitted_by_persona": pa.array(persona.tolist()),
    }, scenario=scen)

    # auth lines
    n_al = 1 + arr.bernoulli("second_line", 0.3).astype(int)
    ai = np.repeat(np.arange(n), n_al)
    aj = np.concatenate([np.arange(x) for x in n_al]) if n else np.array([], np.int64)
    alrr = ctx.rr("auth_line", a_key[ai] * 4 + aj)
    req_units = alrr.integers("units", 1, 13)
    out_i = status[ai]
    appr = np.where(out_i == "APPROVED", req_units, np.where(out_i == "PARTIAL", np.maximum(req_units // 2, 1),
                    np.where(out_i == "DENIED", 0, -1)))
    l_status = np.where(out_i == "APPROVED", "APPROVED", np.where(out_i == "PARTIAL", "MODIFIED",
                        np.where(out_i == "DENIED", "DENIED", None)))
    ctx.write("auth_line", {
        "auth_line_id": pa.array(ctx.ids("auth_line", a_key[ai] * 4 + aj)),
        "auth_id": pa.array(auth_ids[ai].tolist()),
        "cpt_hcpcs_or_ndc": pa.array(np.where(aj == 0, cpt0[ai], alrr.choice("cpt2", ANCILLARY)).tolist()),
        "units_requested": pa.array(req_units),
        "units_approved": pa.array([None if x < 0 else int(x) for x in appr], pa.int64()),
        "site_of_service": pa.array(np.where(np.isin(stype[ai], ["INPATIENT"]), "21", np.where(stype[ai] == "SNF", "31", "22")).tolist()),
        "line_status": pa.array(l_status.tolist(), pa.string()),
    }, scenario=scen[ai])

    # determinations
    d = np.where(decided)[0]
    drr = ctx.rr("auth_determination", a_key[d])
    d_out = status[d]
    denied = d_out == "DENIED"
    valid_from = recv_day[d]
    valid_to = valid_from + 90 * DAY
    valid_to[linked[d]] = c["s_from"][lk] + 60 * DAY
    role = np.where(denied, drr.choice("md", ["MEDICAL_DIRECTOR", "SAME_SPECIALTY_PEER"], [70, 30]),
                    drr.choice("rn", ["NURSE", "MEDICAL_DIRECTOR"], [70, 30]))
    rationale = [f"Clinical documentation does not meet criteria for {CPT[cp][1].lower()} at this time." if den else None
                 for den, cp in zip(denied, cpt0[d])]
    ctx.write("auth_determination", {
        "determination_id": pa.array(ctx.ids("auth_determination", a_key[d])),
        "auth_id": pa.array(auth_ids[d].tolist()),
        "outcome": pa.array(d_out.tolist()),
        "valid_from": dates(np.where(denied, NAT, valid_from)),
        "valid_to": dates(np.where(denied, NAT, valid_to)),
        "denial_category": pa.array(np.where(denied, "MED_NECESSITY", None).tolist(), pa.string()),
        "denial_rationale": pa.array(rationale, pa.string()),
        "criteria_asset_id": pa.array([pa_policy_asset_id(ctx, cp) for cp in cpt0[d]]),
        "decided_by_role": pa.array(role.tolist()),
        "determination_dt": stamps(det_dt[d]),
        "appeal_rights_sent_dt": stamps(np.where(denied, det_dt[d], np.datetime64("NaT", "s"))),
    }, scenario=scen[d])

    # open reviews
    o = np.where(np.isin(status, ["IN_REVIEW", "PENDED_INFO", "SUBMITTED"]))[0]
    orr = ctx.rr("clinical_review_queue", a_key[o])
    ctx.write("clinical_review_queue", {
        "queue_id": pa.array(ctx.ids("clinical_review_queue", a_key[o])),
        "auth_id": pa.array(auth_ids[o].tolist()),
        "review_level": pa.array(orr.choice("level", ["NURSE", "MEDICAL_DIRECTOR"], [80, 20]).tolist()),
        "assigned_dt": stamps(received[o] + 2 * HOUR),
        "sla_due_dt": stamps(due[o]),
        "status": pa.array(np.where(status[o] == "PENDED_INFO", "WAITING", orr.choice("qs", ["WAITING", "IN_REVIEW"])).tolist()),
        "p2p_available_until": stamps(np.full(len(o), np.datetime64("NaT", "s"))),
    }, scenario=scen[o])

    # documentation: REQUESTED for pended auths; RECEIVED for 30% of decided ones
    doc_req = (status == "PENDED_INFO") | (decided & arr.bernoulli("had_docs", 0.3) & ~linked)
    q = np.where(doc_req)[0]
    qrr = ctx.rr("documentation_requirement", a_key[q])
    doc_type = qrr.choice("doc", ["CLINICAL_NOTES", "IMAGING", "LABS", "PT_EVAL", "PRESCRIBER_STATEMENT"]).astype(object)
    doc_type[scen[q] == "PAS-02"] = "DISCHARGE_SUMMARY"
    doc_type[scen[q] == "PAS-03"] = "PT_EVAL"
    pended = status[q] == "PENDED_INFO"
    req_dt = recv_day[q] + DAY
    ctx.write("documentation_requirement", {
        "req_id": pa.array(ctx.ids("documentation_requirement", a_key[q])),
        "auth_id": pa.array(auth_ids[q].tolist()),
        "doc_type": pa.array(doc_type.tolist()),
        "state": pa.array(np.where(pended, "REQUESTED", "RECEIVED").tolist()),
        "requested_dt": dates(req_dt),
        "due_dt": dates(req_dt + 14 * DAY),
        "clock_paused": pa.array(pended),
    }, scenario=scen[q])

    claim_auth = np.full(len(c["ckey"]), None, dtype=object)
    claim_auth[lk] = auth_ids[:len(lk)]
    return {"claim_auth": claim_auth}


# ------------------------------------------------------------------ writers for claim entities
def write_claims(ctx: GenContext, inp: dict, c: dict, claim_auth: np.ndarray) -> None:
    n = len(c["ckey"])
    claim_ids = np.asarray(ctx.ids("claim_header", c["ckey"]), dtype=object)
    member_ids = c["m"]["member_id"][c["ci"]]
    scen, status, ctype = c["scen"], c["status"], c["ctype"]
    crr = c["crr"]
    claim_no = [f"CLM-{int(k // 64) + 1:07d}-{int(k % 64):02d}" for k in c["ckey"]]

    ctx.write("claim_header", {
        "claim_id": pa.array(claim_ids.tolist()),
        "claim_number": pa.array(claim_no),
        "claim_type": pa.array(ctype.tolist()),
        "member_id": pa.array(member_ids.tolist()),
        "plan_id": pa.array(c["plan_ids"].tolist()),
        "billing_npi": pa.array(c["billing"].tolist()),
        "rendering_npi": pa.array(c["rendering"].tolist(), pa.string()),
        "facility_npi": pa.array(c["facility"].tolist(), pa.string()),
        "claim_frequency_code": pa.array(["1"] * n),
        "original_claim_id": pa.array([None] * n, pa.string()),
        "service_from_dt": dates(c["s_from"]),
        "service_to_dt": dates(c["s_to"]),
        "received_dt": dates(c["received"]),
        "adjudicated_dt": dates(c["adjud"]),
        "paid_dt": dates(c["paid_dt"]),
        "status": pa.array(status.tolist()),
        "network_status": pa.array(c["network"].tolist()),
        "is_emergency": pa.array(c["emergency"]),
        "auth_id": pa.array(claim_auth.tolist(), pa.string()),
        "bill_type": pa.array(np.where(ctype == "FACILITY", np.where(c["inpatient"], "0111", "0131"), None).tolist(), pa.string()),
        "drg_code": pa.array(np.where(c["inpatient"], crr.choice("drg", ["470", "291", "193", "871"]), None).tolist(), pa.string()),
        "primary_dx": pa.array(crr.choice("dx", catalog.ICD10).tolist()),
        "total_billed": money(c["tot_billed"]),
        "total_allowed": money(c["tot_allowed"]),
        "total_paid": money(c["tot_paid"]),
        "member_resp": money(c["member_resp"]),
    }, scenario=scen)

    li, ln = c["li"], c["ln"]
    line_ids = np.asarray(ctx.ids("claim_line", c["lkey"]), dtype=object)
    ctx.write("claim_line", {
        "line_id": pa.array(line_ids.tolist()),
        "claim_id": pa.array(claim_ids[li].tolist()),
        "line_no": pa.array((ln + 1).astype(np.int32)),
        "cpt_hcpcs": pa.array(c["cpt"].tolist(), pa.string()),
        "revenue_code": pa.array(np.where(ctype[li] == "FACILITY", "0120", None).tolist(), pa.string()),
        "modifier": pa.array([None] * len(li), pa.string()),
        "units": pa.array(c["units"]),
        "billed": money(c["billed"]),
        "allowed": money(c["allowed"]),
        "paid": money(c["paid"]),
        "deductible": money(c["ded"]),
        "copay": money(c["copay"]),
        "coinsurance": money(c["coins"]),
        "line_status": pa.array(c["line_status"].tolist()),
    }, scenario=scen[li])

    # pharmacy detail (R-08: exactly one per pharmacy claim)
    ph = c["ph"]
    drugs = [DRUGS_NO_PA[i] for i in c["drug_i"][ph]]
    days = crr.choice("days_supply", [30, 90], [80, 20])[ph].astype(np.int64)
    ctx.write("pharmacy_claim_detail", {
        "claim_id": pa.array(claim_ids[ph].tolist()),
        "ndc": pa.array([d[0] for d in drugs]),
        "drug_name": pa.array([d[1] for d in drugs]),
        "quantity": pa.array(days),
        "days_supply": pa.array(days),
        "pharmacy_npi": pa.array(c["billing"][ph].tolist()),
        "fill_dt": dates(c["s_from"][ph]),
        "formulary_tier": pa.array(c["tier"][ph].astype(np.int32)),
        "pa_required": pa.array([d[3] for d in drugs], pa.bool_()),
        "copay_applied": money(np.array([catalog.TIER_COPAY_USD[t] * 100 for t in c["tier"][ph]], dtype=np.int64)),
        "reject_code": pa.array([None] * len(ph), pa.string()),
    }, scenario=scen[ph])

    # denials (claim level, anchored on line 1)
    dn = np.where(status == "DENIED")[0]
    first_line = {int(ci): lid for ci, lid, l in zip(li, line_ids, ln) if l == 0}
    cat = c["denial_cat"][dn]
    lob = c["m"]["lob"][c["ci"][dn]]
    appealable = cat != "DUPLICATE"
    deadline = c["adjud"][dn] + np.where(lob == "COMMERCIAL", 180, 60).astype("timedelta64[D]")
    ctx.write("claim_denial", {
        "denial_id": pa.array(ctx.ids("claim_denial", c["ckey"][dn])),
        "claim_id": pa.array(claim_ids[dn].tolist()),
        "line_id": pa.array([first_line[int(i)] for i in dn]),
        "carc": pa.array([f"CO-{catalog.DENIAL_CATEGORIES[x][0]}" for x in cat]),
        "rarc": pa.array([None] * len(dn), pa.string()),
        "denial_category": pa.array(cat.tolist()),
        "reason_text": pa.array([catalog.DENIAL_CATEGORIES[x][1] for x in cat]),
        "liable_party": pa.array(["PROVIDER"] * len(dn)),
        "denial_dt": dates(c["adjud"][dn]),
        "appealable": pa.array(appealable),
        "appeal_deadline_dt": dates(np.where(appealable, deadline, NAT)),
    }, scenario=scen[dn])

    # pends
    pd_ = np.where(status == "PENDED")[0]
    prr = ctx.rr("claim_pend", c["ckey"][pd_])
    reason = prr.choice("reason", ["RECORDS_REQUEST", "COB_INFO", "CLINICAL_REVIEW", "PRICING"], [40, 25, 20, 15]).astype(object)
    reason[scen[pd_] == "PCS-02"] = "RECORDS_REQUEST"
    pend_dt = np.minimum(c["received"][pd_] + prr.integers("pend_lag", 0, 4).astype("timedelta64[D]"), ctx.as_of)
    ctx.write("claim_pend", {
        "pend_id": pa.array(ctx.ids("claim_pend", c["ckey"][pd_])),
        "claim_id": pa.array(claim_ids[pd_].tolist()),
        "pend_reason": pa.array(reason.tolist()),
        "pend_dt": dates(pend_dt),
        "expected_resolution_dt": dates(pend_dt + prr.integers("res", 10, 31).astype("timedelta64[D]")),
        "info_needed_from": pa.array(np.where(reason == "COB_INFO", "MEMBER", np.where(reason == "PRICING", "PAYER", "PROVIDER")).tolist()),
        "resolved_dt": dates(np.full(len(pd_), NAT)),
    }, scenario=scen[pd_])

    # adjustments (F-05): paid lines get CO-45 / PR-1 / PR-2 / PR-3; denied lines get CO-<carc> = billed
    adj_line, adj_code, adj_amt, adj_slot = [], [], [], []
    paid_l = c["line_status"] == "PAID"
    den_l = c["line_status"] == "DENIED"
    for slot, code, amt, mask in (
        (0, "CO-45", c["billed"] - c["allowed"], paid_l),
        (1, "PR-1", c["ded"], paid_l),
        (2, "PR-2", c["coins"], paid_l),
        (3, "PR-3", c["copay"], paid_l),
    ):
        sel = np.where(mask & (amt > 0))[0]
        adj_line.append(sel); adj_code.append(np.full(len(sel), code, dtype=object)); adj_amt.append(amt[sel]); adj_slot.append(np.full(len(sel), slot))
    sel = np.where(den_l)[0]
    adj_line.append(sel)
    adj_code.append(np.array([f"CO-{catalog.DENIAL_CATEGORIES[x][0]}" for x in c["denial_cat"][li[sel]]], dtype=object))
    adj_amt.append(c["billed"][sel]); adj_slot.append(np.full(len(sel), 4))
    al = np.concatenate(adj_line).astype(np.int64)
    acode = np.concatenate(adj_code) if al.size else np.array([], dtype=object)
    akey = c["lkey"][al] * 8 + np.concatenate(adj_slot)
    order = np.argsort(akey, kind="stable")
    al, acode, akey, aamt = al[order], acode[order], akey[order], np.concatenate(adj_amt)[order]
    ctx.write("claim_adjustment", {
        "adjustment_id": pa.array(ctx.ids("claim_adjustment", akey)),
        "line_id": pa.array(line_ids[al].tolist()),
        "group_code": pa.array([x.split("-")[0] for x in acode], pa.string()),
        "carc": pa.array(acode.tolist(), pa.string()),
        "amount": money(aamt),
        "adjustment_dt": dates(c["adjud"][li[al]]),
    }, scenario=scen[li[al]])

    # payments
    py = np.where(status == "PAID")[0]
    yrr = ctx.rr("claim_payment", c["ckey"][py])
    ctx.write("claim_payment", {
        "payment_id": pa.array(ctx.ids("claim_payment", c["ckey"][py])),
        "claim_id": pa.array(claim_ids[py].tolist()),
        "payment_dt": dates(c["paid_dt"][py]),
        "method": pa.array(yrr.choice("method", ["EFT", "CHECK", "VCARD"], [80, 15, 5]).tolist()),
        "paid_amount": money(c["tot_paid"][py]),
        "trace_no": pa.array([f"TRN{int(k):012d}" for k in c["ckey"][py]]),
        "payee_tin": pa.array([inp["tin"][b] for b in c["billing"][py]]),
        "recoup_flag": pa.array(np.zeros(len(py), bool)),
        "offset_ref": pa.array([None] * len(py), pa.string()),
    }, scenario=scen[py])

    # appeals: 12% of appealable denials, filed before as_of; decided ones are UPHELD in v1
    ap = dn[appealable & crr.bernoulli("appeal", 0.12)[dn]]
    arr = ctx.rr("appeal_record", c["ckey"][ap])
    filed = c["adjud"][ap] + arr.integers("file_lag", 3, 26).astype("timedelta64[D]")
    ok = filed <= ctx.as_of
    ap, filed = ap[ok], filed[ok]
    arr = ctx.rr("appeal_record", c["ckey"][ap])
    ast = arr.choice("status", ["OPEN", "IN_REVIEW", "DECIDED"], [40, 40, 20])
    dec_dt = np.minimum(filed + arr.integers("dec_lag", 5, 30).astype("timedelta64[D]"), ctx.as_of)
    lob_ap = c["m"]["lob"][c["ci"][ap]]
    ctx.write("appeal_record", {
        "appeal_id": pa.array(ctx.ids("appeal_record", c["ckey"][ap])),
        "appeal_number": pa.array([f"APL-{int(k // 64) + 1:07d}-{int(k % 64):02d}" for k in c["ckey"][ap]]),
        "claim_id": pa.array(claim_ids[ap].tolist()),
        "filed_by": pa.array(["PROVIDER"] * len(ap)),
        "level": pa.array(["L1"] * len(ap)),
        "filed_dt": dates(filed),
        "deadline_dt": dates(c["adjud"][ap] + np.where(lob_ap == "COMMERCIAL", 180, 60).astype("timedelta64[D]")),
        "status": pa.array(ast.tolist()),
        "outcome": pa.array(np.where(ast == "DECIDED", "UPHELD", None).tolist(), pa.string()),
        "decision_dt": dates(np.where(ast == "DECIDED", dec_dt, NAT)),
        "documents_received": pa.array([["clinical_notes"] if s != "OPEN" else [] for s in ast], pa.list_(pa.string())),
    }, scenario=scen[ap])

    # reprocessing: none in v1 (no overturned appeals yet); table created with its schema
    ctx.write("reprocessing_history", {
        "reprocess_id": pa.array([], pa.string()), "claim_id": pa.array([], pa.string()),
        "new_claim_id": pa.array([], pa.string()), "trigger": pa.array([], pa.string()),
        "reprocess_dt": pa.array([], pa.timestamp("s")), "paid_before": money(np.array([], np.int64)),
        "paid_after": money(np.array([], np.int64)), "performed_by": pa.array([], pa.string()),
    })

    # accumulators (F-03): PR liability on paid claims, per member, plan year, network
    if ctx.wants("member_accumulator"):
        pl = np.where(paid_l)[0]
        cl = li[pl]
        net = np.where(c["network"][cl] == "IN", 0, 1)
        gkey = c["midx"][c["ci"][cl]] * 2 + net
        uniq, inv = np.unique(gkey, return_inverse=True)
        ded_sum = np.bincount(inv, weights=c["ded"][pl]).astype(np.int64)
        oop_sum = np.bincount(inv, weights=(c["ded"] + c["copay"] + c["coins"])[pl]).astype(np.int64)
        first_claim = np.zeros(len(uniq), dtype=np.int64)
        first_claim[inv] = cl
        ctx.write("member_accumulator", {
            "accumulator_id": pa.array(ctx.ids("member_accumulator", uniq)),
            "member_id": pa.array(member_ids[first_claim].tolist()),
            "plan_year": pa.array(np.full(len(uniq), int(str(ctx.as_of)[:4]))),
            "network": pa.array(np.where(uniq % 2 == 0, "IN", "OON").tolist()),
            "deductible_met": money(ded_sum),
            "oop_met": money(oop_sum),
        })


def run(ctx: GenContext) -> list[str]:
    inp = load_inputs(ctx)
    n_members = len(inp["members"]["member_id"])
    for lo, hi in ctx.shard_ranges(n_members):
        c = gen_claims(ctx, inp, slice(lo, hi))
        auth = gen_auths(ctx, inp, c) if ctx.wants("auth_request") else {"claim_auth": np.full(len(c["ckey"]), None, dtype=object)}
        write_claims(ctx, inp, c, auth["claim_auth"])
    return [e for e in catalog.STAGE_ENTITIES["s5"] if ctx.wants(e)]
