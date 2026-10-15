"""Catalog 2.4.0: domains, entities, use cases, scenarios and pinned reference tables.

Reference tables here are small illustrative subsets. CPT codes are AMA-licensed; production
needs a licensed CPT table or synthetic codes. NDCs below are illustrative, not verified.
"""

from __future__ import annotations

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
    "care_case": "case_id", "transition_of_care": "toc_id",
    "knowledge_asset": "asset_id", "asset_chunk": "chunk_id", "faq_entry": "faq_id", "agent_sop": "sop_id",
    "asset_link": "link_id", "qa_golden": "qa_id",
}

# Entities with a generator in v1. Anything else in a blueprint fails fast.
IMPLEMENTED = set(PRIMARY_KEY)

# Which stage generates which entity. v1 runs s6 after s5 (design doc, gap 1) and derives
# member_accumulator in s5 from paid claims (gap 2).
STAGE_ENTITIES: dict[str, list[str]] = {
    "s3": ["plan", "provider", "network_participation", "practice_location"],
    "s4": ["member_demographics", "family_relationship", "plan_enrollment", "pcp_assignment",
           "risk_score", "care_gap", "sdoh_indicator"],
    "s5": ["claim_header", "claim_line", "pharmacy_claim_detail", "claim_denial", "claim_pend",
           "claim_adjustment", "claim_payment", "appeal_record", "reprocessing_history",
           "auth_request", "auth_line", "auth_determination", "clinical_review_queue",
           "documentation_requirement", "member_accumulator"],
    "s6": ["care_case", "transition_of_care", "knowledge_asset", "asset_chunk", "faq_entry",
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

# Scenario facts the generator enforces (S1 use cases). Others fall back to baseline data.
SCENARIOS: dict[str, dict] = {
    "PCS-01": {"anchor": "claim", "type": "happy", "title": "Status by claim number", "claim_status": "PAID"},
    "PCS-02": {"anchor": "claim", "type": "edge", "title": "Pended for records", "claim_status": "PENDED",
               "pend_reason": "RECORDS_REQUEST"},
    "PCS-03": {"anchor": "claim", "type": "edge", "title": "Denied CO-16 missing information",
               "claim_status": "DENIED", "denial_category": "MISSING_INFO"},
    "PCS-04": {"anchor": "claim", "type": "compliance", "title": "Caller fails verification (API-level)",
               "claim_status": "PAID"},
    "PAS-01": {"anchor": "auth", "type": "happy", "title": "Routine status check", "auth_status": "IN_REVIEW"},
    "PAS-02": {"anchor": "auth", "type": "edge", "title": "Urgent SNF authorization at discharge",
               "auth_status": "PENDED_INFO", "doc_type": "DISCHARGE_SUMMARY"},
    "PAS-03": {"anchor": "auth", "type": "edge", "title": "Missing PT evaluation", "auth_status": "PENDED_INFO",
               "doc_type": "PT_EVAL"},
    "PAS-04": {"anchor": "auth", "type": "compliance", "title": "Denied; asks for reviewer details",
               "auth_status": "DENIED"},
    "PEV-01": {"anchor": "member", "type": "happy", "title": "Active member check"},
    "PEV-02": {"anchor": "member", "type": "edge", "title": "Termed retroactively"},
    "PEV-03": {"anchor": "member", "type": "edge", "title": "Enrollment pending"},
    "PEV-04": {"anchor": "member", "type": "compliance", "title": "Bulk lookup attempt (API-level)"},
}

# Dirty-data tags (Q-02): rows that break a rule on purpose, exempted by name in s7.
DQ_STALE_DIRECTORY = "DQ-STALE-DIRECTORY"

# ---------------------------------------------------------------- distributions
LOB_PERSONA = {"MEDICARE_ADV": "MEDICARE", "MEDICAID_MCO": "MEDICAID", "COMMERCIAL": "COMMERCIAL"}

# D-02 age by LOB: (low, high, share)
AGE_BANDS = {
    "COMMERCIAL": [(0, 17, 20), (18, 44, 40), (45, 64, 36), (65, 75, 4)],
    "MEDICARE_ADV": [(65, 90, 88), (30, 64, 12)],
    "MEDICAID_MCO": [(0, 17, 45), (18, 64, 52), (65, 85, 3)],
}

# D-06 authorization outcomes
AUTH_OUTCOMES = (["APPROVED", "PARTIAL", "DENIED", "IN_REVIEW", "PENDED_INFO", "SUBMITTED", "WITHDRAWN"],
                 [82, 4, 7, 3, 2, 1, 1])

# Baseline denial categories -> CARC (group CO). AUTH (CO-197) is scenario-only.
DENIAL_CATEGORIES = {
    "MED_NECESSITY": ("50", "These services are not deemed medically necessary by the payer."),
    "CODING": ("11", "The diagnosis is inconsistent with the procedure."),
    "DUPLICATE": ("18", "Exact duplicate claim or service."),
    "TIMELY_FILING": ("29", "The time limit for filing has expired."),
    "COB": ("22", "This care may be covered by another payer per coordination of benefits."),
    "NON_COVERED": ("96", "Non-covered charge."),
    "MISSING_INFO": ("16", "Claim lacks information or has submission errors needed for adjudication."),
    "AUTH": ("197", "Precertification/authorization was absent."),
}
BASELINE_DENIALS = (["MED_NECESSITY", "CODING", "DUPLICATE", "TIMELY_FILING", "COB", "NON_COVERED", "MISSING_INFO"],
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
    "PCP": [("207Q00000X", "Family Medicine"), ("207R00000X", "Internal Medicine")],
    "SPECIALIST": [("207RC0000X", "Cardiovascular Disease"), ("207X00000X", "Orthopaedic Surgery"),
                   ("2085R0202X", "Diagnostic Radiology"), ("225100000X", "Physical Therapist")],
    "FACILITY": [("282N00000X", "General Acute Care Hospital"), ("314000000X", "Skilled Nursing Facility"),
                 ("251E00000X", "Home Health"), ("333600000X", "Pharmacy")],
}
FACILITY_TYPE = {"282N00000X": "HOSPITAL", "314000000X": "SNF", "251E00000X": "HOME_HEALTH", "333600000X": "PHARMACY"}

# city, state, zip, lat, lon, geo_type (approximate coordinates)
PLACES = [
    ("Houston", "TX", "77002", 29.756, -95.363, "URBAN"), ("Austin", "TX", "78701", 30.271, -97.743, "URBAN"),
    ("Tyler", "TX", "75701", 32.326, -95.296, "SUBURBAN"), ("Alpine", "TX", "79830", 30.358, -103.661, "RURAL"),
    ("Miami", "FL", "33130", 25.767, -80.205, "URBAN"), ("Tampa", "FL", "33602", 27.951, -82.458, "URBAN"),
    ("Ocala", "FL", "34471", 29.187, -82.140, "SUBURBAN"), ("Sebring", "FL", "33870", 27.495, -81.441, "RURAL"),
    ("Columbus", "OH", "43215", 39.962, -83.000, "URBAN"), ("Dayton", "OH", "45402", 39.759, -84.192, "SUBURBAN"),
    ("Athens", "OH", "45701", 39.329, -82.101, "RURAL"), ("Pittsburgh", "PA", "15222", 40.441, -79.996, "URBAN"),
    ("Lancaster", "PA", "17602", 40.038, -76.306, "SUBURBAN"), ("Wellsboro", "PA", "16901", 41.749, -77.300, "RURAL"),
    ("Atlanta", "GA", "30303", 33.749, -84.388, "URBAN"), ("Macon", "GA", "31201", 32.841, -83.632, "SUBURBAN"),
    ("Tifton", "GA", "31794", 31.450, -83.508, "RURAL"), ("Phoenix", "AZ", "85004", 33.448, -112.074, "URBAN"),
    ("Flagstaff", "AZ", "86001", 35.198, -111.651, "SUBURBAN"), ("Detroit", "MI", "48226", 42.331, -83.046, "URBAN"),
]

FIRST_F = ["Maria", "Linda", "Patricia", "Barbara", "Elizabeth", "Jennifer", "Susan", "Dorothy", "Carmen", "Rosa",
           "Grace", "Helen", "Ana", "Joyce", "Gloria", "Ruth", "Teresa", "Alice", "Diane", "Janet"]
FIRST_M = ["James", "Robert", "John", "Michael", "William", "David", "Richard", "Joseph", "Carlos", "Luis",
           "Thomas", "Charles", "George", "Frank", "Raymond", "Walter", "Henry", "Arthur", "Jose", "Samuel"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson"]
