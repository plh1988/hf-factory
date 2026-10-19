"""Scenario recipes: the facts each scenario forces on the rows it tags, declared in one table.

Generators never branch on scenario ids. They ask which rows carry a scenario with a given property:

    tagged(scen, lambda s: s.missing_rendering_npi)        -> bool mask
    for s in having("auth_service_type"): ...               -> recipes that set a field
    override(arr, scen, "auth_urgent")                      -> arr with each recipe's value on its rows

Recipe kinds (spec scenario types): happy, edge, compliance, data. Claim and auth recipes that are not happy
are *forced*: the generator makes the row satisfy the recipe. Happy-path tags are not forced; they stay only on
rows that naturally match (so the payload's distributions, e.g. the denial rate, are kept). Scenarios marked
API-level only shape API behaviour; their rows are baseline data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np

from factory.codes import AppealOutcome, AppealStatus, AuthStatus, ClaimStatus, DenialCategory, GeoType, ServiceType

BASE = "BASE"  # tag of rows that belong to no scenario


@dataclass(frozen=True)
class AppealRecipe:
    """An appeal the scenario's claim must carry."""

    status: AppealStatus
    outcome: AppealOutcome | None = None
    late_filing: bool = False  # filed after the deadline (rule T-05 exempts these rows)
    awaiting_reprocessing: bool = False  # overturned but the replacement claim is not issued yet


@dataclass(frozen=True)
class Scenario:
    id: str
    anchor: str  # member | claim | auth
    kind: str  # happy | edge | compliance | data
    title: str
    natural: bool = False  # holds on many baseline rows anyway: tagged like a happy path, never forced
    # ---- claims (s5)
    claim_status: ClaimStatus | None = None
    denial_category: DenialCategory | None = None
    pend_reason: str | None = None
    missing_rendering_npi: bool = False  # CO-16 missing information
    pa_code_without_auth: bool = False  # PA-required code billed without an auth (rule R-03 exempts these rows)
    fresh_appeal: bool = False  # an appealable denial with no appeal yet (the agent files it)
    appeal: AppealRecipe | None = None
    reprocess: str | None = None  # reprocessing trigger for a paid claim (PROVIDER_CORRECTION)
    payment: str | None = None  # RECOUPMENT | WRONG_PAYEE
    # ---- authorizations (s5)
    auth_status: AuthStatus | None = None
    doc_type: str | None = None  # document the pended auth waits for
    auth_service_type: ServiceType | None = None
    auth_urgent: bool | None = None
    auth_received_days_ago: int | None = None
    auth_cpt: str | None = None
    # ---- members (s4)
    enrollment: str | None = None  # BASELINE (drawn like untagged members) | RETRO_TERMED | PENDING
    geo_type: GeoType | None = None
    language: str | None = None
    transport_need: bool = False
    open_care_gap: bool = False
    pcp_switch: str | None = None  # change reason of a forced PCP switch
    auto_assigned_pcp: bool = False
    caregiver_scope: list[str] | None = None  # a caregiver with exactly this authorization scope

    @property
    def forced(self) -> bool:
        """Claim and auth recipes other than happy paths (and natural ones) are forced onto their rows."""
        return self.anchor in ("claim", "auth") and self.kind != "happy" and not self.natural

    @property
    def use_case(self) -> str:
        return self.id.split("-")[0]


def _s(id_: str, anchor: str, kind: str, title: str, **facts: Any) -> Scenario:
    return Scenario(id_, anchor, kind, title, **facts)


_RECIPES = [
    # ---- S1 provider-facing: claim status (PCS), auth status (PAS), eligibility (PEV)
    _s("PCS-01", "claim", "happy", "Status by claim number", claim_status=ClaimStatus.PAID),
    _s("PCS-02", "claim", "edge", "Pended for records", claim_status=ClaimStatus.PENDED, pend_reason="RECORDS_REQUEST"),
    _s("PCS-03", "claim", "edge", "Denied CO-16 missing information", claim_status=ClaimStatus.DENIED,
       denial_category=DenialCategory.MISSING_INFO, missing_rendering_npi=True),
    _s("PCS-04", "claim", "compliance", "Caller fails verification (API-level)", claim_status=ClaimStatus.PAID),
    _s("PAS-01", "auth", "happy", "Routine status check", auth_status=AuthStatus.IN_REVIEW, auth_urgent=False,
       auth_received_days_ago=5),
    _s("PAS-02", "auth", "edge", "Urgent SNF authorization at discharge", auth_status=AuthStatus.PENDED_INFO,
       doc_type="DISCHARGE_SUMMARY", auth_service_type=ServiceType.SNF, auth_urgent=True, auth_received_days_ago=1),
    _s("PAS-03", "auth", "edge", "Missing PT evaluation", auth_status=AuthStatus.PENDED_INFO, doc_type="PT_EVAL",
       auth_service_type=ServiceType.OUTPATIENT, auth_urgent=False, auth_received_days_ago=3, auth_cpt="97161"),
    _s("PAS-04", "auth", "compliance", "Denied; asks for reviewer details", auth_status=AuthStatus.DENIED),
    _s("PEV-01", "member", "happy", "Active member check", enrollment="BASELINE"),
    _s("PEV-02", "member", "edge", "Termed retroactively", enrollment="RETRO_TERMED"),
    _s("PEV-03", "member", "edge", "Enrollment pending", enrollment="PENDING"),
    _s("PEV-04", "member", "compliance", "Bulk lookup attempt (API-level)"),
    # ---- member-facing, member-anchored (s4)
    _s("MBN-01", "member", "happy", "Cost of a specialist visit"),
    _s("MBN-02", "member", "edge", "OTC and dental allowance"),
    _s("MBN-03", "member", "edge", "Transportation benefit with a transport need", transport_need=True),
    _s("MBN-04", "member", "compliance", "Caregiver outside authorized scope", caregiver_scope=["BENEFITS"]),
    _s("MPS-01", "member", "happy", "In-network specialist search"),
    _s("MPS-02", "member", "edge", "Spanish-speaking pediatrician in a rural area", geo_type=GeoType.RURAL, language="ES"),
    _s("MPS-03", "member", "edge", "Accessible specialist"),
    _s("MPS-04", "member", "data", "Directory vs participation mismatch (API-level)"),
    _s("MPC-01", "member", "happy", "Standard PCP change"),
    _s("MPC-02", "member", "edge", "PCP terminated and auto-assigned", pcp_switch="PROVIDER_TERMED", auto_assigned_pcp=True),
    _s("MPC-03", "member", "edge", "Moved counties", pcp_switch="MOVED"),
    _s("MPC-04", "member", "compliance", "Caregiver without PCP-change scope", caregiver_scope=["APPOINTMENTS"]),
    _s("MAS-01", "member", "happy", "Sick visit next available"),
    _s("MAS-02", "member", "edge", "Wellness visit with open care gaps and transport need", transport_need=True, open_care_gap=True),
    _s("MAS-03", "member", "compliance", "No availability within the wait standard (API-level)"),
    _s("MAS-04", "member", "data", "Stale availability schedule (provider data)"),
    # ---- claim-anchored (s5)
    _s("MCI-01", "claim", "happy", "EOB explanation", claim_status=ClaimStatus.PAID),
    _s("MCI-02", "claim", "edge", "Denied for missing authorization (CO-197)", claim_status=ClaimStatus.DENIED,
       denial_category=DenialCategory.AUTH, pa_code_without_auth=True),
    _s("MCI-03", "claim", "edge", "Pended for coordination of benefits", claim_status=ClaimStatus.PENDED, pend_reason="COB_INFO"),
    _s("MCI-04", "claim", "data", "Original and reprocessed claim both visible", claim_status=ClaimStatus.PAID,
       reprocess="PROVIDER_CORRECTION"),
    _s("PAP-01", "claim", "happy", "Start a claim appeal", claim_status=ClaimStatus.DENIED,
       denial_category=DenialCategory.MED_NECESSITY, fresh_appeal=True),
    _s("PAP-02", "claim", "edge", "Appeal status", claim_status=ClaimStatus.DENIED, denial_category=DenialCategory.MED_NECESSITY,
       appeal=AppealRecipe(AppealStatus.IN_REVIEW)),
    _s("PAP-03", "claim", "edge", "Appeal overturned, awaiting reprocessing", claim_status=ClaimStatus.DENIED,
       denial_category=DenialCategory.MED_NECESSITY,
       appeal=AppealRecipe(AppealStatus.DECIDED, AppealOutcome.OVERTURNED, awaiting_reprocessing=True)),
    _s("PAP-04", "claim", "compliance", "Deadline passed", claim_status=ClaimStatus.DENIED, denial_category=DenialCategory.MED_NECESSITY,
       appeal=AppealRecipe(AppealStatus.OPEN, late_filing=True)),
    _s("PPI-01", "claim", "happy", "Payment date and trace", claim_status=ClaimStatus.PAID),
    _s("PPI-02", "claim", "edge", "Paid less than billed", claim_status=ClaimStatus.PAID, natural=True),
    _s("PPI-03", "claim", "edge", "Recoupment offset", claim_status=ClaimStatus.PAID, payment="RECOUPMENT"),
    _s("PPI-04", "claim", "data", "Payment to wrong TIN", claim_status=ClaimStatus.PAID, payment="WRONG_PAYEE"),
]

SCENARIOS: dict[str, Scenario] = {s.id: s for s in _RECIPES}

# Happy-path claim scenarios are not forced: the tag stays only on claims that end in the recipe's status
# (PAP-01 also needs an appealable denial); otherwise the claim is BASE. This keeps the payload's denial rate.
HAPPY_CLAIM_SCENARIOS = {s.id for s in _RECIPES if s.anchor == "claim" and not s.forced}
FORCED_CLAIM_SCENARIOS = {s.id for s in _RECIPES if s.anchor == "claim" and s.forced}


# ---------------------------------------------------------------- queries over tag arrays
def ids(where: Callable[[Scenario], Any]) -> list[str]:
    """Scenario ids whose recipe satisfies `where`, in catalog order."""
    return [s.id for s in _RECIPES if where(s)]


def having(field: str) -> list[Scenario]:
    """Recipes that set `field`: its value differs from the field's default (None or False), in catalog order.

    Identity is compared, so a tri-state field such as auth_urgent (default None) counts False as set, and 0 counts
    as set for numeric fields.
    """
    default = Scenario.__dataclass_fields__[field].default
    return [s for s in _RECIPES if getattr(s, field) is not default]


def tagged(scen: np.ndarray, where: Callable[[Scenario], Any]) -> np.ndarray:
    """Bool mask: rows tagged with a scenario whose recipe satisfies `where`."""
    return np.isin(scen, sorted(ids(where)))


def override(arr: np.ndarray, scen: np.ndarray, field: str, value: Callable[[Scenario], Any] | None = None) -> np.ndarray:
    """Set each recipe's `field` value (or value(recipe)) on the rows it tags, in place; returns arr."""
    for s in having(field):
        arr[scen == s.id] = value(s) if value else getattr(s, field)
    return arr


def sql_excluding(where: Callable[[Scenario], Any], column: str = "scenario_id") -> str:
    """SQL condition for rule exemptions: the row's scenario is not one of the matching scenarios."""
    names: Iterable[str] = ids(where)
    return f"{column} NOT IN (" + ", ".join(f"'{x}'" for x in names) + ")"
