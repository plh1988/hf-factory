"""Stage s3: reference data every other entity points at (plans, providers, network, locations)."""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa

from factory.codes import GeoType, Lob, PanelStatus, ParticipationStatus, ProviderStatus, ProviderType
from factory.data_engine import catalog
from factory.data_engine.context import GenContext, dates, stamps
from factory.data_engine.rng import money

N_PLANS = 4

PLAN_TERMS = {  # lob -> (network types, deductibles, oop_max, copay_pcp, copay_spec, coinsurance)
    Lob.MEDICARE_ADV: (["HMO", "PPO"], [0, 250], [3900, 6700], [0, 10], [35, 45], 20),
    Lob.COMMERCIAL: (["PPO", "EPO"], [1500, 3200], [6000, 7500], [25, 30], [50, 60], 20),
    Lob.MEDICAID_MCO: (["HMO", "HMO"], [0, 0], [0, 0], [0, 0], [0, 0], 0),
}


# NPIs are 9 digits + a Luhn check digit computed over the prefix 80840 (the NPI standard). Bases start at
# 190000000 and step by 7 so they look realistic; they are not yet checked against the real NPPES registry.
def luhn_npi(base9: np.ndarray) -> list[str]:
    """10-digit NPI: 9-digit base + Luhn check digit computed with the 80840 prefix."""
    out = []
    for b in base9:
        digits = [int(c) for c in f"80840{int(b):09d}"]
        total = 0
        for i, d in enumerate(reversed(digits)):
            if i % 2 == 0:  # positions that get doubled once the check digit is appended
                d *= 2
                d = d - 9 if d > 9 else d
            total += d
        out.append(f"{int(b):09d}{(10 - total % 10) % 10}")
    return out


DEFAULT_LOB_CYCLE = [Lob.MEDICARE_ADV, Lob.COMMERCIAL, Lob.MEDICAID_MCO]


def n_plans(ctx: GenContext) -> int:
    """Number of plans to create: volumes.plans from the s3 payload, default 4."""
    return int(ctx.spec.volumes.get("plans", N_PLANS))


def plan_lobs(ctx: GenContext) -> list[str]:
    """LOB per plan. The spec's s3 payload carries no LOB mix, so by default plans cycle through
    Medicare Advantage, Commercial and Medicaid; an optional distributions.lob_mix in s3 narrows it."""
    n = n_plans(ctx)
    mix = ctx.spec.distributions.get("lob_mix")
    if not mix:
        return [DEFAULT_LOB_CYCLE[i % len(DEFAULT_LOB_CYCLE)] for i in range(n)]
    lobs, shares = list(mix), np.array(list(mix.values()), dtype=float)
    counts = np.maximum(1, np.round(shares / shares.sum() * n)).astype(int)
    seq = [lob for lob, c in zip(lobs, counts) for _ in range(c)]
    return (seq * n)[:n]


def gen_plans(ctx: GenContext) -> pa.Table:
    """Plans with cost-share terms by LOB (deductible, out-of-pocket max, copays, coinsurance, allowances).

    Money is generated in integer cents and stored as DECIMAL(14,2). Every plan lists the PA-required CPT codes,
    which s5 uses to decide when a claim needs an authorization (rule R-03).
    """
    N_PLANS = n_plans(ctx)  # noqa: N806 - local count shadows the default
    keys = np.arange(N_PLANS)
    lobs = plan_lobs(ctx)
    rows = {k: [] for k in ["network_type", "deductible", "oop_max", "copay_pcp", "copay_specialist", "coinsurance_pct"]}
    for i, lob in enumerate(lobs):
        nt, ded, oop, cp, cs, co = PLAN_TERMS[lob]
        rows["network_type"].append(nt[i % 2])
        rows["deductible"].append(ded[i % 2] * 100)
        rows["oop_max"].append(oop[i % 2] * 100)
        rows["copay_pcp"].append(cp[i % 2] * 100)
        rows["copay_specialist"].append(cs[i % 2] * 100)
        rows["coinsurance_pct"].append(co)
    allowances = [
        json.dumps({"OTC_quarterly": 100, "dental_annual": 1500, "vision_annual": 200, "transport_rides_annual": 24} if lob != Lob.COMMERCIAL else {})
        for lob in lobs
    ]
    return ctx.write("plan", {
        "plan_id": pa.array(ctx.ids("plan", keys)),
        "plan_code": pa.array([f"PLN-{i + 1:03d}" for i in keys]),
        "plan_name": pa.array([f"{lob.replace('_', ' ').title()} {nt} {i + 1}" for i, (lob, nt) in
                               enumerate(zip(lobs, rows["network_type"]))]),
        "lob": pa.array(lobs),
        "plan_year": pa.array(np.full(N_PLANS, int(str(ctx.as_of)[:4]))),
        "network_type": pa.array(rows["network_type"]),
        "deductible": money(np.array(rows["deductible"])),
        "oop_max": money(np.array(rows["oop_max"])),
        "copay_pcp": money(np.array(rows["copay_pcp"])),
        "copay_specialist": money(np.array(rows["copay_specialist"])),
        "coinsurance_pct": pa.array(rows["coinsurance_pct"], pa.int32()),
        "allowances": pa.array(allowances),
        "pa_required_codes": pa.array([catalog.PA_CODES] * N_PLANS, pa.list_(pa.string())),
        "formulary_id": pa.array([f"FORM-{2026}-{i % 2 + 1}" for i in keys]),
    })


def gen_providers(ctx: GenContext) -> pa.Table:
    """Providers: PCPs, specialists and facilities, with Luhn-valid synthetic NPIs and 9xxxxxxxx TINs (rule P-02).

    Rows 0-3 are always one facility of each kind (hospital, SNF, home health, pharmacy) so even tiny datasets can
    place facility and pharmacy claims. `home_place` anchors the provider's first practice location.
    """
    n = int(ctx.spec.volumes["providers"])
    keys = np.arange(n)
    rr = ctx.rr("provider", keys)
    ptype = rr.choice("type", [ProviderType.PCP, ProviderType.SPECIALIST, ProviderType.FACILITY], [35, 50, 15])
    ptype[:4] = ProviderType.FACILITY  # rows 0-3: one hospital, SNF, home health and pharmacy, even in tiny datasets
    tax_idx = rr.integers("taxonomy", 0, 4)
    taxonomy, specialty, facility_type = [], [], []
    for i, (t, k) in enumerate(zip(ptype, tax_idx)):
        opts = catalog.TAXONOMY[t]
        code, name = opts[i] if i < 4 else opts[k % len(opts)]
        taxonomy.append(code)
        specialty.append(name)
        facility_type.append(catalog.FACILITY_TYPE.get(code))
    is_org = ptype == ProviderType.FACILITY
    gender = rr.choice("gender", ["F", "M", "X"], [48, 50, 2])
    first = np.where(gender == "F", rr.choice("first_f", catalog.FIRST_F), rr.choice("first_m", catalog.FIRST_M))
    last = rr.choice("last", catalog.LAST)
    place = rr.integers("place", 0, len(catalog.PLACES))
    org_name = [f"{catalog.PLACES[p][0]} {s}" for p, s in zip(place, specialty)]
    langs = [["EN"] + (["ES"] if es else []) + (["ZH"] if zh else [])
             for es, zh in zip(rr.bernoulli("lang_es", 0.25), rr.bernoulli("lang_zh", 0.05))]
    status = rr.choice("status", [ProviderStatus.ACTIVE, ProviderStatus.TERMED, ProviderStatus.SUSPENDED], [95, 3, 2])
    return ctx.write("provider", {
        "npi": pa.array(luhn_npi(190000000 + keys * 7)),
        "provider_type": pa.array(list(ptype)),
        "entity_type": pa.array(np.where(is_org, "ORG", "INDIVIDUAL").tolist()),
        "first_name": pa.array([None if o else f for o, f in zip(is_org, first)], pa.string()),
        "last_name": pa.array([None if o else n for o, n in zip(is_org, last)], pa.string()),
        "org_name": pa.array([n if o else None for o, n in zip(is_org, org_name)], pa.string()),
        "taxonomy_code": pa.array(taxonomy),
        "specialty": pa.array(specialty),
        "facility_type": pa.array(facility_type, pa.string()),
        "languages": pa.array(langs, pa.list_(pa.string())),
        "gender": pa.array([None if o else g for o, g in zip(is_org, gender)], pa.string()),
        "tin": pa.array([f"9{int(x):08d}" for x in rr.integers("tin", 10_000_000, 99_999_999)]),
        "status": pa.array(list(status)),
        "home_place": pa.array(place.astype(np.int32)),
    })


def gen_participation(ctx: GenContext, providers: pa.Table, plans: pa.Table) -> pa.Table:
    """One network_participation row per (provider, plan): status, tier, panel and onboarding stage.

    Start dates are at least 400 days back so any claim in a 12-month history window falls inside participation
    (R-04). TERMED providers are TERMED on every plan; only PCPs can have a CLOSED panel.
    """
    n_prov, n_plan = providers.num_rows, plans.num_rows
    pi = np.repeat(np.arange(n_prov), n_plan)
    li = np.tile(np.arange(n_plan), n_prov)
    keys = pi * 8 + li
    rr = ctx.rr("network_participation", keys)
    pstatus = np.array(providers["status"].to_pylist(), dtype=object)[pi]
    ptype = np.array(providers["provider_type"].to_pylist(), dtype=object)[pi]
    status = rr.choice("status", [ParticipationStatus.PARTICIPATING, ParticipationStatus.NON_PARTICIPATING, ParticipationStatus.PENDING_ONBOARDING, ParticipationStatus.TERMED], [85, 6, 4, 5])
    status = np.where(pstatus == ProviderStatus.TERMED, ParticipationStatus.TERMED, status)
    eff = ctx.as_of - rr.integers("eff", 400, 5 * 365).astype("timedelta64[D]")
    term = np.where(status == ParticipationStatus.TERMED, ctx.as_of - rr.integers("term", 30, 300).astype("timedelta64[D]"),
                    np.datetime64("NaT", "D"))
    panel = np.where(ptype == ProviderType.PCP, rr.choice("panel", [PanelStatus.OPEN, PanelStatus.LIMITED, PanelStatus.CLOSED], [75, 15, 10]), PanelStatus.OPEN)
    stage = np.where(status == ParticipationStatus.PARTICIPATING, "LIVE",
                     np.where(status == ParticipationStatus.PENDING_ONBOARDING,
                              rr.choice("stage", ["APP_RECEIVED", "DOCS_PENDING", "CONTRACT_SENT"]), None))
    pending = [["W9", "CONTRACT"] if s == "DOCS_PENDING" else (["CONTRACT"] if s == "CONTRACT_SENT" else [])
               for s in stage]
    return ctx.write("network_participation", {
        "participation_id": pa.array(ctx.ids("network_participation", keys)),
        "npi": pa.array(np.array(providers["npi"].to_pylist(), dtype=object)[pi].tolist()),
        "plan_id": pa.array(np.array(plans["plan_id"].to_pylist(), dtype=object)[li].tolist()),
        "status": pa.array(status.tolist()),
        "tier": pa.array(rr.choice("tier", ["T1", "T2", "T3"], [60, 30, 10]).tolist()),
        "effective_dt": dates(eff),
        "term_dt": dates(term),
        "panel_status": pa.array(panel.tolist()),
        "onboarding_stage": pa.array(stage.tolist(), pa.string()),
        "pending_items": pa.array(pending, pa.list_(pa.string())),
    })


def gen_locations(ctx: GenContext, providers: pa.Table) -> pa.Table:
    """One practice location per provider, plus a second one for 40%.

    8% of locations are deliberately stale (last verified over 90 days ago) and tagged DQ-STALE-DIRECTORY, so the
    directory-accuracy scenarios have data and rule T-07 exempts exactly those rows.
    """
    n = providers.num_rows
    rrp = ctx.rr("practice_location:count", np.arange(n))
    n_loc = 1 + rrp.bernoulli("second", 0.4).astype(int)
    pi = np.repeat(np.arange(n), n_loc)
    j = np.concatenate([np.arange(k) for k in n_loc])
    keys = pi * 4 + j
    rr = ctx.rr("practice_location", keys)
    home = np.array(providers["home_place"].to_pylist())[pi]
    place_idx = np.where(j == 0, home, rr.integers("place", 0, len(catalog.PLACES)))
    places = [catalog.PLACES[p] for p in place_idx]
    stale = rr.bernoulli("stale", 0.08)  # Q-02 dirty-data profile: stale directory entries (C-02)
    verified = ctx.as_of - np.where(stale, rr.integers("v_old", 91, 240), rr.integers("v_new", 0, 90)).astype("timedelta64[D]")
    acc = [[a for a, f in (("WHEELCHAIR", w), ("ASL", s), ("TTY", t)) if f] for w, s, t in
           zip(rr.bernoulli("wc", 0.6), rr.bernoulli("asl", 0.05), rr.bernoulli("tty", 0.1))]
    return ctx.write("practice_location", {
        "location_id": pa.array(ctx.ids("practice_location", keys)),
        "npi": pa.array(np.array(providers["npi"].to_pylist(), dtype=object)[pi].tolist()),
        "address": pa.array([f"{100 + int(x)} {s} St" for x, s in
                             zip(rr.integers("street_no", 0, 9800), rr.choice("street", ["Main", "Oak", "Elm", "Pine", "Maple", "Cedar"]))]),
        "city": pa.array([p[0] for p in places]),
        "state": pa.array([p[1] for p in places]),
        "zip": pa.array([p[2] for p in places]),
        "lat": pa.array(np.round([p[3] for p in places] + rr.normal("lat", 0, 0.03), 5)),
        "lon": pa.array(np.round([p[4] for p in places] + rr.normal("lon", 0, 0.03), 5)),
        "geo_type": pa.array([p[5] for p in places]),
        "phone": pa.array([f"({200 + int(a)}) 555-01{int(b):02d}" for a, b in
                           zip(rr.integers("area", 0, 790), rr.integers("ph", 0, 100))]),
        "accepting_new_patients": pa.array(rr.bernoulli("accepting", 0.7)),
        "accessibility": pa.array(acc, pa.list_(pa.string())),
        "telehealth": pa.array(rr.bernoulli("tele", 0.4)),
        "last_verified_dt": dates(verified),
    }, scenario=np.where(stale, catalog.DQ_STALE_DIRECTORY, "BASE"))


# access standard rows: (provider type or specialty, distance by geo URBAN/SUBURBAN/RURAL, max wait days)
ACCESS_RULES = [
    ("PCP", (10, 20, 30), 15), ("PEDIATRICS", (10, 20, 30), 15), ("CARDIOLOGY", (15, 30, 60), 30),
    ("ORTHOPAEDICS", (15, 30, 60), 30), ("RADIOLOGY", (15, 30, 60), 21), ("PHYSICAL_THERAPY", (15, 30, 45), 21),
    ("HOSPITAL", (15, 30, 60), 0), ("SNF", (15, 30, 60), 0), ("PHARMACY", (5, 10, 20), 0), ("HOME_HEALTH", (30, 45, 60), 7),
]
SPECIALTY_KEY = {"Family Medicine": "PCP", "Internal Medicine": "PCP", "Pediatrics": "PEDIATRICS",
                 "Cardiovascular Disease": "CARDIOLOGY", "Orthopaedic Surgery": "ORTHOPAEDICS",
                 "Diagnostic Radiology": "RADIOLOGY", "Physical Therapist": "PHYSICAL_THERAPY",
                 "General Acute Care Hospital": "HOSPITAL", "Skilled Nursing Facility": "SNF",
                 "Home Health": "HOME_HEALTH", "Pharmacy": "PHARMACY"}
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT"]


def gen_access_standards(ctx: GenContext, plans: pa.Table) -> None:
    """Access standards per plan x specialty x geography: maximum distance (miles) and maximum wait (days), from ACCESS_RULES."""
    rows = [(pi, ri, gi) for pi in range(plans.num_rows) for ri in range(len(ACCESS_RULES)) for gi in range(3)]
    keys = np.array([pi * 1000 + ri * 10 + gi for pi, ri, gi in rows], dtype=np.int64)
    plan_ids = plans["plan_id"].to_pylist()
    ctx.write("access_standard", {
        "standard_id": pa.array(ctx.ids("access_standard", keys)),
        "plan_id": pa.array([plan_ids[pi] for pi, _, _ in rows]),
        "provider_type_or_specialty": pa.array([ACCESS_RULES[ri][0] for _, ri, _ in rows]),
        "geo_type": pa.array([(GeoType.URBAN, GeoType.SUBURBAN, GeoType.RURAL)[gi] for _, _, gi in rows]),
        "max_distance_miles": pa.array([ACCESS_RULES[ri][1][gi] for _, ri, gi in rows], pa.int32()),
        "max_wait_days": pa.array([ACCESS_RULES[ri][2] for _, ri, _ in rows], pa.int32()),
    })


def gen_ratings(ctx: GenContext, providers: pa.Table) -> None:
    """One quality/satisfaction rating per provider; ratings with fewer than 20 reviews are hidden by provider search."""
    n = providers.num_rows
    keys = np.arange(n)
    rr = ctx.rr("provider_rating", keys)
    flags = [[f for f, on in (("HIGH_VALUE", a), ("CENTER_OF_EXCELLENCE", b)) if on]
             for a, b in zip(rr.bernoulli("hv", 0.15), rr.bernoulli("coe", 0.05))]
    ctx.write("provider_rating", {
        "rating_id": pa.array(ctx.ids("provider_rating", keys)),
        "npi": pa.array(providers["npi"].to_pylist()),
        "as_of_dt": dates(np.full(n, ctx.as_of - 30 * np.timedelta64(1, "D"))),
        "quality_score": pa.array(np.round(np.clip(rr.normal("quality", 4.0, 0.5), 1.0, 5.0), 1)),
        "patient_satisfaction": pa.array(np.round(np.clip(rr.normal("satisfaction", 4.1, 0.5), 1.0, 5.0), 1)),
        "rating_count": pa.array(rr.integers("count", 5, 400).astype(np.int32)),
        "recognition_flags": pa.array(flags, pa.list_(pa.string())),
    })


def gen_schedules(ctx: GenContext, providers: pa.Table, locations: pa.Table) -> None:
    """Weekday hours per practice location of individual providers; 10% stale (next slot in a blackout, MAS-04)."""
    ptype = dict(zip(providers["npi"].to_pylist(), providers["provider_type"].to_pylist()))
    loc_rows = [(i, lid, npi) for i, (lid, npi) in enumerate(zip(locations["location_id"].to_pylist(), locations["npi"].to_pylist()))
                if ptype.get(npi) in (ProviderType.PCP, ProviderType.SPECIALIST)]
    lrr = ctx.rr("availability_schedule:location", np.array([i for i, _, _ in loc_rows], dtype=np.int64))
    n_days = 5 + lrr.bernoulli("saturday", 0.2).astype(int)
    li = np.repeat(np.arange(len(loc_rows)), n_days)
    di = np.concatenate([np.arange(k) for k in n_days]) if len(li) else np.array([], np.int64)
    keys = np.array([loc_rows[x][0] for x in li], dtype=np.int64) * 8 + di
    rr = ctx.rr("availability_schedule", keys)
    lead = rr.integers("lead", 1, 31)
    next_dt = ctx.as_of + lead.astype("timedelta64[D]")
    slot_hour = rr.integers("slot_hour", 8, 16)
    stale = lrr.bernoulli("stale", 0.10)[li]
    blackout = [[str(d)] if s else ([str(ctx.as_of + np.timedelta64(int(o), "D"))] if b else [])
                for d, s, b, o in zip(next_dt, stale, rr.bernoulli("vacation", 0.15), rr.integers("vac_offset", 35, 90))]
    types = [["NEW_PATIENT", "FOLLOW_UP", "SICK", "WELLNESS"] + (["TELEHEALTH"] if t else []) for t in rr.bernoulli("tele", 0.4)]
    ctx.write("availability_schedule", {
        "schedule_id": pa.array(ctx.ids("availability_schedule", keys)),
        "location_id": pa.array([loc_rows[x][1] for x in li]),
        "npi": pa.array([loc_rows[x][2] for x in li]),
        "day_of_week": pa.array([DAYS[d] for d in di]),
        "open_time": pa.array(["08:00"] * len(li)),
        "close_time": pa.array(np.where(di == 5, "12:00", "17:00").tolist()),
        "appointment_types": pa.array(types, pa.list_(pa.string())),
        "slot_minutes": pa.array(rr.choice("slot", [15, 20, 30], [40, 30, 30]).astype(np.int32)),
        "next_available_dt": stamps(next_dt.astype("datetime64[s]") + slot_hour.astype("timedelta64[h]")),
        "blackout_dates": pa.array(blackout, pa.list_(pa.string())),
    }, scenario=np.where(stale, catalog.DQ_STALE_SCHEDULE, "BASE"))


def run(ctx: GenContext) -> list[str]:
    plans = gen_plans(ctx)
    providers = gen_providers(ctx)
    gen_participation(ctx, providers, plans)
    locations = gen_locations(ctx, providers)
    if ctx.wants("access_standard"):
        gen_access_standards(ctx, plans)
    if ctx.wants("provider_rating"):
        gen_ratings(ctx, providers)
    if ctx.wants("availability_schedule"):
        gen_schedules(ctx, providers, locations)
    return [e for e in catalog.STAGE_ENTITIES["s3"] if ctx.wants(e)]
