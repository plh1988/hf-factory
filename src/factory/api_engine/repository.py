"""Data access for the API handlers: every SQL statement of the business API lives here.

Goes only through the TableStore (portable SQL with ? placeholders: DuckDB locally, AlloyDB in the cloud) and the
BlobStore. Handlers call named methods and contain no SQL. Coded values are bound as parameters, never inlined.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pyarrow as pa

from factory import codes
from factory.adapters.ports import Stores


class Repository:
    """Read access for the API endpoints, through the TableStore port with portable SQL (DuckDB locally, AlloyDB in
    the cloud). Methods return plain dicts; `*_ref` lookups accept either the internal id or the business number.
    """
    def __init__(self, stores: Stores) -> None:
        self.tables = stores.tables
        self.blobs = stores.blobs

    def _rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        return self.tables.query(sql, params).to_pylist()

    def _one(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ---------------------------------------------------------------- providers
    def provider(self, npi: str) -> dict | None:
        return self._one("SELECT npi, tin, provider_type, status FROM provider WHERE npi = ?", [npi])

    # ---------------------------------------------------------------- claims
    def claim(self, claim_ref: str) -> dict | None:
        return self._one("SELECT * FROM claim_header WHERE claim_number = ? OR claim_id = ?", [claim_ref, claim_ref])

    def claim_detail(self, claim_id: str) -> dict:
        return {
            "denial": self._one("SELECT carc, denial_category, reason_text, liable_party, appealable, appeal_deadline_dt "
                                "FROM claim_denial WHERE claim_id = ?", [claim_id]),
            "pend": self._one("SELECT pend_reason, info_needed_from, pend_dt, expected_resolution_dt "
                              "FROM claim_pend WHERE claim_id = ?", [claim_id]),
            "payment": self._one("SELECT payment_dt, method, paid_amount, trace_no FROM claim_payment WHERE claim_id = ?", [claim_id]),
            "lines": self._rows("SELECT line_no, cpt_hcpcs, units, billed, allowed, paid, line_status FROM claim_line "
                                "WHERE claim_id = ? ORDER BY line_no", [claim_id]),
        }

    def member_claims(self, member_id: str, status: str | None, limit: int) -> list[dict]:
        """A member's claims, newest service date first (MCI)."""
        sql, params = "SELECT h.* FROM claim_header h WHERE h.member_id = ?", [member_id]
        if status:
            sql, params = sql + " AND h.status = ?", params + [status]
        return self._rows(sql + " ORDER BY h.service_from_dt DESC, h.claim_number LIMIT ?", params + [limit])

    def claim_lines_with_liability(self, claim_id: str) -> list[dict]:
        """Claim lines with the member-liability split (deductible, copay, coinsurance) for EOB math."""
        return self._rows("SELECT line_no, cpt_hcpcs, units, billed, allowed, deductible, copay, coinsurance, paid, line_status "
                          "FROM claim_line WHERE claim_id = ? ORDER BY line_no", [claim_id])

    def claim_number(self, claim_id: str) -> str | None:
        row = self._one("SELECT claim_number FROM claim_header WHERE claim_id = ?", [claim_id])
        return row and row["claim_number"]

    def reprocessing(self, claim_id: str, either_side: bool = False) -> dict | None:
        """Reprocessing of a claim, with the replacement's number; either_side also finds it from the replacement."""
        if not self.tables.exists("reprocessing_history"):
            return None
        where, params = ("r.claim_id = ? OR r.new_claim_id = ?", [claim_id, claim_id]) if either_side else ("r.claim_id = ?", [claim_id])
        return self._one("SELECT r.*, n.claim_number AS new_number FROM reprocessing_history r "
                         f"JOIN claim_header n ON n.claim_id = r.new_claim_id WHERE {where}", params)

    def replacement_claim_ids(self, claim_id: str) -> list[str]:
        if not self.tables.exists("reprocessing_history"):
            return []
        return [x["new_claim_id"] for x in self._rows("SELECT new_claim_id FROM reprocessing_history WHERE claim_id = ?", [claim_id])]

    def payments(self, claim_ids: list[str]) -> list[dict]:
        """Payments for these claims (an original and its replacements), oldest first, with the claim number."""
        if not claim_ids or not self.tables.exists("claim_payment"):
            return []
        marks = ", ".join("?" * len(claim_ids))
        return self._rows("SELECT p.*, h.claim_number FROM claim_payment p JOIN claim_header h ON h.claim_id = p.claim_id "
                          f"WHERE p.claim_id IN ({marks}) ORDER BY p.payment_dt", claim_ids)

    def adjustment_totals(self, claim_id: str) -> list[dict]:
        """Adjustment amounts of a claim summed per group code (CO, PR) and CARC."""
        if not self.tables.exists("claim_adjustment"):
            return []
        return self._rows("SELECT a.group_code, a.carc, sum(a.amount) AS amount FROM claim_adjustment a "
                          "JOIN claim_line l ON l.line_id = a.line_id WHERE l.claim_id = ? GROUP BY 1, 2 ORDER BY 1, 2", [claim_id])

    # ---------------------------------------------------------------- appeals
    def appeal(self, appeal_ref: str) -> dict | None:
        return self._one("SELECT * FROM appeal_record WHERE appeal_number = ? OR appeal_id = ?", [appeal_ref, appeal_ref])

    def open_appeal(self, claim_id: str) -> dict | None:
        """An appeal on the claim that is not yet decided (one open appeal per claim)."""
        return self._one("SELECT appeal_number, status FROM appeal_record WHERE claim_id = ? AND status <> ?",
                         [claim_id, codes.AppealStatus.DECIDED])

    # ---------------------------------------------------------------- authorizations
    def auth(self, auth_ref: str) -> dict | None:
        return self._one("SELECT * FROM auth_request WHERE auth_number = ? OR auth_id = ?", [auth_ref, auth_ref])

    def auth_detail(self, auth_id: str) -> dict:
        return {
            "lines": self._rows("SELECT cpt_hcpcs_or_ndc, units_requested, units_approved, line_status FROM auth_line "
                                "WHERE auth_id = ? ORDER BY auth_line_id", [auth_id]),
            "determination": self._one("SELECT outcome, valid_from, valid_to, denial_category, denial_rationale, "
                                       "decided_by_role, determination_dt, appeal_rights_sent_dt "
                                       "FROM auth_determination WHERE auth_id = ?", [auth_id]),
            "review": self._one("SELECT review_level, status, sla_due_dt FROM clinical_review_queue WHERE auth_id = ?", [auth_id]),
            "documents": self._rows("SELECT req_id, doc_type, state, requested_dt, due_dt, clock_paused "
                                    "FROM documentation_requirement WHERE auth_id = ? ORDER BY req_id", [auth_id]),
        }

    def receive_document(self, req_id: str) -> None:
        """Mark a requested document as received; the decision clock resumes (T-03)."""
        row = self.tables.query("SELECT * FROM documentation_requirement WHERE req_id = ?", [req_id])
        updated = row.set_column(row.schema.get_field_index("state"), "state", pa.array(["RECEIVED"]))
        updated = updated.set_column(updated.schema.get_field_index("clock_paused"), "clock_paused", pa.array([False]))
        self.tables.upsert("documentation_requirement", updated, "req_id")

    # ---------------------------------------------------------------- members and eligibility
    def member(self, member_ref: str) -> dict | None:
        return self._one("SELECT * FROM member_demographics WHERE member_number = ? OR member_id = ?", [member_ref, member_ref])

    def member_by_demographics(self, first: str, last: str, dob: date) -> dict | None:
        return self._one("SELECT * FROM member_demographics WHERE lower(first_name) = lower(?) AND lower(last_name) = lower(?) "
                         "AND dob = ?", [first, last, dob])

    def enrollment_on(self, member_id: str, dos: date) -> dict | None:
        return self._one(
            "SELECT e.*, p.plan_code, p.plan_name, p.lob, p.network_type, p.deductible, p.oop_max, p.copay_pcp, "
            "p.copay_specialist, p.coinsurance_pct, p.pa_required_codes FROM plan_enrollment e JOIN plan p ON p.plan_id = e.plan_id "
            "WHERE e.member_id = ? AND e.effective_dt <= ? AND (e.term_dt IS NULL OR e.term_dt >= ?) "
            "ORDER BY e.effective_dt DESC", [member_id, dos, dos])

    def latest_enrollment(self, member_id: str) -> dict | None:
        return self._one("SELECT * FROM plan_enrollment WHERE member_id = ? ORDER BY effective_dt DESC", [member_id])

    def pcp_on(self, member_id: str, dos: date) -> dict | None:
        return self._one(
            "SELECT a.pcp_npi, p.first_name, p.last_name, p.specialty FROM pcp_assignment a JOIN provider p ON p.npi = a.pcp_npi "
            "WHERE a.member_id = ? AND a.effective_dt <= ? AND (a.term_dt IS NULL OR a.term_dt >= ?)", [member_id, dos, dos])

    def accumulator(self, member_id: str, year: int) -> dict | None:
        return self._one("SELECT deductible_met, oop_met FROM member_accumulator WHERE member_id = ? AND plan_year = ? "
                         "AND network = 'IN'", [member_id, year])

    def active_pcp_assignment(self, member_id: str) -> dict | None:
        return self._one("SELECT * FROM pcp_assignment WHERE member_id = ? AND status = ?",
                         [member_id, codes.PcpAssignmentStatus.ACTIVE])

    # ---------------------------------------------------------------- network, directory and scheduling
    def access_standard(self, key: str, geo_type: str, plan_id: str | None = None) -> dict | None:
        """Distance and wait standard for a specialty key and geography; the plan's own row when plan_id is given."""
        if not self.tables.exists("access_standard"):
            return None
        sql = "SELECT max_distance_miles, max_wait_days FROM access_standard WHERE provider_type_or_specialty = ? AND geo_type = ?"
        params: list[Any] = [key, geo_type]
        if plan_id:
            sql, params = sql + " AND plan_id = ?", params + [plan_id]
        return self._one(sql + " ORDER BY plan_id LIMIT 1", params)

    def directory_listings(self, specialty: str, plan_id: str | None) -> list[dict]:
        """Active, participating providers with their practice locations; specialty 'PCP' means any primary care provider."""
        sql = ("SELECT DISTINCT p.npi, p.first_name, p.last_name, p.org_name, p.specialty, p.languages, l.location_id, l.address, "
               "l.city, l.state, l.lat, l.lon, l.accepting_new_patients, l.accessibility, l.telehealth, l.last_verified_dt "
               "FROM provider p JOIN network_participation n ON n.npi = p.npi JOIN practice_location l ON l.npi = p.npi "
               "WHERE p.status = ? AND n.status = ? AND n.term_dt IS NULL")
        params: list[Any] = [codes.ProviderStatus.ACTIVE, codes.ParticipationStatus.PARTICIPATING]
        if specialty == codes.ProviderType.PCP:
            sql, params = sql + " AND p.provider_type = ?", params + [codes.ProviderType.PCP]
        else:
            sql, params = sql + " AND p.specialty = ?", params + [specialty]
        if plan_id:
            sql, params = sql + " AND n.plan_id = ?", params + [plan_id]
        return self._rows(sql, params)

    def public_ratings(self) -> dict[str, Any]:
        """npi -> quality score, only for ratings with at least 20 reviews."""
        if not self.tables.exists("provider_rating"):
            return {}
        rows = self._rows("SELECT npi, quality_score FROM provider_rating WHERE rating_count >= 20", [])
        return {x["npi"]: x["quality_score"] for x in rows}

    def next_available_by_location(self) -> dict[str, Any]:
        if not self.tables.exists("availability_schedule"):
            return {}
        rows = self._rows("SELECT location_id, min(next_available_dt) AS nxt FROM availability_schedule GROUP BY 1", [])
        return {x["location_id"]: x["nxt"] for x in rows}

    def provider_on_plan(self, npi: str, plan_id: str) -> dict | None:
        """Provider with its participation status and panel on one plan (participation fields are None when absent)."""
        return self._one("SELECT p.npi, p.provider_type, p.first_name, p.last_name, p.status, n.status AS part, n.panel_status "
                         "FROM provider p LEFT JOIN network_participation n ON n.npi = p.npi AND n.plan_id = ? WHERE p.npi = ?",
                         [plan_id, npi])

    def participation(self, npi: str, plan_id: str) -> dict | None:
        return self._one("SELECT status FROM network_participation WHERE npi = ? AND plan_id = ? AND term_dt IS NULL", [npi, plan_id])

    def provider_profile(self, npi: str) -> dict | None:
        return self._one("SELECT npi, provider_type, specialty, first_name, last_name, org_name FROM provider WHERE npi = ?", [npi])

    def practice_locations(self, npi: str) -> list[dict]:
        return self._rows("SELECT lat, lon FROM practice_location WHERE npi = ?", [npi])

    def schedules(self, npi: str, location_id: str | None = None) -> list[dict]:
        """Availability schedule rows of a provider, optionally for one location."""
        if location_id is not None:
            return self._rows("SELECT * FROM availability_schedule WHERE npi = ? AND location_id = ?", [npi, location_id])
        return self._rows("SELECT * FROM availability_schedule WHERE npi = ? ORDER BY location_id, day_of_week", [npi])

    def alternative_providers(self, specialty: str, exclude_npi: str, limit: int = 3) -> list[dict]:
        """Other providers of the same specialty with the soonest availability, and whether they offer telehealth."""
        return self._rows(
            "SELECT s.npi, min(s.next_available_dt) AS next_available_dt, "
            "max(CAST(list_contains(s.appointment_types, 'TELEHEALTH') AS INTEGER)) AS telehealth "
            "FROM availability_schedule s JOIN provider p ON p.npi = s.npi WHERE p.specialty = ? AND s.npi <> ? "
            "GROUP BY 1 ORDER BY 2 LIMIT ?", [specialty, exclude_npi, limit])

    # ---------------------------------------------------------------- appointments (blobs until an appointment table exists)
    def appointment_exists(self, appointment_id: str) -> bool:
        return f"appointments/{appointment_id}.json" in self.blobs.list("appointments/")

    def save_appointment(self, record: dict[str, Any]) -> None:
        self.blobs.put(f"appointments/{record['appointment_id']}.json", json.dumps(record).encode(), "application/json")

    # ---------------------------------------------------------------- writes and lookups for S2-S4 use cases
    def exists(self, entity: str) -> bool:
        return self.tables.exists(entity)

    def insert_row(self, entity: str, key: str, row: dict[str, Any]) -> None:
        """Upsert one row using the table's own schema (keeps types identical to generated rows)."""
        schema = self.tables.query(f'SELECT * FROM "{entity}" LIMIT 0').schema
        full = {f.name: row.get(f.name) for f in schema}
        self.tables.upsert(entity, pa.Table.from_pylist([full], schema=schema), key)

    def caregiver(self, member_id: str, relationship_id: str) -> dict | None:
        return self._one("SELECT * FROM family_relationship WHERE member_id = ? AND relationship_id = ? AND caregiver_flag",
                         [member_id, relationship_id])

    def plan(self, plan_id: str) -> dict | None:
        return self._one("SELECT * FROM plan WHERE plan_id = ?", [plan_id])

    def plan_documents(self, plan_id: str) -> list[dict]:
        if not (self.tables.exists("asset_link") and self.tables.exists("knowledge_asset")):
            return []
        return self._rows("SELECT k.asset_id, k.title, k.asset_type FROM asset_link l JOIN knowledge_asset k ON k.asset_id = l.asset_id "
                          "WHERE l.linked_entity = 'PLAN' AND l.linked_key = ? AND k.status = 'ACTIVE' ORDER BY k.asset_type", [plan_id])

    def accumulators(self, member_id: str, year: int) -> list[dict]:
        if not self.tables.exists("member_accumulator"):
            return []
        return self._rows("SELECT network, deductible_met, oop_met FROM member_accumulator WHERE member_id = ? AND plan_year = ?",
                          [member_id, year])

    def sdoh(self, member_id: str) -> list[dict]:
        if not self.tables.exists("sdoh_indicator"):
            return []
        return self._rows("SELECT domain, positive, referral_status FROM sdoh_indicator WHERE member_id = ?", [member_id])

    def open_gaps(self, member_id: str) -> list[dict]:
        if not self.tables.exists("care_gap"):
            return []
        return self._rows("SELECT measure, due_dt FROM care_gap WHERE member_id = ? AND status = 'OPEN' ORDER BY measure", [member_id])
