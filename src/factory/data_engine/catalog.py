"""Catalog 2.4.0: domains, entities, use cases, scenarios and pinned reference tables.

Reference tables here are small illustrative subsets. CPT codes are AMA-licensed; production
needs a licensed CPT table or synthetic codes. NDCs below are illustrative, not verified.
"""

from __future__ import annotations

from factory.codes import AuthStatus, DenialCategory, FacilityType, GeoType, Lob, ProviderType

CATALOG_VERSION = "2.4.0"

# Entities per domain: full (primary) list, in generation order.
ENTITIES: dict[str, list[str]] = {
    "M": ["member_demographics", "family_relationship", "plan", "plan_enrollment", "member_accumulator",
          "pcp_assignment", "risk_score", "care_gap", "sdoh_indicator"],
    "P": ["provider", "network_participation", "practice_location", "availability_schedule",
          "provider_rating", "access_standard"],
    "C": ["claim_header", "claim_line", "pharmacy_claim_detail", "claim_denial", "claim_pend",
          "claim_adjustment", "claim_payment", "appeal_record", "reprocessing_history"],
    "A": ["auth_request", "auth_line", "auth_determination", "clinical_review_queue", "documentation_requirement"],
    "CM": ["care_case", "care_plan", "disease_program_enrollment", "transition_of_care", "risk_cohort",
           "outreach_campaign", "outreach_history"],
    "K": ["knowledge_asset", "asset_chunk", "faq_entry", "agent_sop", "asset_link", "qa_golden"],
}

# Primary key column per entity (used for upserts and FK checks).
PRIMARY_KEY: dict[str, str] = {
    "member_demographics": "member_id", "family_relationship": "relationship_id", "plan": "plan_id",
    "plan_enrollment": "enrollment_id", "member_accumulator": "accumulator_id", "pcp_assignment": "assignment_id",
    "risk_score": "risk_score_id", "care_gap": "gap_id", "sdoh_indicator": "indicator_id",
    "provider": "npi", "network_participation": "participation_id", "practice_location": "location_id",
    "claim_header": "claim_id", "claim_line": "line_id", "pharmacy_claim_detail": "claim_id",
    "claim_denial": "denial_id", "claim_pend": "pend_id", "claim_adjustment": "adjustment_id",
    "claim_payment": "payment_id", "appeal_record": "appeal_id", "reprocessing_history": "reprocess_id",
    "auth_request": "auth_id", "auth_line": "auth_line_id", "auth_determination": "determination_id",
    "clinical_review_queue": "queue_id", "documentation_requirement": "req_id",
    "availability_schedule": "schedule_id", "provider_rating": "rating_id", "access_standard": "standard_id",
    "care_case": "case_id", "transition_of_care": "toc_id", "care_plan": "care_plan_id",
    "disease_program_enrollment": "enrollment_id", "risk_cohort": "cohort_id",
    "outreach_campaign": "campaign_id", "outreach_history": "outreach_id",
    "knowledge_asset": "asset_id", "asset_chunk": "chunk_id", "faq_entry": "faq_id", "agent_sop": "sop_id",
    "asset_link": "link_id", "qa_golden": "qa_id",
}

# Entities with a generator in v1. A payload naming anything else is rejected.
IMPLEMENTED = set(PRIMARY_KEY)

# Which stage generates which entity. v1 runs s6 after s5 (design doc, gap 1) and derives
# member_accumulator in s5 from paid claims (gap 2).
STAGE_ENTITIES: dict[str, list[str]] = {
    "s3": ["plan", "provider", "network_participation", "practice_location", "availability_schedule",
           "provider_rating", "access_standard"],
    "s4": ["member_demographics", "family_relationship", "plan_enrollment", "pcp_assignment",
           "risk_score", "care_gap", "sdoh_indicator"],
    "s5": ["claim_header", "claim_line", "pharmacy_claim_detail", "claim_denial", "claim_pend",
           "claim_adjustment", "claim_payment", "appeal_record", "reprocessing_history",
           "auth_request", "auth_line", "auth_determination", "clinical_review_queue",
           "documentation_requirement", "member_accumulator"],
    "s6": ["care_case", "transition_of_care", "care_plan", "disease_program_enrollment", "risk_cohort",
           "outreach_campaign", "outreach_history", "knowledge_asset", "asset_chunk", "faq_entry",
           "agent_sop", "asset_link", "qa_golden"],
}

# ---------------------------------------------------------------- use cases
USE_CASES: dict[str, dict] = {
    "PCS": {
        "name": "Claim status", "group": "provider", "personas": ["OFFICE", "BILLING", "DISCHARGE"],
        "can": ["Report status, pend reason and expected date", "Explain denial code in plain language",
                "Give corrected-claim guidance"],
        "escalate": ["Contract interpretation", "Payment integrity", "System errors"],
        "never": ["Disclose without NPI/TIN verification", "Promise payment", "Alter claims"],
        "verification": ["NPI", "TIN", "claim ID or member + date of service"],
        "handoff_queue": "CLAIMS_TIER2",
        "faqs": [
            ["What is the status of my claim?", "Can you check claim status for me?", "Where is this claim at?"],
            ["Why was my claim denied?", "What does this denial code mean?", "Explain the denial reason."],
            ["What do you need to process a pended claim?", "The claim is pending, what is missing?"],
        ],
    },
    "PAS": {
        "name": "Authorization status", "group": "provider", "personas": ["OFFICE", "REFERRAL", "DISCHARGE"],
        "can": ["Report status and due time", "List required documents", "Give determination and appeal/P2P steps",
                "Accept documents", "Route urgent requests for expedited review"],
        "escalate": ["All clinical review", "Urgent cases near SLA breach"],
        "never": ["Approve or deny", "Interpret criteria as a decision"],
        "verification": ["NPI", "TIN", "patient identifiers"],
        "handoff_queue": "UM_INTAKE",
        "faqs": [
            ["What is the status of this authorization?", "Has the prior auth been decided?"],
            ["What documents are missing for this auth?", "What do you still need from us?"],
            ["How do I request a peer-to-peer review?", "Can the doctor talk to the reviewer?"],
        ],
    },
    "PEV": {
        "name": "Eligibility verification", "group": "provider", "personas": ["OFFICE", "BILLING", "DISCHARGE"],
        "can": ["Return eligibility for a date of service", "Give plan, PCP and cost-share status",
                "Report COB and PA indicators with a reference number"],
        "escalate": ["Retro changes", "Enrollment pending cases", "Data conflicts"],
        "never": ["Guarantee payment", "Disclose beyond minimum necessary"],
        "verification": ["NPI", "TIN", "patient name/DOB or member ID"],
        "handoff_queue": "ELIGIBILITY_SUPPORT",
        "faqs": [
            ["Is this patient eligible on the date of service?", "Can you verify coverage?"],
            ["What is the patient's copay for an office visit?", "What cost share applies?"],
            ["Is prior authorization required for imaging?", "Does this service need a PA?"],
        ],
    },
}

USE_CASES.update({
    "MBN": {
        "name": "Benefits navigation", "group": "member", "personas": ["MEDICARE", "MEDICAID", "COMMERCIAL", "CAREGIVER"],
        "can": ["Quote copay, coinsurance, deductible and out-of-pocket status", "Explain allowances and covered services",
                "Cite the plan summary"],
        "escalate": ["Eligibility disputes", "Grievances", "Coverage exceptions"],
        "never": ["Promise coverage", "Interpret legal terms", "Disclose beyond the verified scope"],
        "verification": ["name", "date of birth", "member ID"], "handoff_queue": "MEMBER_SERVICES",
        "faqs": [["How much is a specialist visit?", "What do I pay to see a specialist?"],
                 ["How much of my deductible have I met?", "Where am I on my deductible?"],
                 ["Is transportation to appointments covered?", "Can I get a ride to my doctor?"]],
    },
    "MCI": {
        "name": "Claims inquiry", "group": "member", "personas": ["MEDICARE", "MEDICAID", "COMMERCIAL", "CAREGIVER"],
        "can": ["Report claim status", "Explain allowed, paid and member responsibility", "Explain denial reasons"],
        "escalate": ["Surprise-bill disputes", "Payment integrity", "Suspected errors above threshold"],
        "never": ["Reprocess or override claims", "Promise payment", "Give legal advice"],
        "verification": ["name", "date of birth", "member ID"], "handoff_queue": "CLAIMS_MEMBER",
        "faqs": [["Why do I owe this amount?", "Explain my EOB."],
                 ["Why was my claim denied?", "What does this denial mean for me?"],
                 ["Why do I see two versions of the same claim?", "Why does my claim show as adjusted?"]],
    },
    "MPS": {
        "name": "Provider search", "group": "member", "personas": ["MEDICARE", "MEDICAID", "COMMERCIAL", "CAREGIVER"],
        "can": ["Search the directory by specialty, distance, language and accessibility", "Show ratings and next availability"],
        "escalate": ["Network adequacy complaints", "No in-network option within the access standard"],
        "never": ["Recommend clinically", "Recommend a termed or pending provider", "Share unverified information"],
        "verification": ["member ID"], "handoff_queue": "NETWORK_SUPPORT",
        "faqs": [["Find an in-network cardiologist near me.", "Who are the cardiologists in my network?"],
                 ["I need a Spanish-speaking pediatrician.", "Is there a pediatrician who speaks Spanish?"],
                 ["Which offices are wheelchair accessible?", "I need an accessible clinic."]],
    },
    "MPC": {
        "name": "PCP change", "group": "member", "personas": ["MEDICARE", "MEDICAID", "COMMERCIAL", "CAREGIVER"],
        "can": ["Validate the new PCP", "Apply the change with an effective date", "Send confirmation"],
        "escalate": ["Continuity-of-care exceptions", "County restrictions with no alternative"],
        "never": ["Change PCP without verification", "Assign a closed-panel PCP"],
        "verification": ["name", "date of birth", "member ID"], "handoff_queue": "MEMBER_SERVICES",
        "faqs": [["I want to change my primary care doctor.", "How do I switch my PCP?"],
                 ["My doctor left the network, what now?", "Why was I assigned a new PCP?"],
                 ["When does my PCP change take effect?", "What is the effective date of the change?"]],
    },
    "MAS": {
        "name": "Appointment scheduling", "group": "member", "personas": ["MEDICARE", "MEDICAID", "COMMERCIAL", "CAREGIVER"],
        "can": ["Show next available slots", "Book visit types", "Suggest telehealth", "Bundle care-gap visits"],
        "escalate": ["No slot within the wait standard", "Urgent symptoms"],
        "never": ["Triage symptoms", "Book with non-participating providers"],
        "verification": ["name", "date of birth", "member ID"], "handoff_queue": "SCHEDULING",
        "faqs": [["When is the next appointment with my doctor?", "Can I see my PCP this week?"],
                 ["Can I book my annual wellness visit?", "I need a checkup."],
                 ["Is there a telehealth option?", "Can I do a video visit?"]],
    },
    "PAP": {
        "name": "Appeals", "group": "provider", "personas": ["BILLING", "OFFICE", "DISCHARGE"],
        "can": ["Open appeals", "List documents and deadlines", "Report status and decision", "Show reprocessing history"],
        "escalate": ["Late filings", "Bulk or systemic denials", "External review requests"],
        "never": ["Predict outcomes", "Waive deadlines"],
        "verification": ["NPI", "TIN", "claim ID"], "handoff_queue": "APPEALS",
        "faqs": [["How do I appeal this denial?", "Can I start an appeal?"],
                 ["What is the status of my appeal?", "Has the appeal been decided?"],
                 ["The appeal was overturned, when will it be reprocessed?", "When will the claim be paid after the appeal?"]],
    },
    "PPI": {
        "name": "Payment inquiry", "group": "provider", "personas": ["BILLING", "OFFICE", "DISCHARGE"],
        "can": ["Give payment date, amount, method and trace", "Explain adjustments and offsets"],
        "escalate": ["Misdirected payments", "Wrong payee", "Disputed recoupments"],
        "never": ["Change payment instructions", "Issue payments"],
        "verification": ["NPI", "TIN", "claim ID"], "handoff_queue": "PROVIDER_PAYMENTS",
        "faqs": [["When was this claim paid?", "What is the EFT trace number?"],
                 ["Why was I paid less than billed?", "Explain the adjustments."],
                 ["Why was money taken back from my payment?", "What is this recoupment?"]],
    },
})

# Scope a caregiver's authorization_scope must cover for a member-facing use case (rule R-06)
CAREGIVER_SCOPE = {"MBN": "BENEFITS", "MCI": "CLAIMS", "MPC": "PCP_CHANGE", "MAS": "APPOINTMENTS", "MBP": "BILLING", "MPA": "PA_STATUS"}

# Built-in use cases, frozen before any capability pack registers: their position is their knowledge-asset slot
# (0-9), which feeds asset ids, so it must never change. Pack use cases get slots 10-98 (USE_CASE_SLOTS).
CORE_USE_CASES: tuple[str, ...] = tuple(sorted(USE_CASES))
USE_CASE_SLOTS: dict[str, int] = {uc: i for i, uc in enumerate(CORE_USE_CASES)}
CORE_ENTITIES: frozenset[str] = frozenset(PRIMARY_KEY)


# ---------------------------------------------------------------- capability packs (factory.capability registry)
# Packs extend the catalog at activation, in one place, and can be removed again (core entries cannot).
def register_entity(name: str, stage: str, primary_key: str, domain: str = "PACK") -> None:
    if name in CORE_ENTITIES:
        return  # core packs (e.g. sdoh_indicator) are already in the catalog
    ENTITIES.setdefault(domain, [])
    if name not in ENTITIES[domain]:
        ENTITIES[domain].append(name)
    PRIMARY_KEY[name] = primary_key
    IMPLEMENTED.add(name)
    if name not in STAGE_ENTITIES[stage]:
        STAGE_ENTITIES[stage].append(name)


def unregister_entity(name: str) -> None:
    if name in CORE_ENTITIES:
        return
    for ents in list(ENTITIES.values()) + list(STAGE_ENTITIES.values()):
        if name in ents:
            ents.remove(name)
    PRIMARY_KEY.pop(name, None)
    IMPLEMENTED.discard(name)


def register_use_case(code: str, info: dict, caregiver_scope: str, slot: int) -> None:
    if code in CORE_USE_CASES:
        raise ValueError(f"{code} is a built-in use case")
    taken = {uc: s for uc, s in USE_CASE_SLOTS.items() if s == slot and uc != code}
    if taken:
        raise ValueError(f"knowledge slot {slot} of {code} is taken by {sorted(taken)}; set knowledge_slot in the pack")
    USE_CASES[code] = info
    CAREGIVER_SCOPE[code] = caregiver_scope
    USE_CASE_SLOTS[code] = slot


def unregister_use_case(code: str) -> None:
    if code in CORE_USE_CASES:
        return
    USE_CASES.pop(code, None)
    USE_CASE_SLOTS.pop(code, None)

# Scenario recipes live in scenarios.py; re-exported here for code that reads the catalog.
from factory.data_engine.scenarios import FORCED_CLAIM_SCENARIOS, HAPPY_CLAIM_SCENARIOS, SCENARIOS  # noqa: E402,F401

# Dirty-data tags (Q-02): rows that break a rule on purpose, exempted by name in s7.
DQ_STALE_DIRECTORY = "DQ-STALE-DIRECTORY"
DQ_STALE_SCHEDULE = "DQ-STALE-SCHEDULE"

# ---------------------------------------------------------------- distributions
LOB_PERSONA = {Lob.MEDICARE_ADV: "MEDICARE", Lob.MEDICAID_MCO: "MEDICAID", Lob.COMMERCIAL: "COMMERCIAL"}

# D-02 age by LOB: (low, high, share)
AGE_BANDS = {
    Lob.COMMERCIAL: [(0, 17, 20), (18, 44, 40), (45, 64, 36), (65, 75, 4)],
    Lob.MEDICARE_ADV: [(65, 90, 88), (30, 64, 12)],
    Lob.MEDICAID_MCO: [(0, 17, 45), (18, 64, 52), (65, 85, 3)],
}

# D-06 authorization outcomes
AUTH_OUTCOMES = ([AuthStatus.APPROVED, AuthStatus.PARTIAL, AuthStatus.DENIED, AuthStatus.IN_REVIEW, AuthStatus.PENDED_INFO, AuthStatus.SUBMITTED, AuthStatus.WITHDRAWN],
                 [82, 4, 7, 3, 2, 1, 1])

# Baseline denial categories -> CARC (group CO). AUTH (CO-197) is scenario-only.
DENIAL_CATEGORIES = {
    DenialCategory.MED_NECESSITY: ("50", "These services are not deemed medically necessary by the payer."),
    DenialCategory.CODING: ("11", "The diagnosis is inconsistent with the procedure."),
    DenialCategory.DUPLICATE: ("18", "Exact duplicate claim or service."),
    DenialCategory.TIMELY_FILING: ("29", "The time limit for filing has expired."),
    DenialCategory.COB: ("22", "This care may be covered by another payer per coordination of benefits."),
    DenialCategory.NON_COVERED: ("96", "Non-covered charge."),
    DenialCategory.MISSING_INFO: ("16", "Claim lacks information or has submission errors needed for adjudication."),
    DenialCategory.AUTH: ("197", "Precertification/authorization was absent."),
}
BASELINE_DENIALS = ([DenialCategory.MED_NECESSITY, DenialCategory.CODING, DenialCategory.DUPLICATE, DenialCategory.TIMELY_FILING, DenialCategory.COB, DenialCategory.NON_COVERED, DenialCategory.MISSING_INFO],
                    [25, 20, 15, 10, 10, 10, 10])

# ---------------------------------------------------------------- reference tables (pinned subsets)
# code, description, base rate USD, PA required, place (prof/fac)
CPT = [
    ("99213", "Office visit, established patient, low complexity", 95, False, "prof"),
    ("99214", "Office visit, established patient, moderate complexity", 135, False, "prof"),
    ("99203", "Office visit, new patient, low complexity", 110, False, "prof"),
    ("G0439", "Annual wellness visit, subsequent", 120, False, "prof"),
    ("93000", "Electrocardiogram, complete", 20, False, "prof"),
    ("71046", "Chest X-ray, 2 views", 35, False, "prof"),
    ("80053", "Comprehensive metabolic panel", 15, False, "prof"),
    ("85025", "Complete blood count with differential", 11, False, "prof"),
    ("36415", "Routine venipuncture", 3, False, "prof"),
    ("70553", "MRI brain without and with contrast", 450, True, "prof"),
    ("72148", "MRI lumbar spine without contrast", 300, True, "prof"),
    ("97161", "Physical therapy evaluation, low complexity", 95, True, "prof"),
    ("99223", "Initial hospital inpatient care, high complexity", 210, False, "fac"),
    ("99232", "Subsequent hospital inpatient care", 105, False, "fac"),
    ("99238", "Hospital discharge day management", 100, False, "fac"),
    ("27447", "Total knee arthroplasty", 1400, True, "fac"),
]
PA_CODES = [c[0] for c in CPT if c[3]]

ICD10 = ["E11.9", "I10", "J44.9", "I50.9", "M17.11", "M54.50", "Z00.00", "E78.5", "N18.30", "F32.9"]

# ndc, drug, formulary tier, PA required
NDC = [
    ("00093-1048-01", "Metformin 500 mg tablet", 1, False),
    ("00378-0208-01", "Lisinopril 10 mg tablet", 1, False),
    ("00071-0155-23", "Atorvastatin 20 mg tablet", 2, False),
    ("00006-0277-31", "Sitagliptin 100 mg tablet", 3, False),
    ("00597-0152-30", "Empagliflozin 10 mg tablet", 3, True),
    ("00169-4060-13", "Semaglutide 1 mg pen", 4, True),
]
TIER_COPAY_USD = {1: 0, 2: 10, 3: 45, 4: 100, 5: 150}

TAXONOMY = {
    ProviderType.PCP: [("207Q00000X", "Family Medicine"), ("207R00000X", "Internal Medicine"), ("208000000X", "Pediatrics")],
    ProviderType.SPECIALIST: [("207RC0000X", "Cardiovascular Disease"), ("207X00000X", "Orthopaedic Surgery"),
                   ("2085R0202X", "Diagnostic Radiology"), ("225100000X", "Physical Therapist")],
    ProviderType.FACILITY: [("282N00000X", "General Acute Care Hospital"), ("314000000X", "Skilled Nursing Facility"),
                 ("251E00000X", "Home Health"), ("333600000X", "Pharmacy")],
}
FACILITY_TYPE = {"282N00000X": FacilityType.HOSPITAL, "314000000X": FacilityType.SNF, "251E00000X": FacilityType.HOME_HEALTH, "333600000X": FacilityType.PHARMACY}

# city, state, zip, lat, lon, geo_type (approximate coordinates)
PLACES = [
    ("Houston", "TX", "77002", 29.756, -95.363, GeoType.URBAN), ("Austin", "TX", "78701", 30.271, -97.743, GeoType.URBAN),
    ("Tyler", "TX", "75701", 32.326, -95.296, GeoType.SUBURBAN), ("Alpine", "TX", "79830", 30.358, -103.661, GeoType.RURAL),
    ("Miami", "FL", "33130", 25.767, -80.205, GeoType.URBAN), ("Tampa", "FL", "33602", 27.951, -82.458, GeoType.URBAN),
    ("Ocala", "FL", "34471", 29.187, -82.140, GeoType.SUBURBAN), ("Sebring", "FL", "33870", 27.495, -81.441, GeoType.RURAL),
    ("Columbus", "OH", "43215", 39.962, -83.000, GeoType.URBAN), ("Dayton", "OH", "45402", 39.759, -84.192, GeoType.SUBURBAN),
    ("Athens", "OH", "45701", 39.329, -82.101, GeoType.RURAL), ("Pittsburgh", "PA", "15222", 40.441, -79.996, GeoType.URBAN),
    ("Lancaster", "PA", "17602", 40.038, -76.306, GeoType.SUBURBAN), ("Wellsboro", "PA", "16901", 41.749, -77.300, GeoType.RURAL),
    ("Atlanta", "GA", "30303", 33.749, -84.388, GeoType.URBAN), ("Macon", "GA", "31201", 32.841, -83.632, GeoType.SUBURBAN),
    ("Tifton", "GA", "31794", 31.450, -83.508, GeoType.RURAL), ("Phoenix", "AZ", "85004", 33.448, -112.074, GeoType.URBAN),
    ("Flagstaff", "AZ", "86001", 35.198, -111.651, GeoType.SUBURBAN), ("Detroit", "MI", "48226", 42.331, -83.046, GeoType.URBAN),
]

FIRST_F = ["Maria", "Linda", "Patricia", "Barbara", "Elizabeth", "Jennifer", "Susan", "Dorothy", "Carmen", "Rosa",
           "Grace", "Helen", "Ana", "Joyce", "Gloria", "Ruth", "Teresa", "Alice", "Diane", "Janet"]
FIRST_M = ["James", "Robert", "John", "Michael", "William", "David", "Richard", "Joseph", "Carlos", "Luis",
           "Thomas", "Charles", "George", "Frank", "Raymond", "Walter", "Henry", "Arthur", "Jose", "Samuel"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson"]
