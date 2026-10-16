"""Build Postman collections for scenarios S1-S4 from the spec payload fixtures.

Run from the repo root:  python scripts/make_postman.py
Changes to the spec payloads (all listed in each request's description):
  * s4 gets the s5 scenario_mix, so member scenarios (PEV/MBN/MPS/MPC/MAS) are generated;
  * S2 and S3 volumes are scaled down to the in-memory profile limits (claims per member kept).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
OUT = ROOT / "postman"

SCENARIOS = {
    "s1": ("S1 provider-facing demo (Medicare Advantage)", None),
    "s2": ("S2 Medicaid member pilot (scaled to 20k members)", {"providers": 216, "members": 20000, "claims": 120000, "authorizations": 6000}),
    "s3": ("S3 claims denial and appeals load test (scaled to 15k members)", {"providers": 162, "members": 15000, "claims": 100000, "authorizations": 5000}),
    "s4": ("S4 sandbox (Commercial)", None),
}
STAGES = {"s3": "generate_reference_data", "s4": "generate_members", "s5": "generate_claims_auth",
          "s6": "generate_care_knowledge", "s7": "validate_and_scan"}


def payloads(name: str) -> tuple[dict[str, dict], list[str]]:
    src = FIX / f"{name}_jobs"
    p = {st: json.loads((src / f"{st}.json").read_text(encoding="utf-8")) for st in [*STAGES, "s8"]}
    changes = []
    scale = SCENARIOS[name][1]
    if scale:
        p["s3"]["volumes"]["providers"] = scale["providers"]
        p["s4"]["volumes"]["members"] = scale["members"]
        p["s5"]["volumes"].update(claims=scale["claims"], authorizations=scale["authorizations"])
        changes.append(f"volumes scaled to {scale['members']:,} members / {scale['claims']:,} claims (spec values exceed the in-memory limits)")
    p["s4"]["scenario_mix"] = copy.deepcopy(p["s5"]["scenario_mix"])
    changes.append("s4 carries the s5 scenario_mix so member scenarios are generated")
    return p, changes


def request(name: str, method: str, path: str, body: dict | None = None, tests: list[str] | None = None,
            headers: list[dict] | None = None, form: dict | None = None, desc: str = "") -> dict:
    url_path, _, query = path.partition("?")
    url = {"raw": "{{baseUrl}}" + path, "host": ["{{baseUrl}}"], "path": url_path.strip("/").split("/")}
    if query:
        url["query"] = [{"key": k, "value": v} for k, v in (x.split("=", 1) for x in query.split("&"))]
    req = {"method": method, "header": [{"key": "Content-Type", "value": "application/json"}] if body is not None else [],
           "url": url, "description": desc}
    req["header"] += headers or []
    if body is not None:
        req["body"] = {"mode": "raw", "raw": json.dumps(body, indent=2), "options": {"raw": {"language": "json"}}}
    if form:
        req["body"] = {"mode": "urlencoded", "urlencoded": [{"key": k, "value": v} for k, v in form.items()]}
    item = {"name": name, "request": req}
    if tests:
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}]
    return item


def ok(status: int) -> list[str]:
    return [f"pm.test('HTTP {status}', () => pm.response.to.have.status({status}));"]


def collection(name: str) -> dict:
    title = SCENARIOS[name][0]
    p, changes = payloads(name)
    tenant = p["s3"]["consumes"]["alloydb"][len("alloy-"):]
    note = "Changes from the spec payload: " + "; ".join(changes) + "."
    data = [request(f"{st} {job}", "POST", f"/factory/data-engine/{st}", p[st],
                    ok(200) + ["pm.test('COMPLETED', () => pm.expect(pm.response.json().status).to.eql('COMPLETED'));"]
                    + (["pm.test('gate passed', () => pm.expect(pm.response.json().gate).to.eql('data_quality_gate PASSED'));"] if st == "s7" else []),
                    desc=(note if st in ("s3", "s4", "s5") else "Spec payload, unchanged.") + " Run in order s3 -> s7.")
            for st, job in STAGES.items()]
    s8 = request("s8 api_generate_and_deploy", "POST", "/factory/api-engine/s8", p["s8"],
                 ok(200) + ["pm.test('DEPLOYED', () => pm.expect(pm.response.json().status).to.eql('DEPLOYED'));"],
                 desc="Spec payload, unchanged. Run after s7; the tenant API then answers at {{baseUrl}}/v1 and {{baseUrl}}/docs.")
    auth = {"key": "Authorization", "value": "Bearer {{token}}"}
    nofault = {"key": "x-fault-injection", "value": "off"}
    tenant_api = [
        request("Get local credentials", "GET", f"/factory/runs/{tenant}/credentials", tests=ok(200) + [
            "const b = pm.response.json();", "pm.collectionVariables.set('clientId', b.client_id);",
            "pm.collectionVariables.set('clientSecret', b.client_secret);"]),
        request("Get token", "POST", "/oauth/token", form={"grant_type": "client_credentials", "client_id": "{{clientId}}",
                                                            "client_secret": "{{clientSecret}}"},
                tests=ok(200) + ["pm.collectionVariables.set('token', pm.response.json().access_token);"]),
        request("Search knowledge", "GET", "/v1/knowledge/search?q=prior authorization&k=3", headers=[auth, nofault], tests=ok(200)),
    ]
    runs = [
        request("Run status", "GET", f"/factory/runs/{tenant}", tests=ok(200)),
        request("Manifest", "GET", f"/factory/runs/{tenant}/manifest", tests=ok(200)),
        request("Rule report", "GET", f"/factory/runs/{tenant}/rule-report", tests=ok(200)),
        request("Peek members", "GET", f"/factory/runs/{tenant}/tables/member_demographics?limit=5", tests=ok(200)),
        request("Reset run (start over)", "DELETE", f"/factory/runs/{tenant}", tests=ok(200)),
    ]
    return {"info": {"name": f"HF Factory - {title}",
                     "description": f"Data Engine s3-s7 then API Engine s8 for tenant {tenant}. Start the server with `python -m factory serve`. {note}",
                     "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
            "variable": [{"key": "baseUrl", "value": "http://127.0.0.1:8080"}, {"key": "clientId", "value": ""},
                         {"key": "clientSecret", "value": ""}, {"key": "token", "value": ""}],
            "item": [{"name": "1 Data engine (s3-s7)", "item": data}, {"name": "2 API engine (s8)", "item": [s8]},
                     {"name": "3 Tenant API (after s8)", "item": tenant_api}, {"name": "4 Run status", "item": runs}]}


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name in SCENARIOS:
        path = OUT / f"hf-factory-{name}.postman_collection.json"
        path.write_text(json.dumps(collection(name), indent=2), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
