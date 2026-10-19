"""Contract tests run by stage s8: four per enabled endpoint (happy path, negative/not found, no token, scenario case).

Samples are picked from the generated data by scenario tag; when a scaled-down dataset lacks a scenario, a
fallback negative case keeps the count at four. Write endpoints are exercised with dry_run=true, so s8 never
changes the validated dataset.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from factory import codes
from factory.adapters.ports import Stores
from factory.api_engine.errors import ErrorCode
from factory.contracts import ApiJob

NO_FAULTS = {"x-fault-injection": "off"}
BAD_TIN = "900000000"


class Suite:
    """Contract-test harness: an in-process TestClient against the tenant app, authenticated with the tenant's
    client credentials, with fault injection switched off for every call.

    Tests are registered first and run together. get/post/put return callables with the path and body bound
    immediately, so a test never sees a variable that was reassigned later.
    """
    def __init__(self, app: FastAPI, job: ApiJob, stores: Stores, as_of: str) -> None:
        self.client = TestClient(app)
        creds = json.loads(stores.blobs.get("secrets/api-client.json"))
        tok = self.client.post("/oauth/token", data={"grant_type": "client_credentials", **creds}).json()["access_token"]
        self.auth = {"Authorization": f"Bearer {tok}", **NO_FAULTS}
        self.base, self.stores, self.as_of = job.base_path, stores, as_of
        self.tests: list[tuple[str, str, Callable[[], Any], Callable[[Any], bool]]] = []

    def add(self, endpoint: str, name: str, call: Callable[[], Any], check: Callable[[Any], bool]) -> None:
        """Register a test: `call` makes the request, `check` decides pass/fail from the response."""
        self.tests.append((endpoint, name, call, check))

    def one(self, sql: str, params: list[Any] | None = None) -> dict | None:
        """First row of a sample query, or None when a table it reads was not generated (the test then uses a fallback)."""
        if not all(self.stores.tables.exists(t) for t in _tables(sql)):
            return None
        rows = self.stores.tables.query(sql + " LIMIT 1", params or []).to_pylist()
        return rows[0] if rows else None

    def prov(self, npi: str) -> dict:
        return self.one("SELECT npi, tin FROM provider WHERE npi = ?", [npi])

    def get(self, path: str, auth: bool = True):
        return lambda: self.client.get(self.base + path, headers=self.auth if auth else NO_FAULTS)

    def post(self, path: str, body: dict, auth: bool = True):
        return lambda: self.client.post(self.base + path, json=body, headers=self.auth if auth else NO_FAULTS)

    def put(self, path: str, body: dict, auth: bool = True):
        return lambda: self.client.put(self.base + path, json=body, headers=self.auth if auth else NO_FAULTS)

    def run(self) -> list[dict[str, Any]]:
        """Run every registered test; a failure records the API's error code (or the start of a non-JSON body)."""
        out = []
        for endpoint, name, call, check in self.tests:
            resp = call()
            try:
                ok = bool(check(resp))
            except Exception:  # a malformed body counts as a failure
                ok = False
            row = {"endpoint": endpoint, "test": name, "status": resp.status_code, "passed": ok}
            if not ok:
                try:
                    row["error"] = resp.json().get("error") or resp.json().get("detail")
                except Exception:  # non-JSON body
                    row["error"] = resp.text[:200]
            out.append(row)
        return out


def _tables(sql: str) -> list[str]:
    """Table names after FROM / JOIN in a sample query (to check they exist first)."""
    words = sql.replace(",", " ").split()
    return [words[i + 1] for i, w in enumerate(words) if w.upper() in ("FROM", "JOIN") and i + 1 < len(words) and words[i + 1][0].isalpha()]


def _status(code: int):
    return lambda r: r.status_code == code


def _error(code: ErrorCode):
    return lambda r: r.status_code == code.status and r.json().get("error") == code


ACTIVE_MEMBER = ("SELECT m.member_number, m.member_id FROM member_demographics m JOIN plan_enrollment e ON e.member_id = m.member_id "
                 "WHERE e.status = 'ACTIVE' AND e.term_dt IS NULL AND m.scenario_id IN ({tags}) ORDER BY m.member_number")


# ------------------------------------------------------------------ S1 use cases
def ct_pcs(t: Suite) -> None:
    ep = f"GET {t.base}/claims/{{claim_id}}/status"
    c = t.one("SELECT claim_number, billing_npi FROM claim_header WHERE status = 'PAID' AND claim_frequency_code = '1' ORDER BY claim_number")
    p = t.prov(c["billing_npi"])
    q = f"?npi={p['npi']}&tin={p['tin']}"
    t.add(ep, "paid claim returns status and payment", t.get(f"/claims/{c['claim_number']}/status{q}"),
          lambda r: r.status_code == 200 and r.json()["status"] == codes.ClaimStatus.PAID)
    t.add(ep, "unknown claim is 404", t.get(f"/claims/CLM-9999999-99/status{q}"), _status(404))
    t.add(ep, "no token is 401", t.get(f"/claims/{c['claim_number']}/status{q}", auth=False), _status(401))
    pend = t.one("SELECT claim_number, billing_npi FROM claim_header WHERE scenario_id = 'PCS-02' ORDER BY claim_number")
    if pend:
        pp = t.prov(pend["billing_npi"])
        t.add(ep, "PCS-02 pended claim lists what is needed", t.get(f"/claims/{pend['claim_number']}/status?npi={pp['npi']}&tin={pp['tin']}"),
              lambda r: r.status_code == 200 and r.json()["pend_reason"] == "RECORDS_REQUEST")
    else:
        t.add(ep, "PCS-04 wrong TIN is refused (403)", t.get(f"/claims/{c['claim_number']}/status?npi={p['npi']}&tin={BAD_TIN}"),
              _error(ErrorCode.VERIFICATION_FAILED))


def ct_pas(t: Suite) -> None:
    a = (t.one("SELECT auth_number, requesting_npi FROM auth_request WHERE scenario_id = 'PAS-01' ORDER BY auth_number")
         or t.one("SELECT auth_number, requesting_npi FROM auth_request ORDER BY auth_number"))
    ap = t.prov(a["requesting_npi"])
    q = f"?npi={ap['npi']}&tin={ap['tin']}"
    ep = f"GET {t.base}/authorizations/{{auth_id}}/status"
    t.add(ep, "status returns due time", t.get(f"/authorizations/{a['auth_number']}/status{q}"),
          lambda r: r.status_code == 200 and "decision_due_dt" in r.json())
    t.add(ep, "unknown auth is 404", t.get(f"/authorizations/AUTH-9999999-99/status{q}"), _status(404))
    t.add(ep, "no token is 401", t.get(f"/authorizations/{a['auth_number']}/status{q}", auth=False), _status(401))
    a2 = t.one("SELECT auth_number, requesting_npi FROM auth_request WHERE scenario_id = 'PAS-02' ORDER BY auth_number")
    if a2:
        a2p = t.prov(a2["requesting_npi"])
        t.add(ep, "PAS-02 urgent SNF shows discharge summary outstanding",
              t.get(f"/authorizations/{a2['auth_number']}/status?npi={a2p['npi']}&tin={a2p['tin']}"),
              lambda r: r.status_code == 200 and r.json()["urgency"] == "URGENT" and r.json()["clock_paused"])
    else:
        t.add(ep, "wrong TIN is refused (403)", t.get(f"/authorizations/{a['auth_number']}/status?npi={ap['npi']}&tin={BAD_TIN}"), _status(403))
    ep = f"POST {t.base}/authorizations/{{auth_id}}/documents"
    body = {"doc_type": "IMAGING", "document_ref": "doc-test", "npi": ap["npi"], "tin": ap["tin"]}
    t.add(ep, "document not requested is 409", t.post(f"/authorizations/{a['auth_number']}/documents", body), _status(409))
    t.add(ep, "unknown auth is 404", t.post("/authorizations/AUTH-9999999-99/documents", body), _status(404))
    t.add(ep, "no token is 401", t.post(f"/authorizations/{a['auth_number']}/documents", body, auth=False), _status(401))
    t.add(ep, "invalid doc type is 422", t.post(f"/authorizations/{a['auth_number']}/documents", {**body, "doc_type": "SELFIE"}), _status(422))


def ct_pev(t: Suite) -> None:
    m = t.one(ACTIVE_MEMBER.format(tags="'PEV-01', 'BASE'")) or t.one(ACTIVE_MEMBER.format(tags="'PEV-01', 'BASE', 'PEV-04'"))
    op = t.one("SELECT npi, tin FROM provider WHERE status = 'ACTIVE' ORDER BY npi")
    req = {"npi": op["npi"], "tin": op["tin"], "date_of_service": t.as_of, "member_id": m["member_number"]}
    ep = f"POST {t.base}/eligibility/verify"
    t.add(ep, "active member is eligible with cost share", t.post("/eligibility/verify", req),
          lambda r: r.status_code == 200 and r.json()["eligible"] and r.json()["cost_share"] is not None)
    t.add(ep, "unknown member is 404", t.post("/eligibility/verify", {**req, "member_id": "MBR-9999999"}), _status(404))
    t.add(ep, "no token is 401", t.post("/eligibility/verify", req, auth=False), _status(401))
    m2 = t.one("SELECT member_number FROM member_demographics WHERE scenario_id = 'PEV-02' ORDER BY member_number")
    if m2:
        t.add(ep, "PEV-02 retro termination: no coverage today", t.post("/eligibility/verify", {**req, "member_id": m2["member_number"]}),
              lambda r: r.status_code == 200 and not r.json()["eligible"])
    else:
        t.add(ep, "wrong TIN is 403", t.post("/eligibility/verify", {**req, "tin": BAD_TIN}), _status(403))


# ------------------------------------------------------------------ S2 / S4 member use cases
def ct_mbn(t: Suite) -> None:
    m = t.one(ACTIVE_MEMBER.format(tags="'MBN-01', 'BASE', 'PEV-01', 'MPS-01', 'MPC-01', 'MAS-01'"))
    mid = m["member_number"]
    ep = f"GET {t.base}/members/{{member_id}}/benefits"
    t.add(ep, "benefits with cost share and plan", t.get(f"/members/{mid}/benefits"),
          lambda r: r.status_code == 200 and "copay_specialist" in r.json()["cost_share"])
    t.add(ep, "unknown member is 404", t.get("/members/MBR-9999999/benefits"), _status(404))
    t.add(ep, "no token is 401", t.get(f"/members/{mid}/benefits", auth=False), _status(401))
    cg = t.one("SELECT m.member_number, f.relationship_id FROM family_relationship f JOIN member_demographics m ON m.member_id = f.member_id "
               "WHERE f.caregiver_flag AND f.scenario_id = 'MPC-04' ORDER BY f.relationship_id")
    if cg:
        t.add(ep, "caregiver without BENEFITS scope is refused (R-06)",
              t.get(f"/members/{cg['member_number']}/benefits?on_behalf_of={cg['relationship_id']}"), _error(ErrorCode.SCOPE_NOT_AUTHORIZED))
    else:
        t.add(ep, "unknown caregiver is refused", t.get(f"/members/{mid}/benefits?on_behalf_of=nobody"), _status(403))
    ep = f"GET {t.base}/members/{{member_id}}/accumulators"
    t.add(ep, "in- and out-of-network progress", t.get(f"/members/{mid}/accumulators"),
          lambda r: r.status_code == 200 and "deductible_remaining" in r.json()["in_network"])
    t.add(ep, "unknown member is 404", t.get("/members/MBR-9999999/accumulators"), _status(404))
    t.add(ep, "no token is 401", t.get(f"/members/{mid}/accumulators", auth=False), _status(401))
    t.add(ep, "a year without claims shows nothing met", t.get(f"/members/{mid}/accumulators?plan_year=1999"),
          lambda r: r.status_code == 200 and r.json()["in_network"]["oop_met"] == "0.00")


def ct_mci(t: Suite) -> None:
    c = t.one("SELECT h.claim_number, m.member_number FROM claim_header h JOIN member_demographics m ON m.member_id = h.member_id "
              "WHERE h.status = 'PAID' AND h.scenario_id IN ('MCI-01', 'BASE', 'PCS-01') ORDER BY h.claim_number")
    mid = c["member_number"]
    ep = f"GET {t.base}/members/{{member_id}}/claims"
    t.add(ep, "member claims list", t.get(f"/members/{mid}/claims"), lambda r: r.status_code == 200 and len(r.json()["claims"]) > 0)
    t.add(ep, "unknown member is 404", t.get("/members/MBR-9999999/claims"), _status(404))
    t.add(ep, "no token is 401", t.get(f"/members/{mid}/claims", auth=False), _status(401))
    d = t.one("SELECT m.member_number FROM claim_header h JOIN member_demographics m ON m.member_id = h.member_id "
              "WHERE h.scenario_id = 'MCI-02' ORDER BY h.claim_number")
    if d:
        t.add(ep, "MCI-02 denial CO-197: provider liable", t.get(f"/members/{d['member_number']}/claims?status=denied"),
              lambda r: r.status_code == 200 and any(x["denial"] and x["denial"]["carc"] == "CO-197" and x["denial"]["liable_party"] == "PROVIDER"
                                                     for x in r.json()["claims"]))
    else:
        t.add(ep, "status filter returns only that status", t.get(f"/members/{mid}/claims?status=paid"),
              lambda r: r.status_code == 200 and all(x["status"] == codes.ClaimStatus.PAID for x in r.json()["claims"]))
    ep = f"GET {t.base}/claims/{{claim_id}}"
    t.add(ep, "claim detail with EOB math", t.get(f"/claims/{c['claim_number']}?member_id={mid}"),
          lambda r: r.status_code == 200 and "You owe" in r.json()["member_owes_explained"] and r.json()["lines"])
    other = t.one("SELECT member_number FROM member_demographics WHERE member_number <> ? ORDER BY member_number", [mid])
    t.add(ep, "another member's claim is 404 (minimum necessary)", t.get(f"/claims/{c['claim_number']}?member_id={other['member_number']}"),
          _status(404))
    t.add(ep, "no token is 401", t.get(f"/claims/{c['claim_number']}?member_id={mid}", auth=False), _status(401))
    v = t.one("SELECT h.claim_number, m.member_number FROM claim_header h JOIN member_demographics m ON m.member_id = h.member_id "
              "WHERE h.scenario_id = 'MCI-04' AND h.status = 'ADJUSTED' ORDER BY h.claim_number")
    if v:
        t.add(ep, "MCI-04 original shows its replacement", t.get(f"/claims/{v['claim_number']}?member_id={v['member_number']}"),
              lambda r: r.status_code == 200 and r.json()["versions"]["replaced_by"] is not None)
    else:
        t.add(ep, "member_id is required (422)", t.get(f"/claims/{c['claim_number']}"), _status(422))


def ct_mps(t: Suite) -> None:
    m = t.one(ACTIVE_MEMBER.format(tags="'MPS-01', 'BASE', 'MBN-01', 'MPC-01', 'MAS-01', 'PEV-01'"))
    ep = f"GET {t.base}/providers/search"
    t.add(ep, "primary care near a member", t.get(f"/providers/search?specialty=primary care&member_id={m['member_number']}"),
          lambda r: r.status_code == 200 and "results_within_radius" in r.json())
    t.add(ep, "unknown specialty is 422", t.get(f"/providers/search?specialty=astrology&member_id={m['member_number']}"), _status(422))
    t.add(ep, "no token is 401", t.get(f"/providers/search?specialty=cardiology&lat=30.27&lon=-97.74", auth=False), _status(401))
    rural = t.one("SELECT member_number FROM member_demographics WHERE scenario_id = 'MPS-02' ORDER BY member_number")
    if rural:
        t.add(ep, "MPS-02 Spanish pediatrician, rural: widens or finds within standard",
              t.get(f"/providers/search?specialty=pediatrics&language=es&member_id={rural['member_number']}"),
              lambda r: r.status_code == 200 and r.json()["access_standard"]["geo_type"] == codes.GeoType.RURAL
              and (r.json()["results_within_radius"] > 0 or r.json()["access_gap_logged"]))
    else:
        t.add(ep, "location is required (422)", t.get("/providers/search?specialty=cardiology"), _status(422))


def _haversine(a: float, b: float, c: float, d: float) -> float:
    p1, p2 = math.radians(a), math.radians(c)
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(d - b) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(x))


def ct_mpc(t: Suite) -> None:
    ep = f"PUT {t.base}/members/{{member_id}}/pcp"
    m = t.one("SELECT m.member_number, m.member_id, m.lat, m.lon, m.geo_type, e.plan_id, a.pcp_npi FROM member_demographics m "
              "JOIN plan_enrollment e ON e.member_id = m.member_id AND e.status = 'ACTIVE' AND e.term_dt IS NULL "
              "JOIN pcp_assignment a ON a.member_id = m.member_id AND a.status = 'ACTIVE' "
              "WHERE m.scenario_id IN ('MPC-01', 'BASE', 'MBN-01', 'MPS-01', 'MAS-01', 'PEV-01') ORDER BY m.member_number")
    new_pcp = None
    if m:
        cands = t.stores.tables.query(
            "SELECT p.npi, l.lat, l.lon FROM provider p JOIN network_participation n ON n.npi = p.npi AND n.plan_id = ? "
            "JOIN practice_location l ON l.npi = p.npi WHERE p.provider_type = 'PCP' AND p.status = 'ACTIVE' AND n.status = 'PARTICIPATING' "
            "AND n.panel_status <> 'CLOSED' AND p.npi <> ? ORDER BY p.npi", [m["plan_id"], m["pcp_npi"]]).to_pylist()
        std = t.one("SELECT max_distance_miles FROM access_standard WHERE plan_id = ? AND provider_type_or_specialty = 'PCP' AND geo_type = ?",
                    [m["plan_id"], m["geo_type"]])
        limit = std["max_distance_miles"] if std else 1e9
        new_pcp = next((x["npi"] for x in cands if _haversine(m["lat"], m["lon"], x["lat"], x["lon"]) <= limit), None)
    if m and new_pcp:
        t.add(ep, "valid PCP change validates (dry run)", t.put(f"/members/{m['member_number']}/pcp?dry_run=true", {"npi": new_pcp}),
              lambda r: r.status_code == 200 and r.json()["status"] == "VALIDATED")
    elif m:
        t.add(ep, "current PCP again is 409", t.put(f"/members/{m['member_number']}/pcp?dry_run=true", {"npi": m["pcp_npi"]}), _status(409))
    mid = m["member_number"] if m else "MBR-0000001"
    t.add(ep, "unknown member is 404", t.put("/members/MBR-9999999/pcp?dry_run=true", {"npi": "1234567890"}), _status(404))
    t.add(ep, "no token is 401", t.put(f"/members/{mid}/pcp?dry_run=true", {"npi": "1234567890"}, auth=False), _status(401))
    cg = t.one("SELECT m.member_number, f.relationship_id FROM family_relationship f JOIN member_demographics m ON m.member_id = f.member_id "
               "WHERE f.caregiver_flag AND f.scenario_id = 'MPC-04' ORDER BY f.relationship_id")
    if cg:
        t.add(ep, "MPC-04 caregiver without PCP_CHANGE scope is refused",
              t.put(f"/members/{cg['member_number']}/pcp?dry_run=true&on_behalf_of={cg['relationship_id']}", {"npi": new_pcp or "1234567890"}),
              _error(ErrorCode.SCOPE_NOT_AUTHORIZED))
    else:
        spec = t.one("SELECT npi FROM provider WHERE provider_type = 'SPECIALIST' ORDER BY npi")
        t.add(ep, "a specialist cannot be a PCP (409)", t.put(f"/members/{mid}/pcp?dry_run=true", {"npi": spec["npi"]}), _status(409))
    if len([x for x in t.tests if x[0] == ep]) < 4:
        t.add(ep, "invalid NPI is 422", t.put(f"/members/{mid}/pcp?dry_run=true", {"npi": "12"}), _status(422))


def ct_mas(t: Suite) -> None:
    s = t.one("SELECT s.npi FROM availability_schedule s JOIN provider p ON p.npi = s.npi WHERE p.provider_type = 'PCP' "
              "AND s.scenario_id = 'BASE' ORDER BY s.npi")
    ep = f"GET {t.base}/providers/{{npi}}/availability"
    t.add(ep, "open slots for a PCP", t.get(f"/providers/{s['npi']}/availability"), lambda r: r.status_code == 200 and r.json()["slots"])
    t.add(ep, "unknown provider is 404", t.get("/providers/1999999999/availability"), _status(404))
    t.add(ep, "no token is 401", t.get(f"/providers/{s['npi']}/availability", auth=False), _status(401))
    stale = t.one("SELECT npi FROM availability_schedule WHERE scenario_id = 'DQ-STALE-SCHEDULE' ORDER BY npi")
    if stale:
        t.add(ep, "MAS-04 stale schedule is flagged and skipped", t.get(f"/providers/{stale['npi']}/availability"),
              lambda r: r.status_code == 200 and r.json()["schedule_flag"] is not None)
    else:
        t.add(ep, "unknown member is 404", t.get(f"/providers/{s['npi']}/availability?member_id=MBR-9999999"), _status(404))
    ep = f"POST {t.base}/appointments"
    m = t.one("SELECT m.member_number, a.pcp_npi FROM member_demographics m JOIN pcp_assignment a ON a.member_id = m.member_id AND a.status = 'ACTIVE' "
              "JOIN plan_enrollment e ON e.member_id = m.member_id AND e.status = 'ACTIVE' AND e.term_dt IS NULL "
              "WHERE EXISTS (SELECT 1 FROM availability_schedule s WHERE s.npi = a.pcp_npi AND s.scenario_id = 'BASE') ORDER BY m.member_number")
    booked = False
    if m:
        slots = t.client.get(f"{t.base}/providers/{m['pcp_npi']}/availability", headers=t.auth).json().get("slots") or []
        if slots:
            body = {"member_id": m["member_number"], "npi": m["pcp_npi"], "location_id": slots[0]["location_id"],
                    "start": slots[0]["start"], "visit_type": "SICK"}
            t.add(ep, "book an open slot (dry run)", t.post("/appointments?dry_run=true", body),
                  lambda r: r.status_code == 201 and r.json()["status"] == "VALIDATED")
            t.add(ep, "a time that is not an open slot is 409",
                  t.post("/appointments?dry_run=true", {**body, "start": "2000-01-01T09:00:00"}), _error(ErrorCode.SLOT_UNAVAILABLE))
            booked = True
    body = {"member_id": m["member_number"] if m else "MBR-0000001", "npi": "1999999999", "location_id": "x",
            "start": "2030-01-01T09:00:00", "visit_type": "SICK"}
    t.add(ep, "no token is 401", t.post("/appointments?dry_run=true", body, auth=False), _status(401))
    t.add(ep, "invalid visit type is 422", t.post("/appointments?dry_run=true", {**body, "visit_type": "SURGERY"}), _status(422))
    if not booked:
        t.add(ep, "unknown member is 404", t.post("/appointments?dry_run=true", {**body, "member_id": "MBR-9999999"}), _status(404))
        t.add(ep, "non-participating provider is 409", t.post("/appointments?dry_run=true", body), _status(409))


# ------------------------------------------------------------------ S3 provider use cases
def ct_pap(t: Suite) -> None:
    ep = f"POST {t.base}/claims/{{claim_id}}/appeals"
    c = t.one("SELECT h.claim_number, h.billing_npi FROM claim_header h JOIN claim_denial d ON d.claim_id = h.claim_id "
              "WHERE h.status = 'DENIED' AND d.appealable AND d.appeal_deadline_dt >= ? "
              "AND NOT EXISTS (SELECT 1 FROM appeal_record a WHERE a.claim_id = h.claim_id) ORDER BY h.claim_number", [t.as_of])
    open_ = t.one("SELECT h.claim_number, h.billing_npi, a.appeal_number FROM appeal_record a JOIN claim_header h ON h.claim_id = a.claim_id "
                  "WHERE a.status <> 'DECIDED' ORDER BY a.appeal_number")
    any_c = c or open_ or t.one("SELECT claim_number, billing_npi FROM claim_header ORDER BY claim_number")
    p = t.prov(any_c["billing_npi"])
    body = {"level": "L1", "reason": codes.DenialCategory.MED_NECESSITY, "documents": ["doc-1"], "npi": p["npi"], "tin": p["tin"]}
    if c:
        t.add(ep, "appeal an appealable denial (dry run)", t.post(f"/claims/{c['claim_number']}/appeals?dry_run=true", body),
              lambda r: r.status_code == 201 and r.json()["status"] == "VALIDATED")
    t.add(ep, "unknown claim is 404", t.post("/claims/CLM-9999999-99/appeals?dry_run=true", body), _status(404))
    t.add(ep, "no token is 401", t.post(f"/claims/{any_c['claim_number']}/appeals?dry_run=true", body, auth=False), _status(401))
    if open_:
        po = t.prov(open_["billing_npi"])
        t.add(ep, "PAP-02 an open appeal blocks a second one (409)",
              t.post(f"/claims/{open_['claim_number']}/appeals?dry_run=true", {**body, "npi": po["npi"], "tin": po["tin"]}),
              _error(ErrorCode.APPEAL_EXISTS))
    paid = t.one("SELECT claim_number, billing_npi FROM claim_header WHERE status = 'PAID' ORDER BY claim_number")
    while len([x for x in t.tests if x[0] == ep]) < 4:
        pp = t.prov(paid["billing_npi"])
        t.add(ep, "a paid claim cannot be appealed (409)",
              t.post(f"/claims/{paid['claim_number']}/appeals?dry_run=true", {**body, "npi": pp["npi"], "tin": pp["tin"]}), _error(ErrorCode.NOT_DENIED))
    ep = f"GET {t.base}/appeals/{{appeal_id}}"
    a = t.one("SELECT a.appeal_number, h.billing_npi FROM appeal_record a JOIN claim_header h ON h.claim_id = a.claim_id "
              "WHERE a.scenario_id IN ('PAP-02', 'BASE') ORDER BY a.appeal_number")
    o = t.one("SELECT a.appeal_number, h.billing_npi FROM appeal_record a JOIN claim_header h ON h.claim_id = a.claim_id "
              "WHERE a.outcome = 'OVERTURNED' AND a.scenario_id = 'PAP-03' ORDER BY a.appeal_number")
    t.add(ep, "unknown appeal is 404", t.get(f"/appeals/APL-NOPE?npi={p['npi']}&tin={p['tin']}"), _status(404))
    if a:
        pa_ = t.prov(a["billing_npi"])
        q = f"?npi={pa_['npi']}&tin={pa_['tin']}"
        t.add(ep, "appeal status", t.get(f"/appeals/{a['appeal_number']}{q}"), lambda r: r.status_code == 200 and r.json()["status"])
        t.add(ep, "no token is 401", t.get(f"/appeals/{a['appeal_number']}{q}", auth=False), _status(401))
        if o:
            po = t.prov(o["billing_npi"])
            t.add(ep, "PAP-03 overturned: awaiting reprocessing", t.get(f"/appeals/{o['appeal_number']}?npi={po['npi']}&tin={po['tin']}"),
                  lambda r: r.status_code == 200 and r.json()["reprocessing"]["status"] == "AWAITING_REPROCESSING")
        else:
            t.add(ep, "wrong TIN is 403", t.get(f"/appeals/{a['appeal_number']}?npi={pa_['npi']}&tin={BAD_TIN}"), _status(403))
    while len([x for x in t.tests if x[0] == ep]) < 4:
        t.add(ep, "missing NPI is 422", t.get("/appeals/APL-NOPE"), _status(422))


def ct_ppi(t: Suite) -> None:
    ep = f"GET {t.base}/claims/{{claim_id}}/payments"
    c = t.one("SELECT claim_number, billing_npi FROM claim_header WHERE status = 'PAID' AND scenario_id IN ('PPI-01', 'PPI-02', 'BASE', 'PCS-01') "
              "ORDER BY claim_number")
    p = t.prov(c["billing_npi"])
    q = f"?npi={p['npi']}&tin={p['tin']}"
    t.add(ep, "payment with trace and adjustments", t.get(f"/claims/{c['claim_number']}/payments{q}"),
          lambda r: r.status_code == 200 and r.json()["payments"] and "member_share_PR" in r.json()["adjustments"])
    t.add(ep, "unknown claim is 404", t.get(f"/claims/CLM-9999999-99/payments{q}"), _status(404))
    t.add(ep, "no token is 401", t.get(f"/claims/{c['claim_number']}/payments{q}", auth=False), _status(401))
    w = t.one("SELECT claim_number, billing_npi FROM claim_header WHERE scenario_id = 'PPI-04' AND status = 'PAID' ORDER BY claim_number")
    if w:
        pw = t.prov(w["billing_npi"])
        t.add(ep, "PPI-04 payment to another TIN is flagged, not changed", t.get(f"/claims/{w['claim_number']}/payments?npi={pw['npi']}&tin={pw['tin']}"),
              lambda r: r.status_code == 200 and r.json()["warnings"] and not r.json()["payments"][0]["payee_matches_billing_tin"])
    else:
        t.add(ep, "wrong TIN is 403", t.get(f"/claims/{c['claim_number']}/payments?npi={p['npi']}&tin={BAD_TIN}"), _status(403))


# ------------------------------------------------------------------ common
def ct_common(t: Suite) -> None:
    op = t.one("SELECT npi, tin FROM provider WHERE status = 'ACTIVE' ORDER BY npi")
    ep = f"POST {t.base}/identity/verify"
    t.add(ep, "provider NPI + TIN verifies", t.post("/identity/verify", {"caller_type": "PROVIDER", **op}),
          lambda r: r.status_code == 200 and r.json()["verified"])
    t.add(ep, "wrong TIN does not verify", t.post("/identity/verify", {"caller_type": "PROVIDER", "npi": op["npi"], "tin": BAD_TIN}),
          lambda r: r.status_code == 200 and not r.json()["verified"])
    t.add(ep, "no token is 401", t.post("/identity/verify", {"caller_type": "PROVIDER", **op}, auth=False), _status(401))
    t.add(ep, "bad caller type is 422", t.post("/identity/verify", {"caller_type": "ROBOT"}), _status(422))
    ep = f"GET {t.base}/knowledge/search"
    t.add(ep, "search returns cited chunks", t.get("/knowledge/search?q=prior authorization urgent decision"),
          lambda r: r.status_code == 200 and len(r.json()["results"]) > 0)
    t.add(ep, "audience filter is applied", t.get("/knowledge/search?q=deductible&audience=MEMBER"),
          lambda r: r.status_code == 200 and all(h["audience"] == "MEMBER" for h in r.json()["results"]))
    t.add(ep, "no token is 401", t.get("/knowledge/search?q=deductible", auth=False), _status(401))
    t.add(ep, "query too short is 422", t.get("/knowledge/search?q=a"), _status(422))


SUITES = {"PCS": ct_pcs, "PAS": ct_pas, "PEV": ct_pev, "MBN": ct_mbn, "MCI": ct_mci, "MPS": ct_mps, "MPC": ct_mpc, "MAS": ct_mas,
          "PAP": ct_pap, "PPI": ct_ppi}


def contract_tests(app: FastAPI, job: ApiJob, stores: Stores, as_of: str) -> list[dict[str, Any]]:
    """Register the suites of the payload's use cases plus the common ones (token, health, knowledge) and run them."""
    suite = Suite(app, job, stores, as_of)
    for uc in job.use_cases:
        SUITES[uc](suite)
    ct_common(suite)
    return suite.run()
