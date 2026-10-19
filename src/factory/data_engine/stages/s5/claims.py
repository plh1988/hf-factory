"""One shard's claims and claim lines as a ClaimBatch, built by a pipeline of steps.

gen_claims runs the steps in order; each step reads fields set by the ones before it and sets its own:

  1. _expand_claims        how many claims each member has (Poisson around claims/members), inside coverage (R-02)
  2. _types_and_dates      claim type, dates of service, received/adjudicated dates (T-02), emergency flag
  3. _assign_providers     billing/rendering/facility providers from the plan's participating pools (R-04)
  4. _assign_status        baseline denial/pend draws, then scenario recipes override them
  5. _draw_lines           lines and procedure codes; PA-required codes are flagged so the auth comes first (R-03)
  6. _price_lines          money in integer cents: allowed, billed, deductible, copay, coinsurance, paid (F-01, F-02)

Every random value comes from a row stream keyed by (seed, entity, row key, draw name), so the order of steps
and of draws inside them never changes the data.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from factory.codes import ClaimStatus, ClaimType, DenialCategory, NetworkStatus, ProviderRole, ProviderType
from factory.data_engine import catalog, keys
from factory.data_engine.context import GenContext
from factory.data_engine.rng import RowRandom
from factory.data_engine.stages.s5.inputs import (
    ANCILLARY, CPT, DAY, DRUG_MEDIAN_CENTS, DRUGS_NO_PA, INPATIENT, NAT, OFFICE, PROF_PA, Inputs, pick_provider, to_date,
)

_ = None  # placeholder default: a step fills the field


@dataclass(eq=False)
class ClaimBatch:
    """Parallel arrays for one shard. Claim arrays have one entry per claim, line arrays one per claim line;
    money is integer cents. Index maps: `ci` claim -> member position in the shard, `cj` claim number for that
    member, `li` line -> claim position, `ln` line number. Keys (see keys.py): claim = member*64 + j, line = claim*8 + n.
    """

    # ---- shard members (set by step 1)
    m: dict[str, np.ndarray]  # member columns of this shard (member_id, lob, plan_id, ...)
    midx: np.ndarray  # member index (member number - 1)
    w_start: np.ndarray  # per member: first possible date of service
    span: np.ndarray  # per member: days in the claims window (< 0: no claims)
    # ---- claims (step 1)
    ci: np.ndarray
    cj: np.ndarray
    ckey: np.ndarray
    crr: RowRandom  # claim_header row streams
    scen: np.ndarray  # scenario tag per claim (reset to BASE when a recipe cannot hold)
    # ---- step 2
    ctype: np.ndarray = _
    forced: np.ndarray = _  # forced-scenario claims: clean professional, in network, non-emergency
    inpatient: np.ndarray = _
    los: np.ndarray = _  # length of stay in days
    s_from: np.ndarray = _
    s_to: np.ndarray = _
    received: np.ndarray = _
    adjud: np.ndarray = _  # NaT unless PAID or DENIED (after step 4)
    in_process: np.ndarray = _  # adjudication would fall after as_of: stays RECEIVED
    emergency: np.ndarray = _
    # ---- step 3
    plan_ids: np.ndarray = _
    billing: np.ndarray = _
    rendering: np.ndarray = _
    facility: np.ndarray = _
    network: np.ndarray = _
    # ---- step 4
    status: np.ndarray = _
    denial_cat: np.ndarray = _
    pend_reason: np.ndarray = _
    edge: np.ndarray = _  # forced scenario that is adjudicated
    paid_dt: np.ndarray = _
    # ---- step 5 (lines)
    li: np.ndarray = _
    ln: np.ndarray = _
    lkey: np.ndarray = _
    lrr: RowRandom = _  # claim_line row streams
    needs_pa: np.ndarray = _  # per claim: a line needs prior authorization
    cpt: np.ndarray = _
    units: np.ndarray = _
    ph: np.ndarray = _  # positions of pharmacy claims
    drug_i: np.ndarray = _  # per claim: index into DRUGS_NO_PA
    tier: np.ndarray = _  # per claim: formulary tier of that drug
    # ---- step 6 (money)
    allowed_full: np.ndarray = _  # per line: allowed before status (used for replacements)
    billed: np.ndarray = _
    allowed: np.ndarray = _
    coins_pct: np.ndarray = _  # per claim
    ded: np.ndarray = _
    copay: np.ndarray = _
    coins: np.ndarray = _
    paid: np.ndarray = _
    line_status: np.ndarray = _
    tot_billed: np.ndarray = _
    tot_allowed: np.ndarray = _
    tot_paid: np.ndarray = _
    member_resp: np.ndarray = _

    @property
    def n(self) -> int:
        return len(self.ckey)


def gen_claims(ctx: GenContext, inp: Inputs, sl: slice) -> ClaimBatch:
    """Claims and claim lines for the members in slice `sl` (written later by writers.write_claims)."""
    c = _expand_claims(ctx, inp, sl)
    _types_and_dates(ctx, c)
    _assign_providers(inp, c)
    _assign_status(ctx, c)
    _draw_lines(ctx, c)
    _price_lines(inp, c)
    return c


def _expand_claims(ctx: GenContext, inp: Inputs, sl: slice) -> ClaimBatch:
    """Step 1: claims per member inside the coverage window, expanded into claim rows with stable keys."""
    m = {k: v[sl] for k, v in inp.members.items()}
    midx = np.array([int(x[4:]) - 1 for x in m["member_number"]], dtype=np.int64)
    rr = ctx.rr("claim_header:count", midx)
    eff, term = to_date(m["effective_dt"]), to_date(m["term_dt"])
    hist_start = ctx.as_of - np.timedelta64(int(ctx.spec.history_months or 12) * 31, "D")
    w_start = np.maximum(eff, hist_start)
    w_end = np.minimum(np.where(np.isnat(term), ctx.as_of, term), ctx.as_of - 3 * DAY)
    span = (w_end - w_start).astype(int)
    lam = ctx.spec.volumes["claims"] / max(len(inp.members["member_id"]), 1)
    k = np.where(span >= 0, np.minimum(rr.poisson("n_claims", lam), 20), 0)

    ci = np.repeat(np.arange(len(midx)), k)
    cj = np.concatenate([np.arange(c) for c in k]) if len(ci) else np.array([], np.int64)
    ckey = keys.claim_key(midx[ci], cj)  # stable per member and claim number
    crr = ctx.rr("claim_header", ckey)
    return ClaimBatch(m=m, midx=midx, w_start=w_start, span=span, ci=ci, cj=cj, ckey=ckey, crr=crr,
                      scen=ctx.scenarios("claim", crr))


def _types_and_dates(ctx: GenContext, c: ClaimBatch) -> None:
    """Step 2: claim type, inpatient stays, dates of service and processing (T-02), emergency flag."""
    crr, scen, span, ci = c.crr, c.scen, c.span, c.ci
    ctype = crr.choice("type", [ClaimType.PHARMACY, ClaimType.PROFESSIONAL, ClaimType.FACILITY], [40, 45, 15])
    # forced scenarios (e.g. PCS-02, MCI-02, PAP-02) need a clean professional, in-network, non-emergency claim
    c.forced = forced = np.isin(scen, sorted(catalog.FORCED_CLAIM_SCENARIOS))
    c.ctype = ctype = np.where(forced, ClaimType.PROFESSIONAL, ctype)
    c.inpatient = inpatient = (ctype == ClaimType.FACILITY) & crr.bernoulli("inpatient", 0.6)
    los = np.where(inpatient, crr.integers("los", 2, 7), 0)
    c.los = los = np.minimum(los, np.maximum(span[ci], 0))
    s_from = c.w_start[ci] + np.floor(crr.u("dos") * (span[ci] - los + 1)).astype("timedelta64[D]")
    early = np.isin(scen, ["PAP-02", "PAP-03", "PAP-04"])  # appeals need time to be filed or decided
    c.s_from = s_from = np.where(early, c.w_start[ci], s_from)
    c.s_to = s_from + los.astype("timedelta64[D]")
    # T-02: service <= received <= adjudicated <= paid, never after as_of; claims not yet adjudicated stay RECEIVED
    c.received = np.minimum(c.s_to + crr.integers("recv_lag", 1, 8).astype("timedelta64[D]"), ctx.as_of)
    c.adjud = c.received + crr.integers("adj_lag", 1, 6).astype("timedelta64[D]")
    c.in_process = c.adjud > ctx.as_of
    emergency = np.where(ctype == ClaimType.FACILITY, inpatient & crr.bernoulli("er", 0.35),
                         (ctype == ClaimType.PROFESSIONAL) & crr.bernoulli("er_p", 0.05))
    c.emergency = emergency & ~forced


def _assign_providers(inp: Inputs, c: ClaimBatch) -> None:
    """Step 3: 92% in network; pick_provider reports the truth if the plan has no participating provider (R-04)."""
    crr, ctype, n = c.crr, c.ctype, c.n
    c.plan_ids = plan_ids = c.m["plan_id"][c.ci]
    in_net = crr.bernoulli("in_network", 0.92) | c.forced
    role = np.where(ctype == ClaimType.PHARMACY, ProviderRole.PHARMACY, np.where(
        ctype == ClaimType.FACILITY, np.where(c.inpatient, ProviderRole.HOSPITAL, ProviderRole.POSTACUTE),
        crr.choice("prof_role", [ProviderRole.PCP, ProviderRole.SPECIALIST], [60, 40])))
    billing = np.empty(n, dtype=object)
    for r in np.unique(role) if n else []:
        mask = role == r
        billing[mask], in_net[mask] = pick_provider(inp, plan_ids[mask], r, in_net[mask], crr.u(f"billing:{r}")[mask],
                                                    with_network=True)
    rendering = np.where(ctype == ClaimType.PROFESSIONAL, billing, None)
    rendering = np.where(c.scen == "PCS-03", None, rendering)  # CO-16: missing rendering NPI
    attending = pick_provider(inp, plan_ids, ProviderRole.SPECIALIST, in_net, crr.u("attending"))
    c.billing = billing
    c.facility = np.where(ctype == ClaimType.FACILITY, billing, None)
    c.rendering = np.where(ctype == ClaimType.FACILITY, attending, rendering)
    c.network = np.where(in_net, NetworkStatus.IN, np.where(c.emergency, NetworkStatus.OON_EMERGENCY, NetworkStatus.OON))


def _assign_status(ctx: GenContext, c: ClaimBatch) -> None:
    """Step 4: baseline denial/pend rates, then scenario recipes; untaggable happy-path claims go back to BASE."""
    crr, scen, n = c.crr, c.scen, c.n
    denial_rate = float(ctx.spec.distributions.get("denial_rate", 0.10))
    deny = (c.ctype != ClaimType.PHARMACY) & crr.bernoulli("deny", denial_rate)
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
    status[c.in_process] = ClaimStatus.RECEIVED
    required = np.array([catalog.SCENARIOS[x]["claim_status"] if x in catalog.HAPPY_CLAIM_SCENARIOS else "" for x in scen], dtype=object)
    # happy-path tags are not forced: they stay only on claims that ended in the recipe's status (keeps the denial rate)
    happy_unmet = np.isin(scen, sorted(catalog.HAPPY_CLAIM_SCENARIOS)) & (status != required)
    happy_unmet |= (scen == "PAP-01") & (np.isin(denial_cat, [DenialCategory.DUPLICATE, DenialCategory.AUTH]) | (c.ctype == ClaimType.PHARMACY))
    c.scen = np.where(c.in_process | happy_unmet, "BASE", scen)
    c.edge = c.forced & ~c.in_process
    c.paid_dt = np.where(status == ClaimStatus.PAID,
                         np.minimum(c.adjud + crr.integers("pay_lag", 1, 6).astype("timedelta64[D]"), ctx.as_of), NAT)
    c.adjud = np.where(np.isin(status, [ClaimStatus.PAID, ClaimStatus.DENIED]), c.adjud, NAT)
    c.status, c.denial_cat, c.pend_reason = status, denial_cat, pend_reason


def _draw_lines(ctx: GenContext, c: ClaimBatch) -> None:
    """Step 5: lines per claim, procedure codes (PA codes where an auth is needed), units, pharmacy drug."""
    crr, ctype, n, scen, inpatient = c.crr, c.ctype, c.n, c.scen, c.inpatient
    n_lines = np.where(ctype == ClaimType.PHARMACY, 1, np.where(ctype == ClaimType.FACILITY,
                       np.minimum(2 + crr.poisson("n_fac_lines", 1.5), 6), np.minimum(1 + crr.poisson("n_prof_lines", 1.3), 4)))
    c.li = li = np.repeat(np.arange(n), n_lines)
    c.ln = ln = np.concatenate([np.arange(x) for x in n_lines]) if n else np.array([], np.int64)
    c.lkey = keys.line_key(c.ckey[li], ln)
    c.lrr = lrr = ctx.rr("claim_line", c.lkey)
    needs_pa_prof = (ctype == ClaimType.PROFESSIONAL) & ~c.edge & ~c.emergency & crr.bernoulli("pa_prof", 0.06)
    needs_pa_fac = inpatient & ~c.emergency & crr.bernoulli("pa_fac", 0.25)
    c.needs_pa = needs_pa_prof | needs_pa_fac
    cpt = np.empty(len(li), dtype=object)
    lt = ctype[li]
    first = ln == 0
    prof = lt == ClaimType.PROFESSIONAL
    cpt[prof & first] = lrr.choice("office", OFFICE)[prof & first]
    cpt[prof & ~first] = lrr.choice("ancillary", ANCILLARY)[prof & ~first]
    pa_line = first & (needs_pa_prof[li] | (scen[li] == "MCI-02"))  # MCI-02: PA code billed without an auth
    cpt[pa_line] = lrr.choice("pa_code", PROF_PA)[pa_line]
    fac = lt == ClaimType.FACILITY
    cpt[fac] = lrr.choice("inpatient", INPATIENT)[fac]
    cpt[fac & first & inpatient[li]] = "99223"
    cpt[fac & (ln == 1) & inpatient[li]] = "99238"
    cpt[fac & first & needs_pa_fac[li]] = "27447"
    units = np.ones(len(li), dtype=np.int64)
    units[cpt == "99232"] = np.maximum(c.los[li][cpt == "99232"] - 1, 1)
    c.cpt, c.units = cpt, units
    c.ph = np.where(ctype == ClaimType.PHARMACY)[0]
    c.drug_i = crr.integers("drug", 0, len(DRUGS_NO_PA))
    c.tier = np.array([DRUGS_NO_PA[d][2] for d in c.drug_i], dtype=np.int64)


def _price_lines(inp: Inputs, c: ClaimBatch) -> None:
    """Step 6: money in integer cents (no float rounding).

    allowed = contract rate x units (80% of it out of network), billed = allowed x 1.4-2.6 markup; denied or pended
    lines get allowed = paid = 0. Member liability: deductible on the member's first claim only, copay on line 0
    (professional and pharmacy), coinsurance otherwise; paid = allowed - liability (F-02).
    """
    crr, lrr, li, ctype, n = c.crr, c.lrr, c.li, c.ctype, c.n
    lt = ctype[li]
    first = c.ln == 0
    rate_cents = np.array([CPT[x][2] * 100 if x else 0 for x in c.cpt], dtype=np.int64) * c.units
    drug_cents = np.round(np.array([DRUG_MEDIAN_CENTS[t] for t in c.tier]) * np.exp(crr.normal("drug_cost", 0, 0.3))).astype(np.int64)
    allowed_full = np.where(lt == ClaimType.PHARMACY, drug_cents[li],
                            np.where(c.network[li] == NetworkStatus.IN, rate_cents, (rate_cents * 0.8).astype(np.int64)))
    billed = np.round(allowed_full * np.where(lt == ClaimType.PHARMACY, 1.1, 1.4 + lrr.u("markup") * 1.2)).astype(np.int64)

    paid_line = c.status[li] == ClaimStatus.PAID
    allowed = np.where(paid_line, allowed_full, 0)

    plan_rows = [inp.plans[p] for p in c.plan_ids]
    cents = lambda key: np.array([int(round(float(r[key]) * 100)) for r in plan_rows], dtype=np.int64)  # noqa: E731
    deductible_c, copay_pcp_c, copay_spec_c = cents("deductible"), cents("copay_pcp"), cents("copay_specialist")
    coins_pct = np.array([int(r["coinsurance_pct"]) for r in plan_rows], dtype=np.int64)
    ded = np.zeros(len(li), np.int64)
    ded_line = first & (c.cj[li] == 0) & paid_line
    ded[ded_line] = np.minimum(allowed[ded_line], deductible_c[li][ded_line])
    is_pcp = np.array([inp.ptype.get(b) == ProviderType.PCP for b in c.billing], dtype=bool) if n else np.zeros(0, bool)
    copay_amt = np.where(ctype == ClaimType.PHARMACY, np.array([catalog.TIER_COPAY_USD[t] * 100 for t in c.tier], dtype=np.int64),
                         np.where(is_pcp, copay_pcp_c, copay_spec_c))
    copay = np.zeros(len(li), np.int64)
    cp_line = first & paid_line & np.isin(lt, [ClaimType.PROFESSIONAL, ClaimType.PHARMACY])
    copay[cp_line] = np.minimum(allowed[cp_line] - ded[cp_line], copay_amt[li][cp_line])
    coins = np.zeros(len(li), np.int64)
    co_line = paid_line & ~cp_line
    coins[co_line] = ((allowed[co_line] - ded[co_line]) * coins_pct[li][co_line]) // 100
    paid = allowed - ded - copay - coins  # F-02; member responsibility = ded + copay + coins (the PR adjustments)

    status_l = c.status[li]
    c.line_status = np.where(status_l == ClaimStatus.PAID, ClaimStatus.PAID,
                             np.where(status_l == ClaimStatus.DENIED, ClaimStatus.DENIED, ClaimStatus.PENDED))

    def per_claim(x: np.ndarray) -> np.ndarray:
        return np.bincount(li, weights=x, minlength=n).astype(np.int64)

    c.allowed_full, c.billed, c.allowed, c.coins_pct = allowed_full, billed, allowed, coins_pct
    c.ded, c.copay, c.coins, c.paid = ded, copay, coins, paid
    c.tot_billed, c.tot_allowed, c.tot_paid = per_claim(billed), per_claim(allowed), per_claim(paid)
    c.member_resp = per_claim(ded + copay + coins)
