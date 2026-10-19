"""What s5 reads from the TableStore (members, plans, provider pools) and how it picks providers."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from factory.codes import FacilityType, ProviderRole, ProviderType
from factory.data_engine import catalog
from factory.data_engine.context import GenContext

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


@dataclass(eq=False)
class Inputs:
    """Everything claims and auths generation reads, loaded once per stage and shared by all tasks."""

    members: dict[str, np.ndarray]  # one row per member, with the latest enrollment (plan_id, dates, status)
    plans: dict[str, dict]  # plan_id -> cost-share terms
    pools: dict[str, list[str]]  # "<plan_id>|<role>" -> participating NPIs
    oon: dict[str, list[str]]  # role -> any active NPI (out-of-network fallback)
    ptype: dict[str, str]  # npi -> provider type
    tin: dict[str, str]  # npi -> TIN
    pcp: dict[str, str]  # member_id -> active PCP npi (requesting provider for auths)


def load_inputs(ctx: GenContext) -> Inputs:
    """Read everything claims generation needs from the TableStore once per stage."""
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
    return Inputs(
        members={c: np.array(members[c].to_pylist(), dtype=object) for c in members.column_names},
        plans={r["plan_id"]: r for r in plans.to_pylist()},
        pools=pools,
        oon=oon,
        ptype=dict(zip(allp["npi"].to_pylist(), allp["provider_type"].to_pylist())),
        tin=dict(zip(allp["npi"].to_pylist(), allp["tin"].to_pylist())),
        pcp=dict(zip(pcp["member_id"].to_pylist(), pcp["pcp_npi"].to_pylist())) if pcp is not None else {},
    )


def pick_provider(inp: Inputs, plan_ids: np.ndarray, role: str, in_network: np.ndarray, u: np.ndarray,
                  with_network: bool = False):
    """Pick from the plan's participating pool when in network; fall back to any active provider.
    with_network=True also returns whether the chosen provider really is in network (R-04)."""
    out = np.empty(len(plan_ids), dtype=object)
    actual = np.zeros(len(plan_ids), dtype=bool)
    for i, (plan, inn, x) in enumerate(zip(plan_ids, in_network, u)):
        pool = inp.pools.get(f"{plan}|{role}") if inn else None
        actual[i] = bool(pool)
        if not pool:
            pool = inp.oon.get(role) or inp.oon[ProviderRole.PCP]
        out[i] = pool[min(int(x * len(pool)), len(pool) - 1)]
    return (out, actual) if with_network else out


def to_date(a) -> np.ndarray:
    """Python dates (None allowed) -> numpy datetime64[D] with NaT for missing values."""
    return np.array([np.datetime64(x, "D") if x is not None else NAT for x in a], dtype="datetime64[D]")
