"""Stage s3: reference data every other entity points at (plans, providers, network, locations)."""

from __future__ import annotations

import json

import numpy as np
import pyarrow as pa

from factory.data_engine import catalog
from factory.data_engine.context import GenContext, dates
from factory.data_engine.rng import money

N_PLANS = 4

PLAN_TERMS = {  # lob -> (network types, deductibles, oop_max, copay_pcp, copay_spec, coinsurance)
    "MEDICARE_ADV": (["HMO", "PPO"], [0, 250], [3900, 6700], [0, 10], [35, 45], 20),
    "COMMERCIAL": (["PPO", "EPO"], [1500, 3200], [6000, 7500], [25, 30], [50, 60], 20),
    "MEDICAID_MCO": (["HMO", "HMO"], [0, 0], [0, 0], [0, 0], [0, 0], 0),
}


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


def plan_lobs(ctx: GenContext) -> list[str]:
    mix = ctx.bp.data_spec.distributions.get("lob_mix", {"COMMERCIAL": 1})
    lobs, shares = list(mix), np.array(list(mix.values()), dtype=float)
    counts = np.maximum(1, np.round(shares / shares.sum() * N_PLANS)).astype(int)
    seq = [lob for lob, c in zip(lobs, counts) for _ in range(c)]
    return (seq * N_PLANS)[:N_PLANS]


def gen_plans(ctx: GenContext) -> pa.Table:
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
        json.dumps({"OTC_quarterly": 100, "dental_annual": 1500, "vision_annual": 200} if lob != "COMMERCIAL" else {})
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
    n = ctx.bp.data_spec.volumes.providers
    keys = np.arange(n)
    rr = ctx.rr("provider", keys)
    ptype = rr.choice("type", ["PCP", "SPECIALIST", "FACILITY"], [35, 50, 15])
    ptype[:4] = "FACILITY"  # rows 0-3: one hospital, SNF, home health and pharmacy, even in tiny datasets
    tax_idx = rr.integers("taxonomy", 0, 4)
    taxonomy, specialty, facility_type = [], [], []
    for i, (t, k) in enumerate(zip(ptype, tax_idx)):
        opts = catalog.TAXONOMY[t]
        code, name = opts[i] if i < 4 else opts[k % len(opts)]
        taxonomy.append(code)
        specialty.append(name)
        facility_type.append(catalog.FACILITY_TYPE.get(code))
    is_org = ptype == "FACILITY"
    gender = rr.choice("gender", ["F", "M", "X"], [48, 50, 2])
    first = np.where(gender == "F", rr.choice("first_f", catalog.FIRST_F), rr.choice("first_m", catalog.FIRST_M))
    last = rr.choice("last", catalog.LAST)
    place = rr.integers("place", 0, len(catalog.PLACES))
    org_name = [f"{catalog.PLACES[p][0]} {s}" for p, s in zip(place, specialty)]
    langs = [["EN"] + (["ES"] if es else []) + (["ZH"] if zh else [])
             for es, zh in zip(rr.bernoulli("lang_es", 0.25), rr.bernoulli("lang_zh", 0.05))]
    status = rr.choice("status", ["ACTIVE", "TERMED", "SUSPENDED"], [95, 3, 2])
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
    n_prov, n_plan = providers.num_rows, plans.num_rows
    pi = np.repeat(np.arange(n_prov), n_plan)
    li = np.tile(np.arange(n_plan), n_prov)
    keys = pi * 8 + li
    rr = ctx.rr("network_participation", keys)
    pstatus = np.array(providers["status"].to_pylist(), dtype=object)[pi]
    ptype = np.array(providers["provider_type"].to_pylist(), dtype=object)[pi]
    status = rr.choice("status", ["PARTICIPATING", "NON_PARTICIPATING", "PENDING_ONBOARDING", "TERMED"], [85, 6, 4, 5])
    status = np.where(pstatus == "TERMED", "TERMED", status)
    eff = ctx.as_of - rr.integers("eff", 365, 5 * 365).astype("timedelta64[D]")
    term = np.where(status == "TERMED", ctx.as_of - rr.integers("term", 30, 300).astype("timedelta64[D]"),
                    np.datetime64("NaT", "D"))
    panel = np.where(ptype == "PCP", rr.choice("panel", ["OPEN", "LIMITED", "CLOSED"], [75, 15, 10]), "OPEN")
    stage = np.where(status == "PARTICIPATING", "LIVE",
                     np.where(status == "PENDING_ONBOARDING",
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


def run(ctx: GenContext) -> list[str]:
    plans = gen_plans(ctx)
    providers = gen_providers(ctx)
    gen_participation(ctx, providers, plans)
    gen_locations(ctx, providers)
    return ["plan", "provider", "network_participation", "practice_location"]
