"""Deterministic list of knowledge assets for a run.

Asset keys are fixed by type and position, so any stage can compute an asset's ID
(e.g. s5 links auth determinations to PA policies before s6 writes the documents).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.data_engine import catalog

USE_CASE_ORDER = catalog.CORE_USE_CASES  # built-in use cases; slots of all use cases: catalog.USE_CASE_SLOTS
DENIAL_ORDER = list(catalog.DENIAL_CATEGORIES)


@dataclass
class AssetSpec:
    """One knowledge document to generate: its deterministic key, type, title, audience, the facts for the prompt, and links."""
    key: int
    asset_type: str
    title: str
    audience: str
    facts: list[str]
    plan_id: str | None = None
    lob: str | None = None
    use_case: str | None = None
    links: list[tuple[str, str]] = field(default_factory=list)  # (linked_entity, linked_key)


def asset_id(ctx, key: int) -> str:
    return ctx.ids("knowledge_asset", [key])[0]


def pa_policy_asset_id(ctx, cpt: str) -> str:
    return asset_id(ctx, 3000 + catalog.PA_CODES.index(cpt))


def asset_specs(ctx, plans: list[dict]) -> list[AssetSpec]:
    """The run's documents, keyed by type and position (plan docs 1000+, provider manual 2000, PA policies
    3000+, claims policies 4000+, FAQ libraries 5000+, agent SOPs 6000+), built only from catalog facts and plan terms.
    """
    specs: list[AssetSpec] = []
    for i, p in enumerate(plans):
        terms = [
            f"Plan {p['plan_name']} ({p['plan_code']}) is a {p['lob'].replace('_', ' ').title()} {p['network_type']} plan for plan year {p['plan_year']}",
            f"Annual deductible is {p['deductible']} USD and the out-of-pocket maximum is {p['oop_max']} USD",
            f"Primary care visits cost {p['copay_pcp']} USD and specialist visits cost {p['copay_specialist']} USD",
            f"Coinsurance of {p['coinsurance_pct']} percent applies after the deductible for services without a copay",
            f"Prior authorization is required for: {', '.join(catalog.PA_CODES)}",
            "Emergency services never require prior authorization",
        ]
        specs.append(AssetSpec(1000 + i * 2, "BENEFIT_DOC", f"{p['plan_name']} Evidence of Coverage", "MEMBER", terms,
                               plan_id=p["plan_id"], lob=p["lob"], links=[("PLAN", p["plan_id"])]))
        specs.append(AssetSpec(1000 + i * 2 + 1, "PLAN_SUMMARY", f"{p['plan_name']} Summary of Benefits", "MEMBER", terms[:4],
                               plan_id=p["plan_id"], lob=p["lob"], links=[("PLAN", p["plan_id"])]))

    manual = [
        "Providers must verify identity with NPI and TIN before receiving member information",
        "Claim status is available by claim number, or by member ID and date of service",
        "Pended claims list the information needed, who must send it, and the expected resolution date",
        "Corrected claims use frequency code 7 (replacement) and reference the original claim",
        "Authorization decisions are due within 72 hours for urgent requests and 7 calendar days for standard requests",
        "The decision clock pauses only while requested documentation is outstanding",
        "Eligibility responses include coverage dates, PCP, cost share, COB and PA indicators with a reference number",
    ]
    specs.append(AssetSpec(2000, "PROVIDER_MANUAL", "Provider Manual: Claims, Authorizations and Eligibility", "PROVIDER", manual))

    for i, code in enumerate(catalog.PA_CODES):
        desc = next(c[1] for c in catalog.CPT if c[0] == code)
        specs.append(AssetSpec(3000 + i, "PA_POLICY", f"Prior Authorization Policy: {desc} ({code})", "PROVIDER", [
            f"Procedure {code} ({desc}) requires prior authorization for non-emergency care",
            "Required documentation: clinical notes, prior conservative treatment, and relevant imaging or test results",
            "Adverse medical-necessity determinations are made by a medical director or same-specialty peer",
            "Providers may request a peer-to-peer review and have appeal rights after a denial",
        ], links=[("CPT", code)]))

    for i, cat in enumerate(DENIAL_ORDER):
        carc, text = catalog.DENIAL_CATEGORIES[cat]
        specs.append(AssetSpec(4000 + i, "CLAIMS_ADJ_POLICY", f"Claims Adjudication Policy: {cat.replace('_', ' ').title()} (CO-{carc})", "PROVIDER", [
            f"Denial code CO-{carc}: {text}",
            "Group code CO means the provider is liable and the member owes nothing for the denied amount",
            "Appealable denials can be appealed within 60 days for Medicare Advantage and Medicaid, 180 days for commercial plans",
        ], links=[("DENIAL_CODE", f"CO-{carc}")]))

    for uc in ctx.spec.use_cases:
        if uc not in catalog.USE_CASES:
            continue
        info = catalog.USE_CASES[uc]
        n = catalog.USE_CASE_SLOTS[uc]  # fixed per use case, so asset ids never shift
        specs.append(AssetSpec(5000 + n, "FAQ_LIBRARY", f"FAQ: {info['name']}", "PROVIDER" if info["group"] == "provider" else "MEMBER",
                               [f"{q[0]} {a}" for q, a in zip(info["faqs"], info["can"])], use_case=uc, links=[("USE_CASE", uc)]))
        specs.append(AssetSpec(6000 + n, "AGENT_SOP", f"Agent SOP: {info['name']}", "INTERNAL", [
            f"Verify the caller with: {', '.join(info['verification'])}",
            *[f"The agent can: {x}" for x in info["can"]],
            *[f"Escalate to a human for: {x}" for x in info["escalate"]],
            *[f"Never: {x}" for x in info["never"]],
        ], use_case=uc, links=[("USE_CASE", uc)]))
    return specs
