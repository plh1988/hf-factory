"""Read/write access for API handlers. Goes only through the TableStore port (portable SQL, ? placeholders)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pyarrow as pa

from factory.adapters.ports import Stores


class Repository:
    """Read access for the API endpoints, through the TableStore port with portable SQL (DuckDB locally, AlloyDB in
    the cloud). Methods return plain dicts; `*_ref` lookups accept either the internal id or the business number.
    """
    def __init__(self, stores: Stores) -> None:
        self.tables = stores.tables

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

    # ---------------------------------------------------------------- writes and lookups for S2-S4 use cases
    def exists(self, entity: str) -> bool:
        return self.tables.exists(entity)

    def insert_row(self, entity: str, key: str, row: dict[str, Any]) -> None:
        """Upsert one row using the table's own schema (keeps types identical to generated rows)."""
        schema = self.tables.query(f'SELECT * FROM "{entity}" LIMIT 0').schema
        full = {f.name: row.get(f.name) for f in schema}
        self.tables.upsert(entity, pa.Table.from_pylist([full], schema=schema), key)

    def rows(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        return self._rows(sql, params or [])

    def one(self, sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
        return self._one(sql, params or [])

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
