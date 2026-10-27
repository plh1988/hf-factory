"""Rule catalog for stage s7, as portable SQL (PostgreSQL/AlloyDB and DuckDB common subset).

Each rule query returns one number: the count of violating rows. Rows tagged with a scenario or
dirty-data id that breaks a rule on purpose are excluded inside the query, by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.data_engine import catalog, scenarios

def _sql_list(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


PA_LIST = _sql_list(catalog.PA_CODES)
CPT_LIST = _sql_list(c[0] for c in catalog.CPT)
NDC_LIST = _sql_list(d[0] for d in catalog.NDC)
TAXONOMY_LIST = _sql_list(code for opts in catalog.TAXONOMY.values() for code, _ in opts)


@dataclass
class Rule:
    id: str
    description: str
    sql: str
    needs: list[str] = field(default_factory=list)


# (child, column, parent, parent key) - R-01 referential integrity
FOREIGN_KEYS = [
    ("family_relationship", "member_id", "member_demographics", "member_id"),
    ("plan_enrollment", "member_id", "member_demographics", "member_id"),
    ("plan_enrollment", "plan_id", "plan", "plan_id"),
    ("pcp_assignment", "member_id", "member_demographics", "member_id"),
    ("pcp_assignment", "pcp_npi", "provider", "npi"),
    ("risk_score", "member_id", "member_demographics", "member_id"),
    ("care_gap", "member_id", "member_demographics", "member_id"),
    ("sdoh_indicator", "member_id", "member_demographics", "member_id"),
    ("member_accumulator", "member_id", "member_demographics", "member_id"),
    ("network_participation", "npi", "provider", "npi"),
    ("network_participation", "plan_id", "plan", "plan_id"),
    ("practice_location", "npi", "provider", "npi"),
    ("claim_header", "member_id", "member_demographics", "member_id"),
    ("claim_header", "plan_id", "plan", "plan_id"),
    ("claim_header", "billing_npi", "provider", "npi"),
    ("claim_header", "rendering_npi", "provider", "npi"),
    ("claim_header", "facility_npi", "provider", "npi"),
    ("claim_header", "auth_id", "auth_request", "auth_id"),
    ("claim_line", "claim_id", "claim_header", "claim_id"),
    ("pharmacy_claim_detail", "claim_id", "claim_header", "claim_id"),
    ("pharmacy_claim_detail", "pharmacy_npi", "provider", "npi"),
    ("claim_denial", "claim_id", "claim_header", "claim_id"),
    ("claim_denial", "line_id", "claim_line", "line_id"),
    ("claim_pend", "claim_id", "claim_header", "claim_id"),
    ("claim_adjustment", "line_id", "claim_line", "line_id"),
    ("claim_payment", "claim_id", "claim_header", "claim_id"),
    ("appeal_record", "claim_id", "claim_header", "claim_id"),
    ("auth_request", "member_id", "member_demographics", "member_id"),
    ("auth_request", "plan_id", "plan", "plan_id"),
    ("auth_request", "requesting_npi", "provider", "npi"),
    ("auth_request", "servicing_npi", "provider", "npi"),
    ("auth_request", "facility_npi", "provider", "npi"),
    ("auth_line", "auth_id", "auth_request", "auth_id"),
    ("auth_determination", "auth_id", "auth_request", "auth_id"),
    ("auth_determination", "criteria_asset_id", "knowledge_asset", "asset_id"),
    ("clinical_review_queue", "auth_id", "auth_request", "auth_id"),
    ("documentation_requirement", "auth_id", "auth_request", "auth_id"),
    ("care_case", "member_id", "member_demographics", "member_id"),
    ("transition_of_care", "member_id", "member_demographics", "member_id"),
    ("transition_of_care", "facility_npi", "provider", "npi"),
    ("transition_of_care", "case_id", "care_case", "case_id"),
    ("asset_chunk", "asset_id", "knowledge_asset", "asset_id"),
    ("availability_schedule", "location_id", "practice_location", "location_id"),
    ("availability_schedule", "npi", "provider", "npi"),
    ("provider_rating", "npi", "provider", "npi"),
    ("access_standard", "plan_id", "plan", "plan_id"),
    ("reprocessing_history", "claim_id", "claim_header", "claim_id"),
    ("reprocessing_history", "new_claim_id", "claim_header", "claim_id"),
    ("claim_header", "original_claim_id", "claim_header", "claim_id"),
    ("care_plan", "case_id", "care_case", "case_id"),
    ("care_plan", "member_id", "member_demographics", "member_id"),
    ("disease_program_enrollment", "member_id", "member_demographics", "member_id"),
    ("outreach_campaign", "target_cohort_id", "risk_cohort", "cohort_id"),
    ("outreach_history", "campaign_id", "outreach_campaign", "campaign_id"),
    ("outreach_history", "member_id", "member_demographics", "member_id"),
    ("faq_entry", "asset_id", "knowledge_asset", "asset_id"),
    ("agent_sop", "asset_id", "knowledge_asset", "asset_id"),
    ("asset_link", "asset_id", "knowledge_asset", "asset_id"),
]


def fk_rules() -> list[Rule]:
    return [
        Rule(f"R-01:{child}.{col}", f"{child}.{col} references {parent}.{pk}",
             f"SELECT count(*) FROM {child} c WHERE c.{col} IS NOT NULL "
             f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{pk} = c.{col})",
             [child, parent])
        for child, col, parent, pk in FOREIGN_KEYS
    ]


def domain_rules(as_of: str, tenant: str) -> list[Rule]:
    d = f"DATE '{as_of}'"
    return [
        Rule("R-02", "Claim service date is covered by an enrollment on the claim's plan", """
            SELECT count(*) FROM claim_header h WHERE NOT EXISTS (
              SELECT 1 FROM plan_enrollment e WHERE e.member_id = h.member_id AND e.plan_id = h.plan_id
                AND e.effective_dt <= h.service_from_dt AND (e.term_dt IS NULL OR e.term_dt >= h.service_from_dt))""",
             ["claim_header", "plan_enrollment"]),
        Rule("R-03", "Non-emergency claims with a PA-required line carry an approved auth (MCI-02 missing-auth denials exempt)", f"""
            SELECT count(*) FROM claim_header h WHERE NOT h.is_emergency
              AND EXISTS (SELECT 1 FROM claim_line l WHERE l.claim_id = h.claim_id AND l.cpt_hcpcs IN ({PA_LIST}))
              AND NOT EXISTS (SELECT 1 FROM auth_request a WHERE a.auth_id = h.auth_id AND a.status IN ('APPROVED', 'PARTIAL'))
              AND {scenarios.sql_excluding(lambda s: s.pa_code_without_auth, "h.scenario_id")}""",
             ["claim_header", "claim_line", "auth_request"]),
        Rule("R-04a", "Participation windows are valid: effective <= term; TERMED rows have a term date", """
            SELECT count(*) FROM network_participation WHERE (term_dt IS NOT NULL AND term_dt < effective_dt)
              OR (status = 'TERMED' AND term_dt IS NULL)""",
             ["network_participation"]),
        Rule("R-04b", "In-network claims bill from a provider participating on the plan at DOS", """
            SELECT count(*) FROM claim_header h WHERE h.network_status = 'IN' AND NOT EXISTS (
              SELECT 1 FROM network_participation n WHERE n.npi = h.billing_npi AND n.plan_id = h.plan_id
                AND n.status = 'PARTICIPATING' AND n.effective_dt <= h.service_from_dt
                AND (n.term_dt IS NULL OR n.term_dt >= h.service_from_dt))""",
             ["claim_header", "network_participation"]),
        Rule("R-05a", "At most one ACTIVE PCP assignment per member", """
            SELECT count(*) FROM (SELECT member_id FROM pcp_assignment WHERE status = 'ACTIVE'
                                  GROUP BY member_id HAVING count(*) > 1) x""",
             ["pcp_assignment"]),
        Rule("R-05b", "Active PCP is a participating PCP with an open or limited panel on the member's plan", """
            SELECT count(*) FROM pcp_assignment a WHERE a.status = 'ACTIVE' AND NOT EXISTS (
              SELECT 1 FROM network_participation n JOIN provider p ON p.npi = n.npi
              WHERE n.npi = a.pcp_npi AND n.plan_id = a.plan_id AND p.provider_type = 'PCP'
                AND n.status = 'PARTICIPATING' AND n.panel_status <> 'CLOSED')""",
             ["pcp_assignment", "network_participation", "provider"]),
        Rule("R-06", "Caregiver rows have a consent window", """
            SELECT count(*) FROM family_relationship WHERE caregiver_flag
              AND (consent_effective_dt IS NULL OR consent_expiry_dt IS NULL OR consent_effective_dt > consent_expiry_dt)""",
             ["family_relationship"]),
        Rule("R-07", "Replacement/void claims reference the original; reprocessed claims have history", """
            SELECT count(*) FROM claim_header h WHERE (h.claim_frequency_code IN ('7', '8') AND h.original_claim_id IS NULL)
              OR (h.status = 'REPROCESSED' AND NOT EXISTS (SELECT 1 FROM reprocessing_history r WHERE r.claim_id = h.claim_id))""",
             ["claim_header", "reprocessing_history"]),
        Rule("R-08", "Pharmacy claims have exactly one pharmacy detail; others have none", """
            SELECT count(*) FROM claim_header h WHERE
              (h.claim_type = 'PHARMACY') <> ((SELECT count(*) FROM pharmacy_claim_detail p WHERE p.claim_id = h.claim_id) = 1)""",
             ["claim_header", "pharmacy_claim_detail"]),
        Rule("R-09", "Care gaps close only through a matching paid claim", """
            SELECT count(*) FROM care_gap g WHERE g.status = 'CLOSED' AND NOT EXISTS (
              SELECT 1 FROM claim_header h WHERE h.claim_id = g.closed_by_claim_id AND h.status = 'PAID')""",
             ["care_gap", "claim_header"]),
        Rule("R-10", "Inpatient facility discharges have a transition-of-care record", """
            SELECT count(*) FROM claim_header h WHERE h.bill_type = '0111' AND NOT EXISTS (
              SELECT 1 FROM transition_of_care t WHERE t.member_id = h.member_id
                AND t.admit_dt = h.service_from_dt AND t.discharge_dt = h.service_to_dt)""",
             ["claim_header", "transition_of_care"]),
        Rule("R-11a", "Every claim denial code links to an explanatory knowledge asset", """
            SELECT count(*) FROM claim_denial d WHERE NOT EXISTS (
              SELECT 1 FROM asset_link l WHERE l.linked_entity = 'DENIAL_CODE' AND l.linked_key = d.carc)""",
             ["claim_denial", "asset_link"]),
        Rule("R-11b", "Every auth determination cites an ACTIVE PA policy asset", """
            SELECT count(*) FROM auth_determination d WHERE NOT EXISTS (
              SELECT 1 FROM knowledge_asset k WHERE k.asset_id = d.criteria_asset_id
                AND k.asset_type = 'PA_POLICY' AND k.status = 'ACTIVE')""",
             ["auth_determination", "knowledge_asset"]),
        Rule("T-01a", "Enrollment spans do not overlap within a plan", """
            SELECT count(*) FROM plan_enrollment a JOIN plan_enrollment b
              ON a.member_id = b.member_id AND a.plan_id = b.plan_id AND a.enrollment_id < b.enrollment_id
            WHERE a.effective_dt <= COALESCE(b.term_dt, DATE '9999-12-31')
              AND b.effective_dt <= COALESCE(a.term_dt, DATE '9999-12-31')""",
             ["plan_enrollment"]),
        Rule("T-01b", "PCP assignments start inside an enrollment span", """
            SELECT count(*) FROM pcp_assignment a WHERE NOT EXISTS (
              SELECT 1 FROM plan_enrollment e WHERE e.member_id = a.member_id AND e.plan_id = a.plan_id
                AND e.effective_dt <= a.effective_dt AND (e.term_dt IS NULL OR e.term_dt >= a.effective_dt))""",
             ["pcp_assignment", "plan_enrollment"]),
        Rule("T-02", "Claim dates are ordered: service from <= to <= received <= adjudicated <= paid", """
            SELECT count(*) FROM claim_header WHERE service_from_dt > service_to_dt OR service_to_dt > received_dt
              OR received_dt > adjudicated_dt OR adjudicated_dt > paid_dt""",
             ["claim_header"]),
        Rule("T-02b", "No claim dates after the as-of date", f"""
            SELECT count(*) FROM claim_header WHERE received_dt > {d} OR adjudicated_dt > {d} OR paid_dt > {d}""",
             ["claim_header"]),
        Rule("T-03", "Auth decision due: urgent 72 hours, standard 7 days", """
            SELECT count(*) FROM auth_request WHERE decision_due_dt <> received_dt +
              CASE WHEN urgency = 'URGENT' THEN INTERVAL '72 hours' ELSE INTERVAL '168 hours' END""",
             ["auth_request"]),
        Rule("T-03b", "Auth determinations are made by the due time", """
            SELECT count(*) FROM auth_determination d JOIN auth_request a ON a.auth_id = d.auth_id
            WHERE d.determination_dt > a.decision_due_dt""",
             ["auth_determination", "auth_request"]),
        Rule("T-04", "An auth on a claim covers the claim's dates of service", """
            SELECT count(*) FROM claim_header h JOIN auth_determination d ON d.auth_id = h.auth_id
            WHERE d.outcome <> 'DENIED' AND (d.valid_from > h.service_from_dt OR d.valid_to < h.service_to_dt)""",
             ["claim_header", "auth_determination"]),
        Rule("T-05", "Appeals are filed by the deadline (PAP-04 late filings exempt by tag)", f"""
            SELECT count(*) FROM appeal_record WHERE filed_dt > deadline_dt
              AND {scenarios.sql_excluding(lambda s: s.appeal and s.appeal.late_filing)}""",
             ["appeal_record"]),
        Rule("T-05b", "Appeal decisions come after filing", """
            SELECT count(*) FROM appeal_record WHERE decision_dt IS NOT NULL AND decision_dt < filed_dt""",
             ["appeal_record"]),
        Rule("T-06", "TOC call due 48h after discharge; follow-up due in 7 days", """
            SELECT count(*) FROM transition_of_care WHERE
              toc_call_due_dt <> CAST(discharge_dt AS TIMESTAMP) + INTERVAL '48 hours'
              OR follow_up_due_dt <> discharge_dt + 7""",
             ["transition_of_care"]),
        Rule("T-07", "Practice locations verified within 90 days (stale directory rows exempt by tag)", f"""
            SELECT count(*) FROM practice_location WHERE last_verified_dt < {d} - 90
              AND scenario_id <> '{catalog.DQ_STALE_DIRECTORY}'""",
             ["practice_location"]),
        Rule("T-07b", "Next available slots are in the future and not on a blackout date (stale schedules exempt by tag)", f"""
            SELECT count(*) FROM availability_schedule WHERE scenario_id <> '{catalog.DQ_STALE_SCHEDULE}'
              AND (CAST(next_available_dt AS DATE) <= {d}
                   OR list_contains(blackout_dates, CAST(CAST(next_available_dt AS DATE) AS VARCHAR)))""",
             ["availability_schedule"]),
        Rule("T-08", "ACTIVE assets are effective on the as-of date", f"""
            SELECT count(*) FROM knowledge_asset WHERE status = 'ACTIVE'
              AND (effective_dt > {d} OR expiry_dt < {d})""",
             ["knowledge_asset"]),
        Rule("F-01a", "Lines: billed >= allowed >= paid >= 0", """
            SELECT count(*) FROM claim_line WHERE NOT (billed >= allowed AND allowed >= paid AND paid >= 0)""",
             ["claim_line"]),
        Rule("F-01b", "Header totals equal the sum of lines", """
            SELECT count(*) FROM claim_header h JOIN (
              SELECT claim_id, sum(billed) b, sum(allowed) a, sum(paid) p FROM claim_line GROUP BY claim_id) l
              ON l.claim_id = h.claim_id
            WHERE h.total_billed <> l.b OR h.total_allowed <> l.a OR h.total_paid <> l.p""",
             ["claim_header", "claim_line"]),
        Rule("F-04a", "Provider taxonomy codes come from the pinned taxonomy table", f"""
            SELECT count(*) FROM provider WHERE taxonomy_code NOT IN ({TAXONOMY_LIST})""",
             ["provider"]),
        Rule("F-04b", "Procedure codes come from the pinned CPT/HCPCS table", f"""
            SELECT count(*) FROM claim_line WHERE cpt_hcpcs IS NOT NULL AND cpt_hcpcs NOT IN ({CPT_LIST})""",
             ["claim_line"]),
        Rule("F-04c", "Drug codes come from the pinned NDC table", f"""
            SELECT count(*) FROM pharmacy_claim_detail WHERE ndc NOT IN ({NDC_LIST})""",
             ["pharmacy_claim_detail"]),
        Rule("F-02a", "Paid lines: paid = allowed - (deductible + copay + coinsurance)", """
            SELECT count(*) FROM claim_line WHERE line_status = 'PAID'
              AND paid <> allowed - deductible - copay - coinsurance""",
             ["claim_line"]),
        Rule("F-02b", "Member liability on paid claims stays within the plan OOP maximum", """
            SELECT count(*) FROM (
              SELECT h.member_id, max(p.oop_max) oop, sum(h.member_resp) resp
              FROM claim_header h JOIN plan p ON p.plan_id = h.plan_id
              WHERE h.status = 'PAID' GROUP BY h.member_id) x WHERE x.oop > 0 AND x.resp > x.oop""",
             ["claim_header", "plan"]),
        Rule("F-03", "Accumulators equal patient-responsibility (PR) amounts on paid claims", """
            SELECT count(*) FROM member_accumulator a JOIN (
              SELECT h.member_id, CASE WHEN h.network_status = 'IN' THEN 'IN' ELSE 'OON' END net,
                     sum(l.deductible) ded, sum(l.deductible + l.copay + l.coinsurance) oop
              FROM claim_header h JOIN claim_line l ON l.claim_id = h.claim_id
              WHERE h.status = 'PAID' GROUP BY 1, 2) s ON s.member_id = a.member_id AND s.net = a.network
            WHERE a.deductible_met <> s.ded OR a.oop_met <> s.oop""",
             ["member_accumulator", "claim_header", "claim_line"]),
        Rule("F-05a", "Member responsibility equals the sum of PR adjustments", """
            SELECT count(*) FROM claim_header h WHERE h.member_resp <> COALESCE((
              SELECT sum(a.amount) FROM claim_adjustment a JOIN claim_line l ON l.line_id = a.line_id
              WHERE l.claim_id = h.claim_id AND a.group_code = 'PR'), 0)""",
             ["claim_header", "claim_adjustment", "claim_line"]),
        Rule("F-05b", "Denied lines pay zero and carry a CO adjustment", """
            SELECT count(*) FROM claim_line l WHERE l.line_status = 'DENIED' AND (l.paid <> 0 OR NOT EXISTS (
              SELECT 1 FROM claim_adjustment a WHERE a.line_id = l.line_id AND a.group_code = 'CO'))""",
             ["claim_line", "claim_adjustment"]),
        Rule("F-06", "Reprocessing: paid_before/paid_after equal the original and replacement claims' paid totals", """
            SELECT count(*) FROM reprocessing_history r
              JOIN claim_header o ON o.claim_id = r.claim_id JOIN claim_header n ON n.claim_id = r.new_claim_id
            WHERE r.paid_before <> o.total_paid OR r.paid_after <> n.total_paid OR r.paid_after < 0""",
             ["reprocessing_history", "claim_header"]),
        Rule("F-07", "A payment equals the claim's paid total", """
            SELECT count(*) FROM claim_payment p JOIN claim_header h ON h.claim_id = p.claim_id WHERE p.paid_amount <> h.total_paid""",
             ["claim_payment", "claim_header"]),
        Rule("R-07b", "Replacement claims (frequency 7) are paid versions of an ADJUSTED or REPROCESSED original", """
            SELECT count(*) FROM claim_header n JOIN claim_header o ON o.claim_id = n.original_claim_id
            WHERE n.claim_frequency_code = '7' AND o.status NOT IN ('ADJUSTED', 'REPROCESSED')""",
             ["claim_header"]),
        Rule("C-03", "Auth denials state a rationale and send appeal rights", """
            SELECT count(*) FROM auth_determination WHERE outcome = 'DENIED'
              AND (denial_rationale IS NULL OR appeal_rights_sent_dt IS NULL)""",
             ["auth_determination"]),
        Rule("C-04", "Emergency claims are never denied for missing authorization", """
            SELECT count(*) FROM claim_header h JOIN claim_denial d ON d.claim_id = h.claim_id
            WHERE h.is_emergency AND d.denial_category = 'AUTH'""",
             ["claim_header", "claim_denial"]),
        Rule("C-07", "Outreach without consent is suppressed (TCPA)", """
            SELECT count(*) FROM outreach_history WHERE consent_basis = 'NONE' AND outcome <> 'SUPPRESSED'""",
             ["outreach_history"]),
        Rule("A-04", "AI outreach discloses that it is AI", """
            SELECT count(*) FROM outreach_history WHERE ai_disclosed IS NOT TRUE""",
             ["outreach_history"]),
        Rule("R-12", "Every plan has access standards for PCP in every geography", """
            SELECT count(*) FROM plan p WHERE (SELECT count(DISTINCT geo_type) FROM access_standard a
              WHERE a.plan_id = p.plan_id AND a.provider_type_or_specialty = 'PCP') < 3""",
             ["plan", "access_standard"]),
        Rule("A-01", "Medical-necessity denials are made by a medical director or same-specialty peer", """
            SELECT count(*) FROM auth_determination WHERE outcome = 'DENIED' AND denial_category = 'MED_NECESSITY'
              AND decided_by_role NOT IN ('MEDICAL_DIRECTOR', 'SAME_SPECIALTY_PEER')""",
             ["auth_determination"]),
        Rule("P-02a", "Member phones use the reserved 555-01xx range", """
            SELECT count(*) FROM member_demographics WHERE phone NOT LIKE '%555-01__'""",
             ["member_demographics"]),
        Rule("P-02c", "Practice location phones use the reserved 555-01xx range", """
            SELECT count(*) FROM practice_location WHERE phone NOT LIKE '%555-01__'""",
             ["practice_location"]),
        Rule("P-02d", "Provider TINs use the synthetic 9xxxxxxxx range", """
            SELECT count(*) FROM provider WHERE tin NOT LIKE '9________'""",
             ["provider"]),
        Rule("P-02b", "Member emails use the reserved example.test domain", """
            SELECT count(*) FROM member_demographics WHERE email NOT LIKE '%@example.test'""",
             ["member_demographics"]),
    ]


def pack_rules(as_of: str = "1970-01-01") -> list[Rule]:
    """Rules of the active entity packs: a foreign-key rule per column with `references`, then the pack's own rules.
    Imported late: packs import the catalog, and the rule catalog must not depend on pack loading order."""
    from factory.data_engine import packs
    from factory.data_engine.packs.model import sql_tables

    out = []
    for p in packs.for_anchor("member"):
        for c in p.entity.columns:
            if c.references is not None:
                child, parent, pk = p.entity.name, c.references.entity, c.references.column
                out.append(Rule(f"R-01:{child}.{c.name}", f"{child}.{c.name} references {parent}.{pk}",
                                f"SELECT count(*) FROM {child} c WHERE c.{c.name} IS NOT NULL "
                                f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{pk} = c.{c.name})", [child, parent]))
        out += [Rule(r.id, r.description, " ".join(r.sql.split()).replace("AS_OF_DATE", f"DATE '{as_of}'"),
                     r.needs or sorted(sql_tables(r.sql))) for r in p.rules]
    return out


def p01_rule(entity: str, tenant: str) -> Rule:
    return Rule(f"P-01:{entity}", f"{entity}: every row is synthetic, tenant-stamped and scenario-tagged",
                f"SELECT count(*) FROM {entity} WHERE is_synthetic IS NOT TRUE OR tenant_id <> '{tenant}' OR scenario_id IS NULL",
                [entity])
