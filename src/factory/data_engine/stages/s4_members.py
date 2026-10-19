"""Stage s4: members, enrollment, PCP assignments, risk, gaps, SDOH, caregivers. Sharded by member index.

gen_shard draws each member's core facts into a MemberBatch (LOB, plan, scenario), then one writer per entity
adds its own draws. Every value comes from the member's own random stream (seed, entity, member index), so rows
are identical whichever shard or worker produces them. Member scenarios (PEV, MBN, MPS, MPC, MAS) are assigned
first and force the facts their recipes need (retro termination, caregiver scope, transport need, PCP switch, ...).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pyarrow as pa

from factory.codes import EnrollmentStatus, GeoType, Lob, PcpAssignmentStatus
from factory.data_engine import catalog, keys
from factory.data_engine.context import GenContext, dates
from factory.data_engine.rng import RowRandom

DAY = np.timedelta64(1, "D")
ENROLLMENT_LOOKBACK_MONTHS = 12


def month_start(d: np.ndarray) -> np.ndarray:
    """Truncate dates to the first day of their month (enrollments start on the 1st)."""
    return d.astype("datetime64[M]").astype("datetime64[D]")


def load_plans(ctx: GenContext) -> dict:
    """Plan ids grouped by line of business, read back from the TableStore (stages only share data through stores)."""
    t = ctx.stores.tables.query("SELECT plan_id, lob FROM plan ORDER BY plan_code")
    ids, lobs = t["plan_id"].to_pylist(), t["lob"].to_pylist()
    by_lob: dict[str, list[str]] = {}
    for i, lob in zip(ids, lobs):
        by_lob.setdefault(lob, []).append(i)
    return by_lob


def load_pcps(ctx: GenContext) -> dict[str, list[str]]:
    """Participating PCPs with an open or limited panel, per plan (R-05)."""
    t = ctx.stores.tables.query(
        """SELECT np.plan_id, p.npi FROM network_participation np JOIN provider p ON p.npi = np.npi
           WHERE p.provider_type = 'PCP' AND p.status = 'ACTIVE' AND np.status = 'PARTICIPATING'
             AND np.panel_status <> 'CLOSED' AND np.term_dt IS NULL
           ORDER BY np.plan_id, p.npi"""
    )
    out: dict[str, list[str]] = {}
    for plan, npi in zip(t["plan_id"].to_pylist(), t["npi"].to_pylist()):
        out.setdefault(plan, []).append(npi)
    return out


def pick(options_by_key: dict[str, list[str]], keys: np.ndarray, u: np.ndarray) -> np.ndarray:
    """Vectorised choice: for each row, pick options_by_key[keys[i]] at position u[i] * len(options).

    Rows whose key has no options get None (e.g. a plan with no eligible PCP).
    """
    out = np.empty(len(keys), dtype=object)
    for k, opts in options_by_key.items():
        m = keys == k
        if m.any() and opts:
            out[m] = np.asarray(opts, dtype=object)[np.minimum((u[m] * len(opts)).astype(int), len(opts) - 1)]
    return out


@dataclass(eq=False)
class MemberInputs:
    """What every task reads once: plan ids per LOB, and eligible PCPs per plan (only when PCPs are generated)."""

    plans_by_lob: dict[str, list[str]]
    pcps: dict[str, list[str]]


_ = None  # placeholder default: a writer fills the field


@dataclass(eq=False)
class MemberBatch:
    """Per-member arrays for one shard (member indexes lo..hi-1)."""

    idx: np.ndarray  # member index
    rr: RowRandom  # member_demographics row streams: every member-level draw is keyed by its name
    scen: np.ndarray
    lob: np.ndarray
    plan_id: np.ndarray
    # ---- set by _write_demographics
    member_id: np.ndarray = _
    member_no: list[str] = _
    sex: np.ndarray = _
    last: np.ndarray = _
    # ---- set by _write_enrollment (current span)
    eff: np.ndarray = _
    term: np.ndarray = _

    @property
    def n(self) -> int:
        return len(self.idx)


def gen_shard(ctx: GenContext, lo: int, hi: int, inp: MemberInputs) -> None:
    """Generate all member-anchored entities for member indexes [lo, hi)."""
    b = _members(ctx, lo, hi, inp)
    _write_demographics(ctx, b)
    _write_enrollment(ctx, b)
    if ctx.wants("pcp_assignment"):
        _write_pcp_assignments(ctx, b, inp.pcps)
    if ctx.wants("risk_score"):
        _write_risk_scores(ctx, b)
    if ctx.wants("care_gap"):
        _write_care_gaps(ctx, b)
    if ctx.wants("sdoh_indicator"):
        _write_sdoh(ctx, b)
    if ctx.wants("family_relationship"):
        _write_household(ctx, b)


def _members(ctx: GenContext, lo: int, hi: int, inp: MemberInputs) -> MemberBatch:
    """LOB from the payload mix, a plan of that LOB, and the member scenario tag."""
    idx = np.arange(lo, hi)
    rr = ctx.rr("member_demographics", idx)
    lob_mix = ctx.spec.distributions.get("lob_mix", {Lob.COMMERCIAL: 1})
    lob = rr.choice("lob", list(lob_mix), list(lob_mix.values()))
    plan_id = pick(inp.plans_by_lob, lob, rr.u("plan"))
    return MemberBatch(idx=idx, rr=rr, scen=ctx.scenarios("member", rr), lob=lob, plan_id=plan_id)


def _write_demographics(ctx: GenContext, b: MemberBatch) -> None:
    """Age by LOB band, name, place (by the payload geo mix), language, channel, accessibility needs."""
    rr, idx, n, lob, scen = b.rr, b.idx, b.n, b.lob, b.scen
    dist = ctx.spec.distributions
    age = np.zeros(n, dtype=np.int64)
    for name, bands in catalog.AGE_BANDS.items():
        m = lob == name
        if m.any():
            band = rr.choice(f"band:{name}", list(range(len(bands))), [x[2] for x in bands])[m].astype(int)
            lo_age = np.array([bands[x][0] for x in band])
            hi_age = np.array([bands[x][1] for x in band])
            age[m] = lo_age + np.floor(rr.u("age")[m] * (hi_age - lo_age + 1)).astype(int)
    dob = ctx.as_of - (age * 365 + rr.integers("dob_day", 0, 365)).astype("timedelta64[D]")
    sex = rr.choice("sex", ["F", "M", "U"], [55, 44.5, 0.5])
    first = np.where(sex == "F", rr.choice("first_f", catalog.FIRST_F), rr.choice("first_m", catalog.FIRST_M))
    last = rr.choice("last", catalog.LAST)
    geo_mix = dist.get("geo_mix")
    if geo_mix:  # payload geo mix: pick URBAN / SUBURBAN / RURAL first, then a place of that type
        geo = rr.choice("geo", list(geo_mix), list(geo_mix.values()))
    else:
        geo = np.array([catalog.PLACES[i][5] for i in rr.integers("geo_place", 0, len(catalog.PLACES))], dtype=object)
    geo = np.where(scen == "MPS-02", GeoType.RURAL, geo)
    by_geo = {g: [i for i, pl in enumerate(catalog.PLACES) if pl[5] == g] for g in (GeoType.URBAN, GeoType.SUBURBAN, GeoType.RURAL)}
    u_place = rr.u("place")
    place_idx = np.array([by_geo[g][min(int(u * len(by_geo[g])), len(by_geo[g]) - 1)] for g, u in zip(geo, u_place)], dtype=np.int64)
    places = [catalog.PLACES[p] for p in place_idx]
    lang_mix = dist.get("language_mix") or {"EN": 85, "ES": 10, "OTHER": 5}
    lang = rr.choice("lang", list(lang_mix), list(lang_mix.values()))
    lang = np.where(scen == "MPS-02", "ES", lang)
    channel = rr.choice("channel", ["VOICE", "CHAT", "SMS"], [55, 30, 15])
    needs = [[a for a, f in (("HEARING", h), ("LARGE_PRINT", v), ("INTERPRETER", i)) if f] for h, v, i in
             zip(rr.bernoulli("hearing", np.where(lob == Lob.MEDICARE_ADV, 0.15, 0.03)),
                 rr.bernoulli("vision", np.where(lob == Lob.MEDICARE_ADV, 0.10, 0.02)),
                 (lang != "EN") & rr.bernoulli("interp", 0.5))]
    member_no = [f"MBR-{i + 1:07d}" for i in idx]
    last4 = rr.integers("last4", 0, 10000)
    member_id = np.asarray(ctx.ids("member_demographics", idx), dtype=object)
    ctx.write("member_demographics", {
        "member_id": pa.array(member_id.tolist()),
        "member_number": pa.array(member_no),
        "persona_type": pa.array([catalog.LOB_PERSONA[x] for x in lob]),
        "lob": pa.array(lob.tolist()),
        "first_name": pa.array(first.tolist()),
        "last_name": pa.array(last.tolist()),
        "dob": dates(dob),
        "sex_at_birth": pa.array(sex.tolist()),
        "address": pa.array([f"{10 + int(x)} {s} Ave" for x, s in
                             zip(rr.integers("street_no", 0, 9900), rr.choice("street", ["Lake", "Hill", "Park", "River", "Sunset", "Church"]))]),
        "city": pa.array([p[0] for p in places]),
        "state": pa.array([p[1] for p in places]),
        "zip": pa.array([p[2] for p in places]),
        "lat": pa.array(np.round([p[3] for p in places] + rr.normal("lat", 0, 0.05), 5)),
        "lon": pa.array(np.round([p[4] for p in places] + rr.normal("lon", 0, 0.05), 5)),
        "geo_type": pa.array([p[5] for p in places]),
        "preferred_language": pa.array(lang.tolist()),
        "accessibility_needs": pa.array(needs, pa.list_(pa.string())),
        "preferred_channel": pa.array(channel.tolist()),
        "phone": pa.array([f"({200 + int(a)}) 555-01{int(x):02d}" for a, x in
                           zip(rr.integers("area", 0, 790), rr.integers("ph", 0, 100))]),
        "email": pa.array([f"member{i + 1}@example.test" for i in idx]),
        "verification_factors": pa.array([json.dumps({"dob": str(d), "zip": p[2], "last4_synthetic": f"{int(l4):04d}"})
                                          for d, p, l4 in zip(dob, places, last4)]),
    }, scenario=scen)
    b.member_id, b.member_no, b.sex, b.last = member_id, member_no, sex, last


def _write_enrollment(ctx: GenContext, b: MemberBatch) -> None:
    """Current span (scenario PEV-02 retro-termed, PEV-03 pending) plus a prior span for 15% of members.

    Spans start at least 12 months back, so any claims window up to 12 months is covered (R-02).
    """
    rr, idx, n, scen = b.rr, b.idx, b.n, b.scen
    hist_start = ctx.as_of - np.timedelta64(ENROLLMENT_LOOKBACK_MONTHS * 31, "D")
    eff = month_start(hist_start - rr.integers("enroll_back", 0, 900).astype("timedelta64[D]"))
    status = np.full(n, EnrollmentStatus.ACTIVE, dtype=object)
    term = np.full(n, np.datetime64("NaT", "D"), dtype="datetime64[D]")
    retro = np.zeros(n, dtype=bool)
    base_termed = (scen == "BASE") | (scen == "PEV-01")
    past_term = base_termed & rr.bernoulli("termed", 0.02)
    status[past_term] = EnrollmentStatus.TERMED
    term[past_term] = ctx.as_of - rr.integers("term_back", 10, 40)[past_term].astype("timedelta64[D]")
    m2 = scen == "PEV-02"  # termed retroactively: term date yesterday
    status[m2], term[m2], retro[m2] = EnrollmentStatus.TERMED, ctx.as_of - DAY, True
    m3 = scen == "PEV-03"  # enrollment pending, retro to application
    status[m3], eff[m3], retro[m3] = EnrollmentStatus.PENDING, month_start(ctx.as_of - 20 * DAY), True
    premium = np.where(b.lob == Lob.MEDICAID_MCO, "NA", rr.choice("premium", ["PAID", "DUE", "PAST_DUE"], [92, 5, 3]))
    prior = rr.bernoulli("prior_span", 0.15)
    p_idx = idx[prior]
    p_eff = month_start(eff[prior] - rr.integers("prior_len", 365, 730)[prior].astype("timedelta64[D]"))
    ctx.write("plan_enrollment", {
        "enrollment_id": pa.array(ctx.ids("plan_enrollment", np.concatenate([
            keys.member_key(idx, 0, keys.SPAN_SLOTS), keys.member_key(p_idx, 1, keys.SPAN_SLOTS)]))),
        "member_id": pa.array(np.concatenate([b.member_id, b.member_id[prior]]).tolist()),
        "plan_id": pa.array(np.concatenate([b.plan_id, b.plan_id[prior]]).tolist()),
        "effective_dt": dates(np.concatenate([eff, p_eff])),
        "term_dt": dates(np.concatenate([term, eff[prior] - DAY])),
        "status": pa.array(np.concatenate([status, np.full(len(p_idx), EnrollmentStatus.TERMED, dtype=object)]).tolist()),
        "premium_status": pa.array(np.concatenate([premium, premium[prior]]).tolist()),
        "cob_flag": pa.array(np.concatenate([rr.bernoulli("cob", 0.05), np.zeros(len(p_idx), bool)])),
        "retro_change_flag": pa.array(np.concatenate([retro, np.zeros(len(p_idx), bool)])),
    }, scenario=np.concatenate([scen, scen[prior]]))
    b.eff, b.term = eff, term


def _write_pcp_assignments(ctx: GenContext, b: MemberBatch, pcps: dict[str, list[str]]) -> None:
    """One PCP per member from the plan's open panels (R-05); 20% (and MPC-02/03) switched within the span."""
    rr, idx, scen, eff, term, member_id, plan_id = b.rr, b.idx, b.scen, b.eff, b.term, b.member_id, b.plan_id
    pcp = pick(pcps, plan_id, rr.u("pcp"))
    has = pcp != None  # noqa: E711 - object array comparison
    span_end = np.where(np.isnat(term), ctx.as_of, term)
    a_status = np.where(np.isnat(term), PcpAssignmentStatus.ACTIVE, PcpAssignmentStatus.ENDED)
    forced_switch = np.isin(scen, ["MPC-02", "MPC-03"])
    switch = has & (rr.bernoulli("pcp_switch", 0.2) | forced_switch) & ((span_end - eff) > 2 * DAY)
    sw_dt = eff + np.floor(rr.u("switch_at") * ((span_end - eff).astype(int) - 1)).astype("timedelta64[D]") + DAY
    old_pcp = pick(pcps, plan_id, rr.u("old_pcp"))
    cur_eff = np.where(switch, sw_dt, eff)
    keep = has
    sk = switch & (old_pcp != None)  # noqa: E711
    ctx.write("pcp_assignment", {
        "assignment_id": pa.array(ctx.ids("pcp_assignment", np.concatenate([
            keys.member_key(idx[keep], 0, keys.SPAN_SLOTS), keys.member_key(idx[sk], 1, keys.SPAN_SLOTS)]))),
        "member_id": pa.array(np.concatenate([member_id[keep], member_id[sk]]).tolist()),
        "pcp_npi": pa.array(np.concatenate([pcp[keep], old_pcp[sk]]).tolist()),
        "plan_id": pa.array(np.concatenate([plan_id[keep], plan_id[sk]]).tolist()),
        "effective_dt": dates(np.concatenate([cur_eff[keep], eff[sk]])),
        "term_dt": dates(np.concatenate([term[keep], sw_dt[sk] - DAY])),
        "assignment_type": pa.array(np.concatenate([np.where(scen == "MPC-02", "AUTO_ASSIGNED",
                                                             rr.choice("atype", ["MEMBER_SELECTED", "AUTO_ASSIGNED"], [70, 30]))[keep],
                                                    np.full(sk.sum(), "MEMBER_SELECTED", dtype=object)]).tolist()),
        "status": pa.array(np.concatenate([a_status[keep], np.full(sk.sum(), PcpAssignmentStatus.ENDED, dtype=object)]).tolist()),
        "change_reason": pa.array(np.concatenate([np.full(keep.sum(), None, dtype=object),
                                                  np.where(scen == "MPC-02", "PROVIDER_TERMED", np.where(scen == "MPC-03", "MOVED",
                                                           rr.choice("reason", ["MOVED", "PROVIDER_TERMED", "PREFERENCE", "ACCESS"])))[sk]]).tolist(), pa.string()),
    }, scenario=np.concatenate([scen[keep], scen[sk]]))


def _write_risk_scores(ctx: GenContext, b: MemberBatch) -> None:
    """HCC RAF (log-normal around an LOB median, per row) and LACE per member."""
    rr, idx, n = b.rr, b.idx, b.n
    median = np.where(b.lob == Lob.MEDICARE_ADV, 1.0, 0.6)  # per row, never per shard
    raf = np.round(np.clip(median * np.exp(rr.normal("raf", 0.0, 0.55)), 0.1, 5.0), 3)
    lace = rr.integers("lace", 0, 20).astype(float)
    tier = np.where(raf < 1.0, "LOW", np.where(raf < 2.0, "RISING", "HIGH"))
    tier_l = np.where(lace < 5, "LOW", np.where(lace < 10, "RISING", "HIGH"))
    ctx.write("risk_score", {
        "risk_score_id": pa.array(ctx.ids("risk_score", np.concatenate([
            keys.member_key(idx, 0, keys.RISK_SLOTS), keys.member_key(idx, 1, keys.RISK_SLOTS)]))),
        "member_id": pa.array(np.concatenate([b.member_id, b.member_id]).tolist()),
        "as_of_dt": dates(np.full(2 * n, ctx.as_of - 30 * DAY)),
        "model": pa.array(["HCC_RAF"] * n + ["LACE"] * n),
        "score": pa.array(np.concatenate([raf, lace])),
        "scale_min": pa.array(np.zeros(2 * n)),
        "scale_max": pa.array(np.concatenate([np.full(n, 5.0), np.full(n, 19.0)])),
        "tier": pa.array(np.concatenate([tier, tier_l]).tolist()),
    }, scenario=np.concatenate([b.scen, b.scen]))


def _write_care_gaps(ctx: GenContext, b: MemberBatch) -> None:
    """Open or excluded gaps (closing needs a paid claim, R-09); MAS-02 members get an open one."""
    rr, idx, n, scen = b.rr, b.idx, b.n, b.scen
    k = np.minimum(rr.poisson("gaps", 0.9), 3)
    k = np.where(scen == "MAS-02", np.maximum(k, 1), k)
    gi = np.repeat(np.arange(n), k)
    gj = np.concatenate([np.arange(x) for x in k]) if len(gi) else np.array([], int)
    gkey = keys.member_key(idx[gi], gj, keys.GAP_SLOTS)
    grr = ctx.rr("care_gap", gkey)
    measure = grr.choice("measure", ["HBD", "CBP", "COL", "SPC", "BCS", "TRC"])
    measure = np.where((measure == "BCS") & (b.sex[gi] != "F"), "CBP", measure)
    ctx.write("care_gap", {
        "gap_id": pa.array(ctx.ids("care_gap", gkey)),
        "member_id": pa.array(b.member_id[gi].tolist()),
        "measure": pa.array(measure.tolist()),
        "status": pa.array(np.where((scen[gi] == "MAS-02") & (gj == 0), "OPEN",
                                    grr.choice("status", ["OPEN", "EXCLUDED"], [85, 15])).tolist()),
        "due_dt": dates(np.full(len(gi), np.datetime64(f"{str(ctx.as_of)[:4]}-12-31"))),
        "closed_by_claim_id": pa.array([None] * len(gi), pa.string()),
    }, scenario=scen[gi])


def _write_sdoh(ctx: GenContext, b: MemberBatch) -> None:
    """SDOH screening for 60% of members; MBN-03 and MAS-02 have a positive transport need."""
    rr, idx, scen = b.rr, b.idx, b.scen
    transport = np.isin(scen, ["MBN-03", "MAS-02"])
    s = rr.bernoulli("screened", 0.6) | transport
    pos_rate = np.where(b.lob == Lob.MEDICAID_MCO, 0.40, 0.18)
    domain = np.where(transport, "TRANSPORT", rr.choice("sdoh_domain", ["FOOD", "HOUSING", "TRANSPORT", "UTILITIES", "SAFETY", "ISOLATION"]))
    positive = rr.bernoulli("sdoh_pos", pos_rate) | transport
    ctx.write("sdoh_indicator", {
        "indicator_id": pa.array(ctx.ids("sdoh_indicator", idx[s])),
        "member_id": pa.array(b.member_id[s].tolist()),
        "domain": pa.array(domain[s].tolist()),
        "positive": pa.array(positive[s]),
        "screened_dt": dates(ctx.as_of - rr.integers("screened_dt", 10, 300)[s].astype("timedelta64[D]")),
        "source": pa.array(rr.choice("sdoh_src", ["SCREENER", "CLAIMS_Z_CODE", "SELF_REPORT"])[s].tolist()),
        "referral_status": pa.array(rr.choice("sdoh_ref", ["NONE", "SENT", "CONNECTED"], [60, 25, 15])[s].tolist()),
    }, scenario=scen[s])


def _write_household(ctx: GenContext, b: MemberBatch) -> None:
    """A self row per member plus a caregiver for 10% (20% of consents expired); MBN-04/MPC-04 get a narrow scope."""
    rr, idx, n, scen = b.rr, b.idx, b.n, b.scen
    narrow = {sid: catalog.SCENARIOS[sid]["caregiver_scope"] for sid in ("MBN-04", "MPC-04")}
    forced_cg = np.isin(scen, list(narrow))
    cg = rr.bernoulli("caregiver", 0.10) | forced_cg
    ci = idx[cg]
    rel = rr.choice("cg_rel", ["ADULT_CHILD", "SPOUSE", "PARENT", "POA"], [55, 25, 15, 5])[cg]
    scope_all = rr.bernoulli("cg_all", 0.3)[cg]
    scopes = [["ALL"] if a else sorted({x, y}) for a, x, y in zip(
        scope_all, rr.choice("cg_s1", ["BENEFITS", "CLAIMS", "BILLING", "PA_STATUS", "APPOINTMENTS", "PCP_CHANGE"])[cg],
        rr.choice("cg_s2", ["BENEFITS", "CLAIMS", "PA_STATUS", "APPOINTMENTS"])[cg])]
    scopes = [narrow[sc] if sc in narrow else x for sc, x in zip(scen[cg], scopes)]
    c_eff = ctx.as_of - rr.integers("cg_eff", 60, 900)[cg].astype("timedelta64[D]")
    expired = rr.bernoulli("cg_expired", 0.2)[cg] & ~forced_cg[cg]
    c_exp = np.where(expired, ctx.as_of - rr.integers("cg_exp_past", 1, 50)[cg].astype("timedelta64[D]"),
                     ctx.as_of + rr.integers("cg_exp_future", 30, 700)[cg].astype("timedelta64[D]"))
    n_c = int(cg.sum())
    member_no = np.array(b.member_no, dtype=object)
    no_date = np.full(n, np.datetime64("NaT", "D"), "datetime64[D]")
    ctx.write("family_relationship", {
        "relationship_id": pa.array(ctx.ids("family_relationship", np.concatenate([
            keys.member_key(idx, 0, keys.SPAN_SLOTS), keys.member_key(ci, 1, keys.SPAN_SLOTS)]))),
        "member_id": pa.array(np.concatenate([b.member_id, b.member_id[cg]]).tolist()),
        "related_member_id": pa.array([None] * (n + n_c), pa.string()),
        "subscriber_id": pa.array(np.concatenate([member_no, member_no[cg]]).tolist()),
        "relationship_code": pa.array(["18"] * n + ["01" if r == "SPOUSE" else "OTHER" for r in rel]),
        "caregiver_flag": pa.array([False] * n + [True] * n_c),
        "caregiver_name": pa.array([None] * n + [f"{f} {l}" for f, l in zip(rr.choice("cg_first", catalog.FIRST_F + catalog.FIRST_M)[cg], b.last[cg])], pa.string()),
        "caregiver_relationship": pa.array([None] * n + rel.tolist(), pa.string()),
        "authorization_scope": pa.array([[]] * n + scopes, pa.list_(pa.string())),
        "consent_effective_dt": dates(np.concatenate([no_date, c_eff])),
        "consent_expiry_dt": dates(np.concatenate([no_date, c_exp])),
        "verified": pa.array(np.concatenate([np.ones(n, bool), rr.bernoulli("cg_verified", 0.9)[cg] | forced_cg[cg]])),
    }, scenario=np.concatenate([scen, scen[cg]]))


def prepare(ctx: GenContext) -> MemberInputs:
    """Read what every task needs once: plans by LOB and eligible PCPs per plan."""
    return MemberInputs(plans_by_lob=load_plans(ctx), pcps=load_pcps(ctx) if ctx.wants("pcp_assignment") else {})


def task_count(ctx: GenContext, prep: MemberInputs) -> int:
    """One task per member shard."""
    return len(ctx.shard_ranges(int(ctx.spec.volumes["members"])))


def run_task(ctx: GenContext, index: int, prep: MemberInputs) -> None:
    """Generate one shard (a member index range). Safe to re-run: rows are upserted on deterministic ids."""
    lo, hi = ctx.shard_ranges(int(ctx.spec.volumes["members"]))[index]
    gen_shard(ctx, lo, hi, prep)


ENTITIES = ["member_demographics", "plan_enrollment", "pcp_assignment", "risk_score", "care_gap", "sdoh_indicator", "family_relationship"]


def run(ctx: GenContext) -> list[str]:
    """Run every task in this process (without the executor)."""
    prep = prepare(ctx)
    for i in range(task_count(ctx, prep)):
        run_task(ctx, i, prep)
    return [e for e in ENTITIES if ctx.wants(e)]
