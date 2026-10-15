"""Stage s4: members, enrollment, PCP assignments, risk, gaps, SDOH, caregivers. Sharded by member index."""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa

from factory.data_engine import catalog
from factory.data_engine.context import GenContext, dates
from factory.data_engine.rng import RowRandom

DAY = np.timedelta64(1, "D")


def month_start(d: np.ndarray) -> np.ndarray:
    return d.astype("datetime64[M]").astype("datetime64[D]")


def load_plans(ctx: GenContext) -> dict:
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
    out = np.empty(len(keys), dtype=object)
    for k, opts in options_by_key.items():
        m = keys == k
        if m.any() and opts:
            out[m] = np.asarray(opts, dtype=object)[np.minimum((u[m] * len(opts)).astype(int), len(opts) - 1)]
    return out


def gen_shard(ctx: GenContext, lo: int, hi: int, plans_by_lob: dict, pcps: dict) -> None:
    idx = np.arange(lo, hi)
    n = len(idx)
    rr = ctx.rr("member_demographics", idx)
    dist = ctx.bp.data_spec.distributions
    lob_mix = dist.get("lob_mix", {"COMMERCIAL": 1})
    lob = rr.choice("lob", list(lob_mix), list(lob_mix.values()))
    plan_id = pick(plans_by_lob, lob, rr.u("plan"))
    scen = ctx.scenarios("member", rr)

    # ---- demographics
    age = np.zeros(n, dtype=np.int64)
    for name, bands in catalog.AGE_BANDS.items():
        m = lob == name
        if m.any():
            band = rr.choice(f"band:{name}", list(range(len(bands))), [b[2] for b in bands])[m].astype(int)
            lo_age = np.array([bands[b][0] for b in band])
            hi_age = np.array([bands[b][1] for b in band])
            age[m] = lo_age + np.floor(rr.u("age")[m] * (hi_age - lo_age + 1)).astype(int)
    dob = ctx.as_of - (age * 365 + rr.integers("dob_day", 0, 365)).astype("timedelta64[D]")
    sex = rr.choice("sex", ["F", "M", "U"], [55, 44.5, 0.5])
    first = np.where(sex == "F", rr.choice("first_f", catalog.FIRST_F), rr.choice("first_m", catalog.FIRST_M))
    last = rr.choice("last", catalog.LAST)
    place_idx = rr.integers("place", 0, len(catalog.PLACES))
    places = [catalog.PLACES[p] for p in place_idx]
    lang_mix = dist.get("language_mix") or {"EN": 85, "ES": 10, "OTHER": 5}
    lang = rr.choice("lang", list(lang_mix), list(lang_mix.values()))
    channel = rr.choice("channel", ["VOICE", "CHAT", "SMS"], [55, 30, 15])
    needs = [[a for a, f in (("HEARING", h), ("LARGE_PRINT", v), ("INTERPRETER", i)) if f] for h, v, i in
             zip(rr.bernoulli("hearing", np.where(lob == "MEDICARE_ADV", 0.15, 0.03)),
                 rr.bernoulli("vision", np.where(lob == "MEDICARE_ADV", 0.10, 0.02)),
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
        "phone": pa.array([f"({200 + int(a)}) 555-01{int(b):02d}" for a, b in
                           zip(rr.integers("area", 0, 790), rr.integers("ph", 0, 100))]),
        "email": pa.array([f"member{i + 1}@example.test" for i in idx]),
        "verification_factors": pa.array([json.dumps({"dob": str(d), "zip": p[2], "last4_synthetic": f"{int(l):04d}"})
                                          for d, p, l in zip(dob, places, last4)]),
    }, scenario=scen)

    # ---- enrollment (current span + 15% prior span)
    hist_start = ctx.as_of - np.timedelta64(ctx.bp.data_spec.history_months * 31, "D")
    eff = month_start(hist_start - rr.integers("enroll_back", 0, 900).astype("timedelta64[D]"))
    status = np.full(n, "ACTIVE", dtype=object)
    term = np.full(n, np.datetime64("NaT", "D"), dtype="datetime64[D]")
    retro = np.zeros(n, dtype=bool)
    base_termed = (scen == "BASE") | (scen == "PEV-01")
    past_term = base_termed & rr.bernoulli("termed", 0.02)
    status[past_term] = "TERMED"
    term[past_term] = ctx.as_of - rr.integers("term_back", 10, 40)[past_term].astype("timedelta64[D]")
    m2 = scen == "PEV-02"  # termed retroactively: term date yesterday
    status[m2], term[m2], retro[m2] = "TERMED", ctx.as_of - DAY, True
    m3 = scen == "PEV-03"  # enrollment pending, retro to application
    status[m3], eff[m3], retro[m3] = "PENDING", month_start(ctx.as_of - 20 * DAY), True
    premium = np.where(lob == "MEDICAID_MCO", "NA", rr.choice("premium", ["PAID", "DUE", "PAST_DUE"], [92, 5, 3]))
    prior = rr.bernoulli("prior_span", 0.15)
    p_idx = idx[prior]
    p_eff = month_start(eff[prior] - rr.integers("prior_len", 365, 730)[prior].astype("timedelta64[D]"))
    ctx.write("plan_enrollment", {
        "enrollment_id": pa.array(ctx.ids("plan_enrollment", np.concatenate([idx * 4, p_idx * 4 + 1]))),
        "member_id": pa.array(np.concatenate([member_id, member_id[prior]]).tolist()),
        "plan_id": pa.array(np.concatenate([plan_id, plan_id[prior]]).tolist()),
        "effective_dt": dates(np.concatenate([eff, p_eff])),
        "term_dt": dates(np.concatenate([term, eff[prior] - DAY])),
        "status": pa.array(np.concatenate([status, np.full(len(p_idx), "TERMED", dtype=object)]).tolist()),
        "premium_status": pa.array(np.concatenate([premium, premium[prior]]).tolist()),
        "cob_flag": pa.array(np.concatenate([rr.bernoulli("cob", 0.05), np.zeros(len(p_idx), bool)])),
        "retro_change_flag": pa.array(np.concatenate([retro, np.zeros(len(p_idx), bool)])),
    }, scenario=np.concatenate([scen, scen[prior]]))

    # ---- PCP assignment: one per member, 20% with an earlier ENDED assignment in the same span
    if ctx.wants("pcp_assignment"):
        pcp = pick(pcps, plan_id, rr.u("pcp"))
        has = pcp != None  # noqa: E711 - object array comparison
        span_end = np.where(np.isnat(term), ctx.as_of, term)
        a_status = np.where(np.isnat(term), "ACTIVE", "ENDED")
        switch = has & rr.bernoulli("pcp_switch", 0.2) & ((span_end - eff) > 2 * DAY)
        sw_dt = eff + np.floor(rr.u("switch_at") * ((span_end - eff).astype(int) - 1)).astype("timedelta64[D]") + DAY
        old_pcp = pick(pcps, plan_id, rr.u("old_pcp"))
        cur_eff = np.where(switch, sw_dt, eff)
        keep = has
        sk = switch & (old_pcp != None)  # noqa: E711
        ctx.write("pcp_assignment", {
            "assignment_id": pa.array(ctx.ids("pcp_assignment", np.concatenate([idx[keep] * 4, idx[sk] * 4 + 1]))),
            "member_id": pa.array(np.concatenate([member_id[keep], member_id[sk]]).tolist()),
            "pcp_npi": pa.array(np.concatenate([pcp[keep], old_pcp[sk]]).tolist()),
            "plan_id": pa.array(np.concatenate([plan_id[keep], plan_id[sk]]).tolist()),
            "effective_dt": dates(np.concatenate([cur_eff[keep], eff[sk]])),
            "term_dt": dates(np.concatenate([term[keep], sw_dt[sk] - DAY])),
            "assignment_type": pa.array(np.concatenate([rr.choice("atype", ["MEMBER_SELECTED", "AUTO_ASSIGNED"], [70, 30])[keep],
                                                        np.full(sk.sum(), "MEMBER_SELECTED", dtype=object)]).tolist()),
            "status": pa.array(np.concatenate([a_status[keep], np.full(sk.sum(), "ENDED", dtype=object)]).tolist()),
            "change_reason": pa.array(np.concatenate([np.full(keep.sum(), None, dtype=object),
                                                      rr.choice("reason", ["MOVED", "PROVIDER_TERMED", "PREFERENCE", "ACCESS"])[sk]]).tolist(), pa.string()),
        }, scenario=np.concatenate([scen[keep], scen[sk]]))

    # ---- risk scores: HCC RAF + LACE per member
    if ctx.wants("risk_score"):
        median = np.where(lob == "MEDICARE_ADV", 1.0, 0.6)  # per row, never per shard
        raf = np.round(np.clip(median * np.exp(rr.normal("raf", 0.0, 0.55)), 0.1, 5.0), 3)
        lace = rr.integers("lace", 0, 20).astype(float)
        tier = np.where(raf < 1.0, "LOW", np.where(raf < 2.0, "RISING", "HIGH"))
        tier_l = np.where(lace < 5, "LOW", np.where(lace < 10, "RISING", "HIGH"))
        ctx.write("risk_score", {
            "risk_score_id": pa.array(ctx.ids("risk_score", np.concatenate([idx * 2, idx * 2 + 1]))),
            "member_id": pa.array(np.concatenate([member_id, member_id]).tolist()),
            "as_of_dt": dates(np.full(2 * n, ctx.as_of - 30 * DAY)),
            "model": pa.array(["HCC_RAF"] * n + ["LACE"] * n),
            "score": pa.array(np.concatenate([raf, lace])),
            "scale_min": pa.array(np.zeros(2 * n)),
            "scale_max": pa.array(np.concatenate([np.full(n, 5.0), np.full(n, 19.0)])),
            "tier": pa.array(np.concatenate([tier, tier_l]).tolist()),
        }, scenario=np.concatenate([scen, scen]))

    # ---- care gaps (open or excluded; closing needs a paid claim, R-09)
    if ctx.wants("care_gap"):
        k = np.minimum(rr.poisson("gaps", 0.9), 3)
        gi = np.repeat(np.arange(n), k)
        gj = np.concatenate([np.arange(c) for c in k]) if len(gi) else np.array([], int)
        grr = RowRandom(ctx.seed, "care_gap", idx[gi] * 8 + gj)
        measure = grr.choice("measure", ["HBD", "CBP", "COL", "SPC", "BCS", "TRC"])
        measure = np.where((measure == "BCS") & (sex[gi] != "F"), "CBP", measure)
        ctx.write("care_gap", {
            "gap_id": pa.array(ctx.ids("care_gap", idx[gi] * 8 + gj)),
            "member_id": pa.array(member_id[gi].tolist()),
            "measure": pa.array(measure.tolist()),
            "status": pa.array(grr.choice("status", ["OPEN", "EXCLUDED"], [85, 15]).tolist()),
            "due_dt": dates(np.full(len(gi), np.datetime64(f"{str(ctx.as_of)[:4]}-12-31"))),
            "closed_by_claim_id": pa.array([None] * len(gi), pa.string()),
        }, scenario=scen[gi])

    # ---- SDOH screening
    if ctx.wants("sdoh_indicator"):
        s = rr.bernoulli("screened", 0.6)
        pos_rate = np.where(lob == "MEDICAID_MCO", 0.40, 0.18)
        ctx.write("sdoh_indicator", {
            "indicator_id": pa.array(ctx.ids("sdoh_indicator", idx[s])),
            "member_id": pa.array(member_id[s].tolist()),
            "domain": pa.array(rr.choice("sdoh_domain", ["FOOD", "HOUSING", "TRANSPORT", "UTILITIES", "SAFETY", "ISOLATION"])[s].tolist()),
            "positive": pa.array(rr.bernoulli("sdoh_pos", pos_rate)[s]),
            "screened_dt": dates(ctx.as_of - rr.integers("screened_dt", 10, 300)[s].astype("timedelta64[D]")),
            "source": pa.array(rr.choice("sdoh_src", ["SCREENER", "CLAIMS_Z_CODE", "SELF_REPORT"])[s].tolist()),
            "referral_status": pa.array(rr.choice("sdoh_ref", ["NONE", "SENT", "CONNECTED"], [60, 25, 15])[s].tolist()),
        }, scenario=scen[s])

    # ---- household: self row + caregiver for 10% (20% of consents expired)
    if ctx.wants("family_relationship"):
        cg = rr.bernoulli("caregiver", 0.10)
        ci = idx[cg]
        rel = rr.choice("cg_rel", ["ADULT_CHILD", "SPOUSE", "PARENT", "POA"], [55, 25, 15, 5])[cg]
        scope_all = rr.bernoulli("cg_all", 0.3)[cg]
        scopes = [["ALL"] if a else sorted({x, y}) for a, x, y in zip(
            scope_all, rr.choice("cg_s1", ["BENEFITS", "CLAIMS", "BILLING", "PA_STATUS", "APPOINTMENTS", "PCP_CHANGE"])[cg],
            rr.choice("cg_s2", ["BENEFITS", "CLAIMS", "PA_STATUS", "APPOINTMENTS"])[cg])]
        c_eff = ctx.as_of - rr.integers("cg_eff", 60, 900)[cg].astype("timedelta64[D]")
        expired = rr.bernoulli("cg_expired", 0.2)[cg]
        c_exp = np.where(expired, ctx.as_of - rr.integers("cg_exp_past", 1, 50)[cg].astype("timedelta64[D]"),
                         ctx.as_of + rr.integers("cg_exp_future", 30, 700)[cg].astype("timedelta64[D]"))
        n_c = int(cg.sum())
        ctx.write("family_relationship", {
            "relationship_id": pa.array(ctx.ids("family_relationship", np.concatenate([idx * 4, ci * 4 + 1]))),
            "member_id": pa.array(np.concatenate([member_id, member_id[cg]]).tolist()),
            "related_member_id": pa.array([None] * (n + n_c), pa.string()),
            "subscriber_id": pa.array(np.concatenate([np.array(member_no, dtype=object), np.array(member_no, dtype=object)[cg]]).tolist()),
            "relationship_code": pa.array(["18"] * n + ["01" if r == "SPOUSE" else "OTHER" for r in rel]),
            "caregiver_flag": pa.array([False] * n + [True] * n_c),
            "caregiver_name": pa.array([None] * n + [f"{f} {l}" for f, l in zip(rr.choice("cg_first", catalog.FIRST_F + catalog.FIRST_M)[cg], last[cg])], pa.string()),
            "caregiver_relationship": pa.array([None] * n + rel.tolist(), pa.string()),
            "authorization_scope": pa.array([[]] * n + scopes, pa.list_(pa.string())),
            "consent_effective_dt": dates(np.concatenate([np.full(n, np.datetime64("NaT", "D"), "datetime64[D]"), c_eff])),
            "consent_expiry_dt": dates(np.concatenate([np.full(n, np.datetime64("NaT", "D"), "datetime64[D]"), c_exp])),
            "verified": pa.array(np.concatenate([np.ones(n, bool), rr.bernoulli("cg_verified", 0.9)[cg]])),
        }, scenario=np.concatenate([scen, scen[cg]]))


def run(ctx: GenContext) -> list[str]:
    plans_by_lob = load_plans(ctx)
    pcps = load_pcps(ctx) if ctx.wants("pcp_assignment") else {}
    for lo, hi in ctx.shard_ranges(ctx.bp.data_spec.volumes.members):
        gen_shard(ctx, lo, hi, plans_by_lob, pcps)
    return [e for e in ["member_demographics", "plan_enrollment", "pcp_assignment", "risk_score", "care_gap",
                        "sdoh_indicator", "family_relationship"] if ctx.wants(e)]
