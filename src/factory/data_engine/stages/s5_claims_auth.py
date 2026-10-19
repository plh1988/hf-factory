"""Stage s5: claims (all claim entities), authorizations, and member accumulators derived from paid claims.

Sharded by member index: each member's claims and auths come from that member's own row streams.
Rules enforced while generating: R-02 coverage, R-03 auth for PA lines, R-04 network, R-08 pharmacy
detail, T-02 date order, T-03 auth clocks, T-04 auth covers DOS, F-01/F-02/F-05 money math, F-03 accumulators.
"""

from __future__ import annotations

import numpy as np
import pyarrow as pa

from factory.codes import (
    AppealOutcome, AppealStatus, AuthLineStatus, AuthStatus, ClaimStatus, ClaimType, DenialCategory,
    FacilityType, Lob, NetworkStatus, ProviderRole, ProviderType, ServiceType,
)
from factory.data_engine import catalog, keys
from factory.data_engine.context import GenContext, dates, stamps
from factory.data_engine.knowledge_catalog import pa_policy_asset_id
from factory.data_engine.rng import money

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
    """Read everything claims generation needs from the TableStore once per stage.

    Returns members with their latest enrollment, plan terms, provider pools per (plan, role) for in-network picks,
    an out-of-network pool per role, provider types and TINs, and each member's active PCP (requesting provider
    for authorizations).
    """
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
        if ptype in (ProviderType.PCP, ProviderType.SPECIALIST):
            return ptype
        return {FacilityType.PHARMACY: ProviderRole.PHARMACY, FacilityType.HOSPITAL: ProviderRole.HOSPITAL}.get(ftype or "", ProviderRole.POSTACUTE)

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
            pool = inp["oon"].get(role) or inp["oon"][ProviderRole.PCP]
        out[i] = pool[min(int(x * len(pool)), len(pool) - 1)]
    return (out, actual) if with_network else out


def to_date(a) -> np.ndarray:
    """Python dates (None allowed) -> numpy datetime64[D] with NaT for missing values."""
    return np.array([np.datetime64(x, "D") if x is not None else NAT for x in a], dtype="datetime64[D]")


# ------------------------------------------------------------------ claims
def gen_claims(ctx: GenContext, inp: dict, sl: slice) -> dict:
    """Claims and claim lines for one shard of members, as parallel numpy arrays (written later by write_claims).

    Order of work (each step only uses the ones before it):
      1. how many claims each member has (Poisson around claims/members), inside their coverage window (R-02)
      2. claim type, dates of service, received/adjudicated dates (T-02), emergency flag
      3. billing/rendering/facility providers from the plan's participating pools (R-04)
      4. status: baseline denial/pend draws, then scenario recipes override them
      5. lines and procedure codes; PA-required codes are flagged so s5 can create the auth first (R-03)
      6. money in integer cents: allowed, billed, deductible, copay, coinsurance, paid (F-01, F-02)

    Arrays: `ci` maps claim -> member position in the shard, `cj` is the claim's number for that member,
    `li` maps line -> claim position, `ln` is the line number. Keys: claim = member*64 + j, line = claim*8 + n.
    """
    m = {k: v[sl] for k, v in inp["members"].items()}
    midx = np.array([int(x[4:]) - 1 for x in m["member_number"]], dtype=np.int64)
    rr = ctx.rr("claim_header:count", midx)
    eff, term = to_date(m["effective_dt"]), to_date(m["term_dt"])
    hist_start = ctx.as_of - np.timedelta64(int(ctx.spec.history_months or 12) * 31, "D")
    w_start = np.maximum(eff, hist_start)
    w_end = np.minimum(np.where(np.isnat(term), ctx.as_of, term), ctx.as_of - 3 * DAY)
    span = (w_end - w_start).astype(int)
    lam = ctx.spec.volumes["claims"] / max(len(inp["members"]["member_id"]), 1)
    k = np.where(span >= 0, np.minimum(rr.poisson("n_claims", lam), 20), 0)

    # ---- claims: expand members into claim rows (ci = member position, cj = claim number for that member)
    ci = np.repeat(np.arange(len(midx)), k)
    cj = np.concatenate([np.arange(c) for c in k]) if len(ci) else np.array([], np.int64)
    ckey = keys.claim_key(midx[ci], cj)  # stable per member and claim number (see keys.py)
    n = len(ckey)
    crr = ctx.rr("claim_header", ckey)
    scen = ctx.scenarios("claim", crr)
    ctype = crr.choice("type", [ClaimType.PHARMACY, ClaimType.PROFESSIONAL, ClaimType.FACILITY], [40, 45, 15])
    # forced scenarios (e.g. PCS-02, MCI-02, PAP-02) need a clean professional, in-network, non-emergency claim
    forced = np.isin(scen, sorted(catalog.FORCED_CLAIM_SCENARIOS))
    ctype = np.where(forced, ClaimType.PROFESSIONAL, ctype)
    inpatient = (ctype == ClaimType.FACILITY) & crr.bernoulli("inpatient", 0.6)
    los = np.where(inpatient, crr.integers("los", 2, 7), 0)
    los = np.minimum(los, np.maximum(span[ci], 0))
    s_from = w_start[ci] + np.floor(crr.u("dos") * (span[ci] - los + 1)).astype("timedelta64[D]")
    early = np.isin(scen, ["PAP-02", "PAP-03", "PAP-04"])  # appeals need time to be filed or decided
    s_from = np.where(early, w_start[ci], s_from)
    s_to = s_from + los.astype("timedelta64[D]")
    # T-02: service <= received <= adjudicated <= paid, never after as_of; claims not yet adjudicated stay RECEIVED
    received = np.minimum(s_to + crr.integers("recv_lag", 1, 8).astype("timedelta64[D]"), ctx.as_of)
    adjud = received + crr.integers("adj_lag", 1, 6).astype("timedelta64[D]")
    in_process = adjud > ctx.as_of
    emergency = np.where(ctype == ClaimType.FACILITY, inpatient & crr.bernoulli("er", 0.35), (ctype == ClaimType.PROFESSIONAL) & crr.bernoulli("er_p", 0.05))
    emergency &= ~forced

    # ---- providers: 92% in network; pick_provider reports the truth if the plan has no participating provider (R-04)
    plan_ids = m["plan_id"][ci]
    in_net = crr.bernoulli("in_network", 0.92) | forced
    role = np.where(ctype == ClaimType.PHARMACY, ProviderRole.PHARMACY, np.where(ctype == ClaimType.FACILITY,
                    np.where(inpatient, ProviderRole.HOSPITAL, ProviderRole.POSTACUTE), crr.choice("prof_role", [ProviderRole.PCP, ProviderRole.SPECIALIST], [60, 40])))
    billing = np.empty(n, dtype=object)
    for r in np.unique(role) if n else []:
        mask = role == r
        billing[mask], in_net[mask] = pick_provider(inp, plan_ids[mask], r, in_net[mask], crr.u(f"billing:{r}")[mask],
                                                    with_network=True)
    rendering = np.where(ctype == ClaimType.PROFESSIONAL, billing, None)
    rendering = np.where(scen == "PCS-03", None, rendering)  # CO-16: missing rendering NPI
    facility = np.where(ctype == ClaimType.FACILITY, billing, None)
    attending = pick_provider(inp, plan_ids, ProviderRole.SPECIALIST, in_net, crr.u("attending"))
    rendering = np.where(ctype == ClaimType.FACILITY, attending, rendering)
    network = np.where(in_net, NetworkStatus.IN, np.where(emergency, NetworkStatus.OON_EMERGENCY, NetworkStatus.OON))

    # status: scenario recipes first, then baseline denial/pend rates
    denial_rate = float(ctx.spec.distributions.get("denial_rate", 0.10))
    deny = (ctype != ClaimType.PHARMACY) & crr.bernoulli("deny", denial_rate)
    pend = crr.bernoulli("pend", 0.05)
    status = np.where(deny, ClaimStatus.DENIED, np.where(pend, ClaimStatus.PENDED, ClaimStatus.PAID)).astype(object)
    denial_cat = crr.choice("denial_cat", *catalog.BASELINE_DENIALS).astype(object)
    pend_reason = np.full(n, None, dtype=object)
    for sid in catalog.FORCED_CLAIM_SCENARIOS:  # scenario recipes override the baseline draws
        mask = scen == sid
        if not mask.any():
            continue
        recipe = catalog.SCENARIOS[sid]
        status[mask] = recipe["claim_status"]
        if "denial_category" in recipe:
            denial_cat[mask] = recipe["denial_category"]
        if "pend_reason" in recipe:
            pend_reason[mask] = recipe["pend_reason"]
    status[in_process] = ClaimStatus.RECEIVED
    required = np.array([catalog.SCENARIOS[x]["claim_status"] if x in catalog.HAPPY_CLAIM_SCENARIOS else "" for x in scen], dtype=object)
    # happy-path tags are not forced: they stay only on claims that ended in the recipe's status (keeps the denial rate)
    happy_unmet = np.isin(scen, sorted(catalog.HAPPY_CLAIM_SCENARIOS)) & (status != required)
    happy_unmet |= (scen == "PAP-01") & (np.isin(denial_cat, [DenialCategory.DUPLICATE, DenialCategory.AUTH]) | (ctype == ClaimType.PHARMACY))
    scen = np.where(in_process | happy_unmet, "BASE", scen)
    edge = forced & ~in_process
    paid_dt = np.where(status == ClaimStatus.PAID, np.minimum(adjud + crr.integers("pay_lag", 1, 6).astype("timedelta64[D]"), ctx.as_of), NAT)
    adjud = np.where(np.isin(status, [ClaimStatus.PAID, ClaimStatus.DENIED]), adjud, NAT)

    # ---- lines
    n_lines = np.where(ctype == ClaimType.PHARMACY, 1, np.where(ctype == ClaimType.FACILITY,
                       np.minimum(2 + crr.poisson("n_fac_lines", 1.5), 6), np.minimum(1 + crr.poisson("n_prof_lines", 1.3), 4)))
    li = np.repeat(np.arange(n), n_lines)
    ln = np.concatenate([np.arange(c) for c in n_lines]) if n else np.array([], np.int64)
    lkey = keys.line_key(ckey[li], ln)
    lrr = ctx.rr("claim_line", lkey)
    needs_pa_prof = (ctype == ClaimType.PROFESSIONAL) & ~edge & ~emergency & crr.bernoulli("pa_prof", 0.06)
    needs_pa_fac = inpatient & ~emergency & crr.bernoulli("pa_fac", 0.25)
    cpt = np.empty(len(li), dtype=object)
    lt = ctype[li]
    first = ln == 0
    cpt[(lt == ClaimType.PROFESSIONAL) & first] = lrr.choice("office", OFFICE)[(lt == ClaimType.PROFESSIONAL) & first]
    cpt[(lt == ClaimType.PROFESSIONAL) & ~first] = lrr.choice("ancillary", ANCILLARY)[(lt == ClaimType.PROFESSIONAL) & ~first]
    pa_line = first & (needs_pa_prof[li] | (scen[li] == "MCI-02"))  # MCI-02: PA code billed without an auth
    cpt[pa_line] = lrr.choice("pa_code", PROF_PA)[pa_line]
    fac = lt == ClaimType.FACILITY
    cpt[fac] = lrr.choice("inpatient", INPATIENT)[fac]
    cpt[fac & first & inpatient[li]] = "99223"
    cpt[fac & (ln == 1) & inpatient[li]] = "99238"
    cpt[fac & first & needs_pa_fac[li]] = "27447"
    units = np.ones(len(li), dtype=np.int64)
    units[cpt == "99232"] = np.maximum(los[li][cpt == "99232"] - 1, 1)

    # pharmacy detail inputs
    ph = np.where(ctype == ClaimType.PHARMACY)[0]
    drug_i = crr.integers("drug", 0, len(DRUGS_NO_PA))
    tier = np.array([DRUGS_NO_PA[d][2] for d in drug_i], dtype=np.int64)

    # ---- money, in integer cents (no float rounding): allowed = contract rate x units (80% of it out of network),
    # billed = allowed x 1.4-2.6 markup; denied or pended lines get allowed = paid = 0
    rate_cents = np.array([CPT[c][2] * 100 if c else 0 for c in cpt], dtype=np.int64) * units
    drug_cents = np.round(np.array([DRUG_MEDIAN_CENTS[t] for t in tier]) * np.exp(crr.normal("drug_cost", 0, 0.3))).astype(np.int64)
    allowed_full = np.where(lt == ClaimType.PHARMACY, drug_cents[li], np.where(network[li] == NetworkStatus.IN, rate_cents, (rate_cents * 0.8).astype(np.int64)))
    billed = np.round(allowed_full * np.where(lt == ClaimType.PHARMACY, 1.1, 1.4 + lrr.u("markup") * 1.2)).astype(np.int64)

    paid_line = status[li] == ClaimStatus.PAID
    allowed = np.where(paid_line, allowed_full, 0)

    # member liability: deductible on the member's first claim only, copay on line 0 (prof/pharmacy), coinsurance otherwise
    plan_rows = [inp["plans"][p] for p in plan_ids]
    cents = lambda key: np.array([int(round(float(r[key]) * 100)) for r in plan_rows], dtype=np.int64)  # noqa: E731
    deductible_c, copay_pcp_c, copay_spec_c = cents("deductible"), cents("copay_pcp"), cents("copay_specialist")
    coins_pct = np.array([int(r["coinsurance_pct"]) for r in plan_rows], dtype=np.int64)
    ded = np.zeros(len(li), np.int64)
    ded_line = first & (cj[li] == 0) & paid_line
    ded[ded_line] = np.minimum(allowed[ded_line], deductible_c[li][ded_line])
    is_pcp = np.array([inp["ptype"].get(b) == ProviderType.PCP for b in billing], dtype=bool) if n else np.zeros(0, bool)
    copay_amt = np.where(ctype == ClaimType.PHARMACY, np.array([catalog.TIER_COPAY_USD[t] * 100 for t in tier], dtype=np.int64),
                         np.where(is_pcp, copay_pcp_c, copay_spec_c))
    copay = np.zeros(len(li), np.int64)
    cp_line = first & paid_line & np.isin(lt, [ClaimType.PROFESSIONAL, ClaimType.PHARMACY])
    copay[cp_line] = np.minimum(allowed[cp_line] - ded[cp_line], copay_amt[li][cp_line])
    coins = np.zeros(len(li), np.int64)
    co_line = paid_line & ~cp_line
    coins[co_line] = ((allowed[co_line] - ded[co_line]) * coins_pct[li][co_line]) // 100
    paid = allowed - ded - copay - coins  # F-02; member responsibility = ded + copay + coins (the PR adjustments)

    line_status = np.where(status[li] == ClaimStatus.PAID, ClaimStatus.PAID, np.where(status[li] == ClaimStatus.DENIED, ClaimStatus.DENIED, ClaimStatus.PENDED))

    def per_claim(x: np.ndarray) -> np.ndarray:
        return np.bincount(li, weights=x, minlength=n).astype(np.int64)

    tot_billed, tot_allowed, tot_paid = per_claim(billed), per_claim(allowed), per_claim(paid)
    member_resp = per_claim(ded + copay + coins)

    return dict(midx=midx, m=m, ci=ci, cj=cj, ckey=ckey, crr=crr, scen=scen, ctype=ctype, inpatient=inpatient,
                allowed_full=allowed_full, coins_pct=coins_pct, pend_reason=pend_reason, lrr=lrr,
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
    lk = np.where(c["needs_pa"])[0]  # claims that need an auth; their auth keys reuse the claim key
    # standalone
    rr = ctx.rr("auth_request:count", midx)
    k = np.minimum(rr.poisson("n_auths", ctx.spec.volumes.get("authorizations", 0) / max(len(inp["members"]["member_id"]), 1) * 0.5), 5)
    si = np.repeat(np.arange(len(midx)), k)
    sj = np.concatenate([np.arange(x) for x in k]) if len(si) else np.array([], np.int64)

    a_member = np.concatenate([c["ci"][lk], si])
    a_key = np.concatenate([c["ckey"][lk], keys.standalone_auth_key(midx[si], sj)])  # claim-linked auths reuse the claim key
    linked = np.concatenate([np.ones(len(lk), bool), np.zeros(len(si), bool)])
    n = len(a_key)
    arr = ctx.rr("auth_request", a_key)
    scen = np.where(linked, "BASE", ctx.scenarios("auth", arr)).astype(object)
    # happy path PAS-01 = a routine standard review; other baseline rows keep the D-06 outcome mix
    outcome = arr.choice("outcome", *catalog.AUTH_OUTCOMES).astype(object)
    scen[(scen == "PAS-01") & (outcome != AuthStatus.IN_REVIEW)] = "BASE"

    stype = arr.choice("stype", [ServiceType.OUTPATIENT, ServiceType.INPATIENT, ServiceType.DME, ServiceType.HOME_HEALTH, ServiceType.SNF, ServiceType.PHARMACY, ServiceType.BH], [45, 10, 10, 10, 10, 10, 5]).astype(object)
    stype[linked] = np.where(c["ctype"][lk] == ClaimType.FACILITY, ServiceType.INPATIENT, ServiceType.OUTPATIENT)
    stype[scen == "PAS-02"] = ServiceType.SNF
    stype[scen == "PAS-03"] = ServiceType.OUTPATIENT
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
    scen = np.where(routine, "PAS-01", np.where(scen == "PAS-01", "BASE", scen))

    # codes per auth (line 0)
    cpt0 = arr.choice("cpt", catalog.PA_CODES).astype(object)
    claim_line0 = {int(ck): cp for ck, cp, l in zip(c["ckey"][c["li"]], c["cpt"], c["ln"]) if l == 0}
    cpt0[linked] = [claim_line0[int(k)] for k in c["ckey"][lk]]
    cpt0[scen == "PAS-03"] = "97161"

    plan_ids = m["plan_id"][a_member]
    member_ids = m["member_id"][a_member]
    requesting = np.array([inp["pcp"].get(mid) for mid in member_ids], dtype=object)
    fallback = pick_provider(inp, plan_ids, ProviderRole.PCP, np.ones(n, bool), arr.u("req"))
    requesting = np.where(requesting == None, fallback, requesting)  # noqa: E711
    servicing = pick_provider(inp, plan_ids, ProviderRole.SPECIALIST, np.ones(n, bool), arr.u("svc"))
    servicing[linked] = c["billing"][lk]
    fac_role = np.where(stype == ServiceType.INPATIENT, ProviderRole.HOSPITAL, ProviderRole.POSTACUTE)
    facility = np.where(np.isin(stype, [ServiceType.INPATIENT, ServiceType.SNF, ServiceType.HOME_HEALTH]), None, None).astype(object)
    for r in (ProviderRole.HOSPITAL, ProviderRole.POSTACUTE):
        mask = np.isin(stype, [ServiceType.INPATIENT, ServiceType.SNF, ServiceType.HOME_HEALTH]) & (fac_role == r)
        facility[mask] = pick_provider(inp, plan_ids[mask], r, np.ones(mask.sum(), bool), arr.u(f"fac:{r}")[mask])
    persona = np.where(np.isin(stype, [ServiceType.SNF, ServiceType.INPATIENT, ServiceType.HOME_HEALTH]), "DISCHARGE",
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
    appr = np.where(out_i == AuthStatus.APPROVED, req_units, np.where(out_i == AuthStatus.PARTIAL, np.maximum(req_units // 2, 1),
                    np.where(out_i == AuthStatus.DENIED, 0, -1)))
    l_status = np.where(out_i == AuthStatus.APPROVED, AuthLineStatus.APPROVED, np.where(out_i == AuthStatus.PARTIAL, AuthLineStatus.MODIFIED,
                        np.where(out_i == AuthStatus.DENIED, AuthLineStatus.DENIED, None)))
    if ctx.wants("auth_line"):
      ctx.write("auth_line", {
        "auth_line_id": pa.array(ctx.ids("auth_line", a_key[ai] * 4 + aj)),
        "auth_id": pa.array(auth_ids[ai].tolist()),
        "cpt_hcpcs_or_ndc": pa.array(np.where(aj == 0, cpt0[ai], alrr.choice("cpt2", ANCILLARY)).tolist()),
        "units_requested": pa.array(req_units),
        "units_approved": pa.array([None if x < 0 else int(x) for x in appr], pa.int64()),
        "site_of_service": pa.array(np.where(np.isin(stype[ai], [ServiceType.INPATIENT]), "21", np.where(stype[ai] == ServiceType.SNF, "31", "22")).tolist()),
        "line_status": pa.array(l_status.tolist(), pa.string()),
      }, scenario=scen[ai])

    # determinations
    d = np.where(decided)[0]
    drr = ctx.rr("auth_determination", a_key[d])
    d_out = status[d]
    denied = d_out == AuthStatus.DENIED
    valid_from = recv_day[d]
    valid_to = valid_from + 90 * DAY
    valid_to[linked[d]] = c["s_from"][lk] + 60 * DAY
    role = np.where(denied, drr.choice("md", ["MEDICAL_DIRECTOR", "SAME_SPECIALTY_PEER"], [70, 30]),
                    drr.choice("rn", ["NURSE", "MEDICAL_DIRECTOR"], [70, 30]))
    rationale = [f"Clinical documentation does not meet criteria for {CPT[cp][1].lower()} at this time." if den else None
                 for den, cp in zip(denied, cpt0[d])]
    if ctx.wants("auth_determination"):
      ctx.write("auth_determination", {
        "determination_id": pa.array(ctx.ids("auth_determination", a_key[d])),
        "auth_id": pa.array(auth_ids[d].tolist()),
        "outcome": pa.array(d_out.tolist()),
        "valid_from": dates(np.where(denied, NAT, valid_from)),
        "valid_to": dates(np.where(denied, NAT, valid_to)),
        "denial_category": pa.array(np.where(denied, DenialCategory.MED_NECESSITY, None).tolist(), pa.string()),
        "denial_rationale": pa.array(rationale, pa.string()),
        "criteria_asset_id": pa.array([pa_policy_asset_id(ctx, cp) for cp in cpt0[d]]),
        "decided_by_role": pa.array(role.tolist()),
        "determination_dt": stamps(det_dt[d]),
        "appeal_rights_sent_dt": stamps(np.where(denied, det_dt[d], np.datetime64("NaT", "s"))),
      }, scenario=scen[d])

    # open reviews
    o = np.where(np.isin(status, [AuthStatus.IN_REVIEW, AuthStatus.PENDED_INFO, AuthStatus.SUBMITTED]))[0]
    orr = ctx.rr("clinical_review_queue", a_key[o])
    if ctx.wants("clinical_review_queue"):
      ctx.write("clinical_review_queue", {
        "queue_id": pa.array(ctx.ids("clinical_review_queue", a_key[o])),
        "auth_id": pa.array(auth_ids[o].tolist()),
        "review_level": pa.array(orr.choice("level", ["NURSE", "MEDICAL_DIRECTOR"], [80, 20]).tolist()),
        "assigned_dt": stamps(received[o] + 2 * HOUR),
        "sla_due_dt": stamps(due[o]),
        "status": pa.array(np.where(status[o] == AuthStatus.PENDED_INFO, "WAITING", orr.choice("qs", ["WAITING", "IN_REVIEW"])).tolist()),
        "p2p_available_until": stamps(np.full(len(o), np.datetime64("NaT", "s"))),
      }, scenario=scen[o])

    # documentation: REQUESTED for pended auths; RECEIVED for 30% of decided ones
    doc_req = (status == AuthStatus.PENDED_INFO) | (decided & arr.bernoulli("had_docs", 0.3) & ~linked)
    q = np.where(doc_req)[0]
    qrr = ctx.rr("documentation_requirement", a_key[q])
    doc_type = qrr.choice("doc", ["CLINICAL_NOTES", "IMAGING", "LABS", "PT_EVAL", "PRESCRIBER_STATEMENT"]).astype(object)
    doc_type[scen[q] == "PAS-02"] = "DISCHARGE_SUMMARY"
    doc_type[scen[q] == "PAS-03"] = "PT_EVAL"
    pended = status[q] == AuthStatus.PENDED_INFO
    req_dt = recv_day[q] + DAY
    if ctx.wants("documentation_requirement"):
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
def plan_appeals(ctx: GenContext, c: dict) -> dict:
    """Appeals on denied claims: 12% baseline plus the PAP scenarios. Returns per-appeal arrays (claim positions)."""
    status, scen, cat = c["status"], c["scen"], c["denial_cat"]
    lob = c["m"]["lob"][c["ci"]]
    deadline_days = np.where(lob == Lob.COMMERCIAL, 180, 60)
    appealable = (status == ClaimStatus.DENIED) & (cat != DenialCategory.DUPLICATE) & (cat != DenialCategory.AUTH)
    rr = c["crr"]
    other_scenario = np.isin(scen, sorted((catalog.FORCED_CLAIM_SCENARIOS | {"PAP-01"}) - {"PAP-02", "PAP-03", "PAP-04"}))
    pick = appealable & ((rr.bernoulli("appeal", 0.12) & ~other_scenario) | np.isin(scen, ["PAP-02", "PAP-03", "PAP-04"]))
    ap = np.where(pick)[0]
    arr = ctx.rr("appeal_record", c["ckey"][ap])
    adjud = c["adjud"][ap]
    deadline = adjud + deadline_days[ap].astype("timedelta64[D]")
    sc = scen[ap]
    filed = adjud + arr.integers("file_lag", 3, 26).astype("timedelta64[D]")
    filed = np.where(sc == "PAP-04", deadline + arr.integers("late", 3, 21).astype("timedelta64[D]"), filed)
    st = arr.choice("status", [AppealStatus.OPEN, AppealStatus.IN_REVIEW, AppealStatus.DECIDED], [40, 40, 20]).astype(object)
    outcome = np.where(st == AppealStatus.DECIDED, arr.choice("outcome", [AppealOutcome.UPHELD, AppealOutcome.OVERTURNED], [60, 40]), None).astype(object)
    st[sc == "PAP-02"], outcome[sc == "PAP-02"] = AppealStatus.IN_REVIEW, None
    st[sc == "PAP-03"], outcome[sc == "PAP-03"] = AppealStatus.DECIDED, AppealOutcome.OVERTURNED
    st[sc == "PAP-04"], outcome[sc == "PAP-04"] = AppealStatus.OPEN, None
    decision = np.where(st == AppealStatus.DECIDED, filed + arr.integers("dec_lag", 5, 30).astype("timedelta64[D]"), NAT)
    ok = filed <= ctx.as_of
    ok &= np.isnat(decision) | (decision <= ctx.as_of)
    # a scenario that cannot fit before the as-of date falls back to a baseline denial
    c["scen"][ap[~ok & np.isin(sc, ["PAP-02", "PAP-03", "PAP-04"])]] = "BASE"
    keep = np.where(ok)[0]
    return {"pos": ap[keep], "arr_keys": c["ckey"][ap[keep]], "filed": filed[keep], "deadline": deadline[keep],
            "status": st[keep], "outcome": outcome[keep], "decision": decision[keep]}


def plan_reprocessing(ctx: GenContext, c: dict, appeals: dict) -> dict:
    """Claims re-adjudicated into a replacement claim (frequency 7):
    overturned baseline appeals (APPEAL_OVERTURN) and MCI-04 corrections of paid claims (PROVIDER_CORRECTION).
    PAP-03 stays 'awaiting reprocessing'."""
    scen = c["scen"]
    over = appeals["outcome"] == AppealOutcome.OVERTURNED
    a_pos = appeals["pos"][over]
    a_dec = appeals["decision"][over]
    awaiting = scen[a_pos] == "PAP-03"
    a_pos, a_dec = a_pos[~awaiting], a_dec[~awaiting]
    a_dt = a_dec + 2 * DAY
    m_pos = np.where((scen == "MCI-04") & (c["status"] == ClaimStatus.PAID))[0]
    m_dt = c["paid_dt"][m_pos] + 3 * DAY
    pos = np.concatenate([a_pos, m_pos]).astype(np.int64)
    when = np.concatenate([a_dt, m_dt]).astype("datetime64[D]")
    trigger = np.array(["APPEAL_OVERTURN"] * len(a_pos) + ["PROVIDER_CORRECTION"] * len(m_pos), dtype=object)
    ok = when < ctx.as_of
    c["scen"][pos[~ok & (trigger == "PROVIDER_CORRECTION")]] = "BASE"
    return {"pos": pos[ok], "when": when[ok], "trigger": trigger[ok]}


def write_claims(ctx: GenContext, inp: dict, c: dict, claim_auth: np.ndarray) -> None:
    """Write every claim entity for a shard, adding appeals, reprocessing and accumulators.

    Reprocessed claims keep their original row (status ADJUSTED or REPROCESSED) and get a replacement claim
    (frequency code 7, key = original + 32) with its own lines, adjustments and payment, plus a reprocessing_history
    row (R-07, F-06). Only entities listed in the payloads are written (ctx.wants).
    """
    li, ln, scen, ctype = c["li"], c["ln"], c["scen"], c["ctype"]
    crr = c["crr"]
    appeals = plan_appeals(ctx, c)
    rp = plan_reprocessing(ctx, c, appeals)
    scen = c["scen"]  # may have been reset to BASE by the planners

    # ---------------- final status of the originals
    status = c["status"].copy()
    status[rp["pos"][rp["trigger"] == "APPEAL_OVERTURN"]] = ClaimStatus.REPROCESSED
    status[rp["pos"][rp["trigger"] == "PROVIDER_CORRECTION"]] = ClaimStatus.ADJUSTED

    # ---------------- replacement claims (key + 32 keeps them unique per member)
    r = rp["pos"]
    r_lines = np.where(np.isin(li, r))[0]
    r_map = {int(p): k for k, p in enumerate(r)}
    r_li = np.array([r_map[int(x)] for x in li[r_lines]], dtype=np.int64)  # replacement claim index per line
    r_allowed = c["allowed_full"][r_lines]
    r_coins = (r_allowed * c["coins_pct"][li[r_lines]]) // 100
    r_paid = r_allowed - r_coins
    nr = len(r)
    r_tot = lambda x: np.bincount(r_li, weights=x, minlength=nr).astype(np.int64)  # noqa: E731
    r_ckey = keys.replacement_key(c["ckey"][r])
    r_paid_dt = np.minimum(rp["when"] + DAY, ctx.as_of)

    claim_ids = np.asarray(ctx.ids("claim_header", c["ckey"]), dtype=object)
    r_claim_ids = np.asarray(ctx.ids("claim_header", r_ckey), dtype=object)
    member_ids = c["m"]["member_id"][c["ci"]]

    if ctx.wants("claim_header"):
        ctx.write("claim_header", {
            "claim_id": pa.array(np.concatenate([claim_ids, r_claim_ids]).tolist()),
            "claim_number": pa.array(keys.claim_numbers(c["ckey"]) + keys.claim_numbers(r_ckey)),
            "claim_type": pa.array(np.concatenate([ctype, ctype[r]]).tolist()),
            "member_id": pa.array(np.concatenate([member_ids, member_ids[r]]).tolist()),
            "plan_id": pa.array(np.concatenate([c["plan_ids"], c["plan_ids"][r]]).tolist()),
            "billing_npi": pa.array(np.concatenate([c["billing"], c["billing"][r]]).tolist()),
            "rendering_npi": pa.array(np.concatenate([c["rendering"], c["rendering"][r]]).tolist(), pa.string()),
            "facility_npi": pa.array(np.concatenate([c["facility"], c["facility"][r]]).tolist(), pa.string()),
            "claim_frequency_code": pa.array(["1"] * len(claim_ids) + ["7"] * nr),
            "original_claim_id": pa.array([None] * len(claim_ids) + claim_ids[r].tolist(), pa.string()),
            "service_from_dt": dates(np.concatenate([c["s_from"], c["s_from"][r]])),
            "service_to_dt": dates(np.concatenate([c["s_to"], c["s_to"][r]])),
            "received_dt": dates(np.concatenate([c["received"], rp["when"]])),
            "adjudicated_dt": dates(np.concatenate([c["adjud"], rp["when"]])),
            "paid_dt": dates(np.concatenate([c["paid_dt"], r_paid_dt])),
            "status": pa.array(np.concatenate([status, np.full(nr, ClaimStatus.PAID, dtype=object)]).tolist()),
            "network_status": pa.array(np.concatenate([c["network"], c["network"][r]]).tolist()),
            "is_emergency": pa.array(np.concatenate([c["emergency"], c["emergency"][r]])),
            "auth_id": pa.array(np.concatenate([claim_auth, claim_auth[r]]).tolist(), pa.string()),
            "bill_type": pa.array(np.concatenate([np.where(ctype == ClaimType.FACILITY, np.where(c["inpatient"], "0111", "0131"), None),
                                                  np.where(ctype[r] == ClaimType.FACILITY, np.where(c["inpatient"][r], "0111", "0131"), None)]).tolist(), pa.string()),
            "drg_code": pa.array(np.concatenate([np.where(c["inpatient"], crr.choice("drg", ["470", "291", "193", "871"]), None),
                                                 np.where(c["inpatient"], crr.choice("drg", ["470", "291", "193", "871"]), None)[r]]).tolist(), pa.string()),
            "primary_dx": pa.array(np.concatenate([crr.choice("dx", catalog.ICD10), crr.choice("dx", catalog.ICD10)[r]]).tolist()),
            "total_billed": money(np.concatenate([c["tot_billed"], c["tot_billed"][r]])),
            "total_allowed": money(np.concatenate([c["tot_allowed"], r_tot(r_allowed)])),
            "total_paid": money(np.concatenate([c["tot_paid"], r_tot(r_paid)])),
            "member_resp": money(np.concatenate([c["member_resp"], r_tot(r_coins)])),
        }, scenario=np.concatenate([scen, scen[r]]))

    line_ids = np.asarray(ctx.ids("claim_line", c["lkey"]), dtype=object)
    r_lkey = keys.line_key(r_ckey[r_li], ln[r_lines])
    r_line_ids = np.asarray(ctx.ids("claim_line", r_lkey), dtype=object)
    if ctx.wants("claim_line"):
        ctx.write("claim_line", {
            "line_id": pa.array(np.concatenate([line_ids, r_line_ids]).tolist()),
            "claim_id": pa.array(np.concatenate([claim_ids[li], r_claim_ids[r_li]]).tolist()),
            "line_no": pa.array(np.concatenate([ln + 1, ln[r_lines] + 1]).astype(np.int32)),
            "cpt_hcpcs": pa.array(np.concatenate([c["cpt"], c["cpt"][r_lines]]).tolist(), pa.string()),
            "revenue_code": pa.array(np.where(np.concatenate([ctype[li], ctype[li[r_lines]]]) == ClaimType.FACILITY, "0120", None).tolist(), pa.string()),
            "modifier": pa.array([None] * (len(li) + len(r_lines)), pa.string()),
            "units": pa.array(np.concatenate([c["units"], c["units"][r_lines]])),
            "billed": money(np.concatenate([c["billed"], c["billed"][r_lines]])),
            "allowed": money(np.concatenate([c["allowed"], r_allowed])),
            "paid": money(np.concatenate([c["paid"], r_paid])),
            "deductible": money(np.concatenate([c["ded"], np.zeros(len(r_lines), np.int64)])),
            "copay": money(np.concatenate([c["copay"], np.zeros(len(r_lines), np.int64)])),
            "coinsurance": money(np.concatenate([c["coins"], r_coins])),
            "line_status": pa.array(np.concatenate([c["line_status"], np.full(len(r_lines), ClaimStatus.PAID, dtype=object)]).tolist()),
        }, scenario=np.concatenate([scen[li], scen[li[r_lines]]]))

    # pharmacy detail (R-08: exactly one per pharmacy claim)
    ph = c["ph"]
    if ctx.wants("pharmacy_claim_detail"):
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

    # denials (claim level, anchored on line 1); they stay on reprocessed originals as history
    dn = np.where(c["status"] == ClaimStatus.DENIED)[0]
    if ctx.wants("claim_denial"):
        first_line = {int(ci): lid for ci, lid, l in zip(li, line_ids, ln) if l == 0}
        cat = c["denial_cat"][dn]
        lob = c["m"]["lob"][c["ci"][dn]]
        appealable = (cat != DenialCategory.DUPLICATE) & (cat != DenialCategory.AUTH)
        deadline = c["adjud"][dn] + np.where(lob == Lob.COMMERCIAL, 180, 60).astype("timedelta64[D]")
        ctx.write("claim_denial", {
            "denial_id": pa.array(ctx.ids("claim_denial", c["ckey"][dn])),
            "claim_id": pa.array(claim_ids[dn].tolist()),
            "line_id": pa.array([first_line[int(i)] for i in dn], pa.string()),
            "carc": pa.array([f"CO-{catalog.DENIAL_CATEGORIES[x][0]}" for x in cat], pa.string()),
            "rarc": pa.array([None] * len(dn), pa.string()),
            "denial_category": pa.array(cat.tolist(), pa.string()),
            "reason_text": pa.array([catalog.DENIAL_CATEGORIES[x][1] for x in cat], pa.string()),
            "liable_party": pa.array(["PROVIDER"] * len(dn), pa.string()),
            "denial_dt": dates(c["adjud"][dn]),
            "appealable": pa.array(appealable, pa.bool_()),
            "appeal_deadline_dt": dates(np.where(appealable, deadline, NAT)),
        }, scenario=scen[dn])

    # pends
    pd_ = np.where(c["status"] == ClaimStatus.PENDED)[0]
    if ctx.wants("claim_pend"):
        prr = ctx.rr("claim_pend", c["ckey"][pd_])
        reason = prr.choice("reason", ["RECORDS_REQUEST", "COB_INFO", "CLINICAL_REVIEW", "PRICING"], [40, 25, 20, 15]).astype(object)
        forced_reason = c["pend_reason"][pd_]
        reason = np.where(forced_reason != None, forced_reason, reason)  # noqa: E711
        pend_dt = np.minimum(c["received"][pd_] + prr.integers("pend_lag", 0, 4).astype("timedelta64[D]"), ctx.as_of)
        ctx.write("claim_pend", {
            "pend_id": pa.array(ctx.ids("claim_pend", c["ckey"][pd_]), pa.string()),
            "claim_id": pa.array(claim_ids[pd_].tolist(), pa.string()),
            "pend_reason": pa.array(reason.tolist(), pa.string()),
            "pend_dt": dates(pend_dt),
            "expected_resolution_dt": dates(pend_dt + prr.integers("res", 10, 31).astype("timedelta64[D]")),
            "info_needed_from": pa.array(np.where(reason == "COB_INFO", "MEMBER", np.where(reason == "PRICING", "PAYER", "PROVIDER")).tolist(), pa.string()),
            "resolved_dt": dates(np.full(len(pd_), NAT)),
        }, scenario=scen[pd_])

    # adjustments (F-05): paid lines CO-45 / PR-1 / PR-2 / PR-3; denied lines CO-<carc> = billed; replacements CO-45 / PR-2
    if ctx.wants("claim_adjustment"):
        a_line, a_code, a_amt, a_key, a_date, a_scen = [], [], [], [], [], []
        paid_l = c["line_status"] == ClaimStatus.PAID
        den_l = c["line_status"] == ClaimStatus.DENIED
        for slot, code, amt, mask in ((0, "CO-45", c["billed"] - c["allowed"], paid_l), (1, "PR-1", c["ded"], paid_l),
                                      (2, "PR-2", c["coins"], paid_l), (3, "PR-3", c["copay"], paid_l)):
            sel = np.where(mask & (amt > 0))[0]
            a_line.append(line_ids[sel]); a_code.append(np.full(len(sel), code, dtype=object)); a_amt.append(amt[sel])
            a_key.append(keys.adjustment_key(c["lkey"][sel], slot)); a_date.append(c["adjud"][li[sel]]); a_scen.append(scen[li[sel]])
        sel = np.where(den_l)[0]
        a_line.append(line_ids[sel])
        a_code.append(np.array([f"CO-{catalog.DENIAL_CATEGORIES[x][0]}" for x in c["denial_cat"][li[sel]]], dtype=object))
        a_amt.append(c["billed"][sel]); a_key.append(keys.adjustment_key(c["lkey"][sel], keys.DENIAL_ADJUSTMENT_SLOT)); a_date.append(c["adjud"][li[sel]]); a_scen.append(scen[li[sel]])
        for slot, code, amt in ((0, "CO-45", c["billed"][r_lines] - r_allowed), (2, "PR-2", r_coins)):
            sel = np.where(amt > 0)[0]
            a_line.append(r_line_ids[sel]); a_code.append(np.full(len(sel), code, dtype=object)); a_amt.append(amt[sel])
            a_key.append(keys.adjustment_key(r_lkey[sel], slot)); a_date.append(rp["when"][r_li[sel]]); a_scen.append(scen[li[r_lines[sel]]])
        akey = np.concatenate(a_key).astype(np.int64)
        order = np.argsort(akey, kind="stable")
        codes = np.concatenate(a_code)[order] if akey.size else np.array([], dtype=object)
        ctx.write("claim_adjustment", {
            "adjustment_id": pa.array(ctx.ids("claim_adjustment", akey[order]), pa.string()),
            "line_id": pa.array(np.concatenate(a_line)[order].tolist(), pa.string()),
            "group_code": pa.array([x.split("-")[0] for x in codes], pa.string()),
            "carc": pa.array(codes.tolist(), pa.string()),
            "amount": money(np.concatenate(a_amt)[order]),
            "adjustment_dt": dates(np.concatenate(a_date)[order]),
        }, scenario=np.concatenate(a_scen)[order] if akey.size else "BASE")

    # payments: originals paid at adjudication (adjusted ones too) + replacement payments
    if ctx.wants("claim_payment"):
        py = np.where(c["status"] == ClaimStatus.PAID)[0]
        yrr = ctx.rr("claim_payment", c["ckey"][py])
        payee = np.array([inp["tin"][b] for b in c["billing"][py]], dtype=object)
        wrong = scen[py] == "PPI-04"  # payee TIN differs from the billing group
        payee[wrong] = [f"9{(int(t[1:]) + 1234567) % 100000000:08d}" for t in payee[wrong]]
        recoup = scen[py] == "PPI-03"
        traces = np.array([f"TRN{int(k):012d}" for k in c["ckey"][py]], dtype=object)
        r_traces = [f"TRN{int(k):012d}" for k in r_ckey]
        ctx.write("claim_payment", {
            "payment_id": pa.array(ctx.ids("claim_payment", np.concatenate([c["ckey"][py], r_ckey])), pa.string()),
            "claim_id": pa.array(np.concatenate([claim_ids[py], r_claim_ids]).tolist(), pa.string()),
            "payment_dt": dates(np.concatenate([c["paid_dt"][py], r_paid_dt])),
            "method": pa.array(np.concatenate([yrr.choice("method", ["EFT", "CHECK", "VCARD"], [80, 15, 5]),
                                               np.full(nr, "EFT", dtype=object)]).tolist(), pa.string()),
            "paid_amount": money(np.concatenate([c["tot_paid"][py], r_tot(r_paid)])),
            "trace_no": pa.array(traces.tolist() + r_traces, pa.string()),
            "payee_tin": pa.array(payee.tolist() + [inp["tin"][b] for b in c["billing"][r]], pa.string()),
            "recoup_flag": pa.array(np.concatenate([recoup, np.zeros(nr, bool)])),
            "offset_ref": pa.array([f"RCP-{t[3:]}" if f else None for t, f in zip(traces, recoup)] + [None] * nr, pa.string()),
            "offset_amount": money(np.concatenate([np.where(recoup, (c["tot_paid"][py] * 0.25).astype(np.int64), 0),
                                                   np.zeros(nr, np.int64)])),
        }, scenario=np.concatenate([scen[py], scen[r]]))

    # appeals
    if ctx.wants("appeal_record"):
        pos = appeals["pos"]
        lob_ap = c["m"]["lob"][c["ci"][pos]]
        ctx.write("appeal_record", {
            "appeal_id": pa.array(ctx.ids("appeal_record", appeals["arr_keys"]), pa.string()),
            "appeal_number": pa.array([f"APL-{int(k // 64) + 1:07d}-{int(k % 64):02d}" for k in appeals["arr_keys"]], pa.string()),
            "claim_id": pa.array(claim_ids[pos].tolist(), pa.string()),
            "filed_by": pa.array(["PROVIDER"] * len(pos), pa.string()),
            "level": pa.array(["L1"] * len(pos), pa.string()),
            "reason": pa.array([DenialCategory.MED_NECESSITY if x == DenialCategory.MED_NECESSITY else "OTHER" for x in c["denial_cat"][pos]], pa.string()),
            "filed_dt": dates(appeals["filed"]),
            "deadline_dt": dates(appeals["deadline"]),
            "status": pa.array(appeals["status"].tolist(), pa.string()),
            "outcome": pa.array(appeals["outcome"].tolist(), pa.string()),
            "decision_dt": dates(appeals["decision"]),
            "documents_received": pa.array([["clinical_notes"] if st != AppealStatus.OPEN else [] for st in appeals["status"]], pa.list_(pa.string())),
            "late_filing_reason": pa.array(["GOOD_CAUSE_REQUESTED" if f > d else None for f, d in zip(appeals["filed"], appeals["deadline"])],
                                           pa.string()),
        }, scenario=scen[pos])

    # reprocessing trail (R-07, F-06)
    if ctx.wants("reprocessing_history"):
        ctx.write("reprocessing_history", {
            "reprocess_id": pa.array(ctx.ids("reprocessing_history", c["ckey"][r]), pa.string()),
            "claim_id": pa.array(claim_ids[r].tolist(), pa.string()),
            "new_claim_id": pa.array(r_claim_ids.tolist(), pa.string()),
            "trigger": pa.array(rp["trigger"].tolist(), pa.string()),
            "reprocess_dt": stamps(rp["when"].astype("datetime64[s]") + np.timedelta64(10, "h")),
            "paid_before": money(c["tot_paid"][r]),
            "paid_after": money(r_tot(r_paid)),
            "performed_by": pa.array(np.where(rp["trigger"] == "APPEAL_OVERTURN", "ANALYST", "SYSTEM").tolist(), pa.string()),
        }, scenario=scen[r])

    # accumulators (F-03): PR liability on claims that are PAID in their final version
    if ctx.wants("member_accumulator"):
        final_paid_orig = (status == ClaimStatus.PAID)[li] & (c["line_status"] == ClaimStatus.PAID)
        pl = np.where(final_paid_orig)[0]
        mem = np.concatenate([c["midx"][c["ci"][li[pl]]], c["midx"][c["ci"][li[r_lines]]]])
        net = np.concatenate([c["network"][li[pl]], c["network"][li[r_lines]]])
        ded = np.concatenate([c["ded"][pl], np.zeros(len(r_lines), np.int64)])
        oop = np.concatenate([(c["ded"] + c["copay"] + c["coins"])[pl], r_coins])
        gkey = mem * 2 + np.where(net == NetworkStatus.IN, 0, 1)
        uniq, inv = np.unique(gkey, return_inverse=True)
        member_of = {int(mx): mid for mx, mid in zip(c["midx"], c["m"]["member_id"])}
        ctx.write("member_accumulator", {
            "accumulator_id": pa.array(ctx.ids("member_accumulator", uniq), pa.string()),
            "member_id": pa.array([member_of[int(k // 2)] for k in uniq], pa.string()),
            "plan_year": pa.array(np.full(len(uniq), int(str(ctx.as_of)[:4]))),
            "network": pa.array(np.where(uniq % 2 == 0, NetworkStatus.IN, NetworkStatus.OON).tolist(), pa.string()),
            "deductible_met": money(np.bincount(inv, weights=ded, minlength=len(uniq)).astype(np.int64)),
            "oop_met": money(np.bincount(inv, weights=oop, minlength=len(uniq)).astype(np.int64)),
        })


def prepare(ctx: GenContext) -> dict:
    """Read members, plans and provider pools once for all tasks."""
    return load_inputs(ctx)


def task_count(ctx: GenContext, prep: dict | None = None) -> int:
    n = len(prep["members"]["member_id"]) if prep else ctx.stores.tables.query("SELECT count(*) FROM member_demographics").column(0)[0].as_py()
    return len(ctx.shard_ranges(n))


def run_task(ctx: GenContext, index: int, prep: dict) -> None:
    """Generate claims and authorizations for one shard of members. Safe to re-run (upserts on deterministic ids)."""
    lo, hi = ctx.shard_ranges(len(prep["members"]["member_id"]))[index]
    c = gen_claims(ctx, prep, slice(lo, hi))
    auth = gen_auths(ctx, prep, c) if ctx.wants("auth_request") else {"claim_auth": np.full(len(c["ckey"]), None, dtype=object)}
    write_claims(ctx, prep, c, auth["claim_auth"])


def run(ctx: GenContext) -> list[str]:
    prep = prepare(ctx)
    for i in range(task_count(ctx, prep)):
        run_task(ctx, i, prep)
    return [e for e in catalog.STAGE_ENTITIES["s5"] if ctx.wants(e)]
