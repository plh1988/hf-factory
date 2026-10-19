# Demo Guide — Data Engine + API Engine (local)

A 20–25 minute live demo of the factory on one laptop. Everything runs in memory with the `memory-offline` profile:
no cloud account and no internet needed. The data is deterministic, so every ID in this guide is the same on any machine
that runs these exact payloads.

**The story:** a customer request becomes a working, tested healthcare API in about a minute, driven by the exact job
payloads in the architecture spec, with byte-identical data every time and one switch to move to Google Cloud.

Contents: [1 Pitch](#1-the-60-second-pitch) · [2 Setup](#2-one-time-setup) · [3 Before the audience](#3-before-the-audience-arrives)
· [4 Act 1 Context](#4-act-1--context-2-min) · [5 Act 2 Data Engine](#5-act-2--data-engine-s3s7-5-min)
· [6 Act 3 API Engine](#6-act-3--api-engine-s8-2-min) · [7 Act 4 Use the API](#7-act-4--use-the-api-like-an-agent-6-min)
· [8 Act 5 Member-facing (S2)](#8-act-5--member-facing-api-s2-optional-4-min) · [9 Act 6 Appeals and payments (S3)](#9-act-6--appeals-and-payments-s3-optional-3-min)
· [10 Act 7 Trust](#10-act-7--trust-repeatability-and-guardrails-3-min) · [11 Act 8 Cloud](#11-act-8--path-to-the-cloud-2-min)
· [12 Q&A](#12-likely-questions) · [13 Troubleshooting](#13-troubleshooting) · [Appendix A: S1 payloads](#appendix-a--s1-request-bodies)

---

## 1. The 60-second pitch

> The factory turns a customer request into a running environment. My part is two engines. The **Data Engine**
> takes a stage job payload and generates a fully synthetic, rule-checked healthcare dataset (members, providers,
> claims, authorizations, care records and knowledge documents) and checks it against 100+ rules before anything is
> released. The **API Engine** then builds a secured REST API over that data, tests every endpoint, and publishes
> tool definitions so voice and chat agents can call it. Everything is driven by the exact JSON payloads in the spec,
> the same payload always produces byte-identical data, and one environment variable moves it to Google Cloud.

## 2. One-time setup

Run in the `hf-factory` folder (Windows, Git Bash or PowerShell):

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -e ".[dev]"
```

```bash
copy .env.example .env
```

`.env` must contain `FACTORY_PROFILE=memory-offline`.

Import into Postman (File → Import):

| Collection | Scenario | Tenant |
|---|---|---|
| `postman/hf-factory-s1.postman_collection.json` | S1 provider-facing demo, Medicare Advantage, 50k members | `ma-demo-01` |
| `postman/hf-factory-s2.postman_collection.json` | S2 Medicaid member pilot (scaled to 20k members) | `mdcd-pilot-02` |
| `postman/hf-factory-s3.postman_collection.json` | S3 claims denials and appeals (scaled to 15k members) | `perf-claims-03` |

Each collection has a `baseUrl` variable (`http://127.0.0.1:8080`) and four folders: **1 Data engine (s3–s7)**,
**2 API engine (s8)**, **3 Tenant API (after s8)** (fetches credentials and a token automatically) and **4 Run status**.

## 3. Before the audience arrives

1. Start the server in a terminal you can show:

   ```bash
   .venv/Scripts/python -m factory serve
   ```

   It prints `control plane: http://127.0.0.1:8080/factory/docs`. Leave it running.
2. Open browser tabs: `docs/architecture.svg` (open the file), http://127.0.0.1:8080/factory/docs.
3. Dry run: in Postman run the S1 folders **1** and **2** once (Runner or one by one), check s8 says `DEPLOYED`, then
   run **4 Run status → Reset run (start over)** (`DELETE /factory/runs/ma-demo-01`).
4. Keep this guide open; copy IDs from the tables below.

---

## 4. Act 1 — Context (2 min)

Show `docs/architecture.svg` and walk top to bottom:

- **Caller → REST control plane:** "Postman stands in for the Orchestrator. Each stage is one REST call with the
  spec's payload."
- **Data Engine (pink), s3 → s7:** reference data, members, claims and authorizations, care and knowledge, validation.
- **API Engine (teal), s8:** builds and tests the API, then the tenant API goes live on the same server.
- **Ports and adapters:** "Green is what runs today on this laptop, blue is the same code pointed at GCP."

## 5. Act 2 — Data Engine, s3–s7 (5 min)

In Postman, collection **S1**, folder **1 Data engine**. Send each request in order. Method `POST`, header
`Content-Type: application/json`, bodies in [Appendix A](#appendix-a--s1-request-bodies).

| Step | Endpoint | Point at in the response | Say |
|---|---|---|---|
| 1 | `POST /factory/data-engine/s3` | `status: COMPLETED`, `rule_checks`, `notes` | "The spec's s3 payload has no plan types; the engine says exactly how it filled that gap." |
| 2 | `POST /factory/data-engine/s4` | `entities[]` (about 400k rows in ~3 s) | "50,000 members with enrollments, PCPs, caregivers. Every row is deterministic." |
| 3 | `POST /factory/data-engine/s5` | `rule_checks` all `PASS`, `notes: history_months inferred = 2` | "Claims, denials, appeals and authorizations. Money math and dates are checked as they are generated." |
| 4 | `POST /factory/data-engine/s6` | knowledge entities, `notes` about the LLM | "Plan documents, policies and FAQs, written by the LLM through a cache, then chunked and indexed for search." |
| 5 | `POST /factory/data-engine/s7` | `gate: data_quality_gate PASSED`, `validation.passed = rules_checked` | "Over a hundred checks. Nothing reaches agents unless all of them pass." |

Optional extras while on Act 2:

- `GET http://127.0.0.1:8080/factory/runs/ma-demo-01` — stage status and the run settings built from the payloads.
- `GET http://127.0.0.1:8080/factory/runs/ma-demo-01/tables/claim_header?scenario_id=PCS-02&limit=3` — peek at rows.
- `GET http://127.0.0.1:8080/factory/runs/ma-demo-01/analytics/claims-summary` — claims by status, denials by category.

## 6. Act 3 — API Engine, s8 (2 min)

Folder **2 API engine** → `POST /factory/api-engine/s8` (body in Appendix A).

Point at: `status: DEPLOYED`, `contract_tests: {"total": 24, "passed": 24}`, `endpoint_count: 6`, `gateway_url`,
`tool_manifest_uri`.

Say: "It built an API for exactly the three use cases in the payload (claim status, authorization status, eligibility),
tested every endpoint four ways against the real data, and published the tool definitions agents use. Locally the API
now runs on this same server; in the cloud this step deploys a Cloud Run service behind API Gateway."

## 7. Act 4 — Use the API like an agent (6 min)

### 7.1 Get a token

Postman folder **3 Tenant API**:

1. **Get local credentials** — `GET {{baseUrl}}/factory/runs/ma-demo-01/credentials`. Saves `clientId` and `clientSecret`.
2. **Get token** — `POST {{baseUrl}}/oauth/token`, body `x-www-form-urlencoded`:

   | key | value |
   |---|---|
   | `grant_type` | `client_credentials` |
   | `client_id` | `{{clientId}}` |
   | `client_secret` | `{{clientSecret}}` |

   Saves `token`.

For the calls below either use Postman (new request, header `Authorization: Bearer {{token}}`) or Swagger:
open http://127.0.0.1:8080/docs, click **Authorize**, paste the token. Add header `x-fault-injection: off` in Postman
to avoid the spec's deliberate 1% simulated failures (Swagger cannot set it; just retry a 503).

### 7.2 Calls (S1)

| # | Method and URL | Body | What the audience sees |
|---|---|---|---|
| 1 | `GET /v1/claims/CLM-0000009-01/status?npi=1900014774&tin=933919124` | — | Paid claim: amounts, payment date and trace, "No action needed" |
| 2 | `GET /v1/claims/CLM-0000076-00/status?npi=1900025069&tin=955169095` | — | Pended: `RECORDS_REQUEST`, info needed from `PROVIDER`, expected date |
| 3 | `GET /v1/claims/CLM-0000040-00/status?npi=1900027511&tin=995013013` | — | Denied `CO-16` missing information, advice to send a corrected claim |
| 4 | `GET /v1/claims/CLM-0000009-01/status?npi=1900014774&tin=900000000` | — | **403 `VERIFICATION_FAILED`**: a wrong TIN gets nothing |
| 5 | `GET /v1/authorizations/AUTH-0000347-32/status?npi=1900036686&tin=956458042` | — | Urgent SNF request, `clock_paused: true`, discharge summary outstanding |
| 6 | `POST /v1/authorizations/AUTH-0000347-32/documents` | see below | Document received, clock resumes; repeat call 5 to show it |
| 7 | `POST /v1/eligibility/verify` | see below (`MBR-0000001`) | Eligible, plan, PCP, cost share, reference number |
| 8 | `POST /v1/eligibility/verify` | same with `MBR-0000190` | Not eligible today: terminated retroactively |
| 9 | `GET /v1/knowledge/search?q=prior authorization urgent&k=3` | — | Policy text with titles and chunk ids (citations) |

Call 6 body:

```json
{"doc_type": "DISCHARGE_SUMMARY", "document_ref": "doc-1", "npi": "1900036686", "tin": "956458042"}
```

Call 7 body (call 8: change `member_id` to `MBR-0000190`):

```json
{"npi": "1900014774", "tin": "933919124", "date_of_service": "2026-10-12", "member_id": "MBR-0000001"}
```

Talking point for call 4: "Verification is enforced in the API, not left to the agent."
Talking point for call 9: "Answers are grounded in governed documents and cite their source."

## 8. Act 5 — Member-facing API, S2 (optional, 4 min)

Run the **S2** collection folders 1 and 2 (about 10 s in total), then folder 3 for a token
(the latest s8 replaces the live tenant API; to go back to S1, run S1's s8 again).

| # | Method and URL | Body | Shows |
|---|---|---|---|
| 1 | `GET /v1/members/MBR-0000045/benefits` | — | Cost share, allowances, transport benefit and that this member needs transport (MBN-03) |
| 2 | `GET /v1/members/MBR-0000024/benefits?on_behalf_of=778652f1-121b-530b-a495-4741336b2ed4` | — | A caregiver whose scope is BENEFITS is allowed (MBN-04) |
| 3 | `GET /v1/members/MBR-0000024/accumulators?on_behalf_of=778652f1-121b-530b-a495-4741336b2ed4` | — | Same caregiver, accumulators are benefits too: allowed |
| 4 | `PUT /v1/members/MBR-0000242/pcp?dry_run=true&on_behalf_of=6d7fb257-4029-5345-848a-1769067fc888` | `{"npi": "1900012885"}` | **403 `SCOPE_NOT_AUTHORIZED`**: this caregiver may only book appointments (MPC-04) |
| 5 | `GET /v1/providers/search?specialty=pediatrics&language=es&member_id=MBR-0000023` | — | Rural Spanish-speaking family: access standard 30 miles, widened search or access gap, telehealth (MPS-02) |
| 6 | `PUT /v1/members/MBR-0000017/pcp?dry_run=true` | `{"npi": "1900012885"}` | PCP change validated: participating, open panel, within distance, effective 2026-11-01 |
| 7 | `GET /v1/providers/1900003157/availability?member_id=MBR-0000130` | — | Next slots skipping blackout dates, wait standard met or not |
| 8 | `POST /v1/appointments?dry_run=true` | see below | Booking validated; open care gaps `HBD`, `TRC` and a transport offer (MAS-02) |

Call 8 body:

```json
{"member_id": "MBR-0000130", "npi": "1900003157", "location_id": "f50cc21e-9ec1-5897-aa12-1c949c8e0a54",
 "start": "2026-10-20T08:00:00", "visit_type": "WELLNESS"}
```

Say: "`dry_run` validates everything without writing, which is how the deployment tests run and how an agent can
confirm before acting. Drop `dry_run` to really make the change."

## 9. Act 6 — Appeals and payments, S3 (optional, 3 min)

Run the **S3** collection folders 1 and 2 (about 17 s), then folder 3 for a token.

| # | Method and URL | Body | Shows |
|---|---|---|---|
| 1 | `POST /v1/claims/CLM-0000001-04/appeals?dry_run=true` | see below | Appealable denial within the deadline: appeal validated (PAP-01) |
| 2 | `GET /v1/appeals/APL-0000003-03?npi=1900001961&tin=950399998` | — | Level 1 appeal in review, decision due date (PAP-02) |
| 3 | `GET /v1/appeals/APL-0000004-13?npi=1900000849&tin=924566903` | — | Overturned, **awaiting reprocessing**, expected date (PAP-03) |
| 4 | `GET /v1/appeals/APL-0000001-01?npi=1900001268&tin=925680980` | — | Filed after the deadline: exception review, no promise (PAP-04) |
| 5 | `GET /v1/claims/CLM-0000001-05/payments?npi=1900003017&tin=970773329` | — | Recoupment: part of the remittance recovered for an earlier overpayment (PPI-03) |
| 6 | `GET /v1/claims/CLM-0000006-09/payments?npi=1900003785&tin=926344052` | — | Paid to a different TIN: warning, correction ticket, no change by the agent (PPI-04) |
| 7 | `GET /factory/runs/perf-claims-03/analytics/claims-summary` | — | Analytics: claims by status, denials by category, monthly volumes, appeal outcomes |

Call 1 body:

```json
{"level": "L1", "reason": "MED_NECESSITY", "documents": ["doc-1"], "npi": "1900001334", "tin": "910634860"}
```

## 10. Act 7 — Trust: repeatability and guardrails (3 min)

Pick any of these (S1 collection):

| Demo | How | Expected |
|---|---|---|
| Same input, same data | Re-send **s3**; compare `entities[].checksum` with the first run | identical checksums; `GET /factory/runs/ma-demo-01` now shows s7 and s8 `NOT_RUN` (they must re-run) |
| Strict payload checking | Edit the **s4** body: `"root_seed": 1`, send | **409 `RUN_SETTINGS_DIFFER`** with a field-by-field diff |
| Bad stage seed | Edit **s4**: `"stage_seed": 123` | **422 `BAD_STAGE_SEED`** naming the expected FNV-1a value |
| Order enforced | **Reset run**, then send **s5** first | **409 `STAGE_ORDER`** |
| Size guard | In the S2 collection, set s4 `volumes.members` to `100000` | **413 `PAYLOAD_TOO_LARGE_FOR_PROFILE`** |
| Automated tests | `.venv/Scripts/python -m pytest -q` | `43 passed, 6 skipped` in about a minute (skips are scenario-specific tests) |

Say: "Determinism is proven by tests: identical checksums for 1, 3, 5 or 7 shards, identical replays, and a shard that
fails half-way is retried with identical results."

## 11. Act 8 — Path to the cloud (2 min)

1. Show `config/memory-offline.yaml` next to `config/gcp.yaml`: same ports, different adapters
   (AlloyDB, BigQuery, Cloud Storage, Vector Search, Pub/Sub, Vertex AI, Cloud Run Jobs, Cloud Run + API Gateway).
2. Show `Dockerfile`: one image, three roles (`serve`, `task`, `api`).
3. Run `.venv/Scripts/python -m factory doctor` to list the adapters of the active profile.

Say: "`FACTORY_PROFILE` is the only switch. The GCP adapters are written against the real client libraries; the next
step is running them in a project."

## 12. Likely questions

| Question | Answer |
|---|---|
| Is any real patient data used? | No. Every row is synthetic and stamped `is_synthetic`; phones, emails and TINs use reserved formats. |
| Why not ML-generated data? | There is no real data to learn from and the rules must hold exactly; rule-driven generation is deterministic and verifiable. |
| What if a spec payload lacks something? | The engine derives it and says so in `notes`; the design doc lists every gap (plan LOBs, member scenario mix, history window, use cases). |
| How big can it go? | In memory up to 60k members / 200k claims per run (~1.5M rows); larger runs are for the gcp profile. |
| Is it running on GCP? | Not yet. The adapters are implemented against the client libraries but untested in a project. |
| What would break in the cloud first? | Tokens and rate limits are per instance, the secret is not in Secret Manager yet, and Cloud Run ingress needs restricting (see `docs/api-engine-design.md` §12). |
| Can agents use it? | Yes: `api/tools.json` defines one tool per endpoint for Dialogflow CX or Vertex AI agents. |

## 13. Troubleshooting

| Symptom | Fix |
|---|---|
| `serve` fails with "address already in use" | Another server holds port 8080. Stop it (Ctrl+C), or find it: `netstat -ano \| findstr :8080`, then `taskkill /PID <pid> /F`. |
| `503 INJECTED_FAULT` on a business call | The spec's fault injection (1% in S1, 3% in S2/S3). Retry, or send header `x-fault-injection: off`. |
| `401 INVALID_TOKEN` | Token expired (1 h) or the server restarted; run **Get local credentials** and **Get token** again. |
| `404 NOT_DEPLOYED` on credentials | s8 has not run for that tenant (or a later stage reset it); run s8 again. |
| IDs from this guide return 404 | The server restarted or another scenario's s8 replaced the live API. Re-run that collection's folders 1 and 2. |
| `409 STAGE_ORDER` | Run the stages in order; after re-running any of s3–s6, run s7 and s8 again. |

The data is deterministic: after a restart, re-running a collection produces the same rows and the same IDs.

---

## Appendix A — S1 request bodies

Exactly as in `postman/hf-factory-s1.postman_collection.json`: the spec's S1 payloads, with one documented change —
the s4 body carries the s5 `scenario_mix` so member scenarios (PEV-01..04) are generated.

### s3 — `POST /factory/data-engine/s3`

```json
{
  "job": "generate_reference_data",
  "stage": "s3",
  "root_seed": 20261012,
  "stage_seed": 3033971103,
  "as_of_date": "2026-10-12",
  "catalog_version": "2.4.0",
  "id_strategy": "UUIDv5(dataset_id, entity, seed, row_index)",
  "shards": 8,
  "consumes": {
    "from": "s2",
    "alloydb": "alloy-ma-demo-01",
    "gcs": "gs://ma-demo-01-docs",
    "vector_index": "kb-ma-demo-01"
  },
  "entities": [
    "plan",
    "provider",
    "network_participation",
    "practice_location"
  ],
  "volumes": {
    "providers": 540,
    "plans": 4,
    "access_standards": 120
  },
  "rules": [
    "R-04",
    "F-04",
    "P-02"
  ]
}
```

### s4 — `POST /factory/data-engine/s4`

```json
{
  "job": "generate_members",
  "stage": "s4",
  "root_seed": 20261012,
  "stage_seed": 2983638246,
  "as_of_date": "2026-10-12",
  "catalog_version": "2.4.0",
  "id_strategy": "UUIDv5(dataset_id, entity, seed, row_index)",
  "shards": 8,
  "consumes": {
    "from": "s2",
    "alloydb": "alloy-ma-demo-01",
    "gcs": "gs://ma-demo-01-docs",
    "vector_index": "kb-ma-demo-01"
  },
  "consumes_refs": "s3 plan and provider ids",
  "entities": [
    "member_demographics",
    "family_relationship",
    "plan_enrollment",
    "member_accumulator",
    "pcp_assignment",
    "risk_score",
    "care_gap",
    "sdoh_indicator"
  ],
  "volumes": {
    "members": 50000
  },
  "distributions": {
    "lob_mix": {
      "MEDICARE_ADV": 1
    },
    "age_by_lob": "D-02",
    "denial_rate": 0.1
  },
  "personas": [
    "BILLING",
    "REFERRAL",
    "OFFICE"
  ],
  "rules": [
    "R-02",
    "R-05",
    "R-06",
    "T-01"
  ],
  "scenario_mix": [
    {
      "scenario_id": "PCS-01",
      "share_pct": 31.7
    },
    {
      "scenario_id": "PCS-02",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PCS-03",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PCS-04",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PAS-01",
      "share_pct": 31.7
    },
    {
      "scenario_id": "PAS-02",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PAS-03",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PAS-04",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PEV-01",
      "share_pct": 31.7
    },
    {
      "scenario_id": "PEV-02",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PEV-03",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PEV-04",
      "share_pct": 0.6
    }
  ]
}
```

### s5 — `POST /factory/data-engine/s5`

```json
{
  "job": "generate_claims_auth",
  "stage": "s5",
  "root_seed": 20261012,
  "stage_seed": 3000415865,
  "as_of_date": "2026-10-12",
  "catalog_version": "2.4.0",
  "id_strategy": "UUIDv5(dataset_id, entity, seed, row_index)",
  "shards": 8,
  "consumes": {
    "from": "s2",
    "alloydb": "alloy-ma-demo-01",
    "gcs": "gs://ma-demo-01-docs",
    "vector_index": "kb-ma-demo-01"
  },
  "consumes_refs": "s3 providers, s4 members and enrollments",
  "entities": [
    "claim_header",
    "claim_line",
    "pharmacy_claim_detail",
    "claim_denial",
    "claim_pend",
    "claim_adjustment",
    "claim_payment",
    "appeal_record",
    "reprocessing_history",
    "auth_request",
    "auth_line",
    "auth_determination",
    "clinical_review_queue",
    "documentation_requirement"
  ],
  "volumes": {
    "claims": 50000,
    "authorizations": 2500
  },
  "denial_rate": 0.1,
  "scenario_mix": [
    {
      "scenario_id": "PCS-01",
      "share_pct": 31.7
    },
    {
      "scenario_id": "PCS-02",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PCS-03",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PCS-04",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PAS-01",
      "share_pct": 31.7
    },
    {
      "scenario_id": "PAS-02",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PAS-03",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PAS-04",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PEV-01",
      "share_pct": 31.7
    },
    {
      "scenario_id": "PEV-02",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PEV-03",
      "share_pct": 0.6
    },
    {
      "scenario_id": "PEV-04",
      "share_pct": 0.6
    }
  ],
  "rules": [
    "R-03",
    "R-07",
    "F-01",
    "F-02",
    "F-05",
    "T-02",
    "T-03",
    "T-04"
  ]
}
```

### s6 — `POST /factory/data-engine/s6`

```json
{
  "job": "generate_care_knowledge",
  "stage": "s6",
  "root_seed": 20261012,
  "stage_seed": 2950083008,
  "as_of_date": "2026-10-12",
  "catalog_version": "2.4.0",
  "id_strategy": "UUIDv5(dataset_id, entity, seed, row_index)",
  "shards": 8,
  "consumes": {
    "from": "s2",
    "alloydb": "alloy-ma-demo-01",
    "gcs": "gs://ma-demo-01-docs",
    "vector_index": "kb-ma-demo-01"
  },
  "consumes_refs": "s3 plans and providers, s4 members",
  "entities": [
    "care_case",
    "transition_of_care",
    "knowledge_asset",
    "asset_chunk",
    "faq_entry",
    "agent_sop",
    "asset_link",
    "qa_golden"
  ],
  "volumes": {
    "knowledge_assets": 49,
    "faq_entries": 90
  },
  "llm": {
    "provider": "Vertex AI",
    "model": "pinned at blueprint time",
    "temperature": 0,
    "text_cache": "content-addressed, Cloud Storage"
  },
  "rules": [
    "R-09",
    "R-10",
    "R-11",
    "T-08"
  ]
}
```

### s7 — `POST /factory/data-engine/s7`

```json
{
  "job": "validate_and_scan",
  "stage": "s7",
  "consumes": {
    "from": [
      "s3",
      "s4",
      "s5",
      "s6"
    ],
    "entity_count": 34,
    "total_rows": 860839
  },
  "rule_sets": [
    "R",
    "T",
    "F",
    "C",
    "P",
    "A"
  ],
  "privacy_scan": {
    "near_duplicates": true,
    "rare_combinations": true,
    "reserved_formats": true
  }
}
```

### s8 — `POST /factory/api-engine/s8`

```json
{
  "job": "api_generate_and_deploy",
  "stage": "s8",
  "style": "REST, OpenAPI 3.1",
  "base_path": "/v1",
  "use_cases": [
    "PCS",
    "PAS",
    "PEV"
  ],
  "consumes": {
    "from": [
      "s2",
      "s7"
    ],
    "alloydb": "alloy-ma-demo-01",
    "bigquery": "ma_demo_01_claims",
    "vector_index": "kb-ma-demo-01",
    "manifest": "gs://ma-demo-01-docs/manifest/20261012.json"
  },
  "endpoints": [
    {
      "method": "GET",
      "path": "/v1/claims/{claim_id}/status",
      "use_case": "PCS"
    },
    {
      "method": "GET",
      "path": "/v1/authorizations/{auth_id}/status",
      "use_case": "PAS"
    },
    {
      "method": "POST",
      "path": "/v1/authorizations/{auth_id}/documents",
      "use_case": "PAS"
    },
    {
      "method": "POST",
      "path": "/v1/eligibility/verify",
      "use_case": "PEV"
    },
    {
      "method": "POST",
      "path": "/v1/identity/verify",
      "use_case": "COMMON"
    },
    {
      "method": "GET",
      "path": "/v1/knowledge/search",
      "use_case": "COMMON"
    }
  ],
  "auth": {
    "type": "OAuth2 client credentials",
    "secret_ref": "projects/hf-ma-demo-01/secrets/api-client-secret"
  },
  "rate_limit": {
    "per_client_rpm": 600
  },
  "fault_injection": {
    "error_rate_pct": 1,
    "latency_p95_ms": 450
  },
  "agent_tools": true
}
```

S2 and S3 bodies are in their Postman collections (and `tests/fixtures/s2_jobs`, `s3_jobs`); their volumes are scaled
to the in-memory limit as noted in each request's description.
