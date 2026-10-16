# Healthcare AI Platform Factory: Data Engine + API Engine (v1)

The **Data Engine** (stages s3–s7) and **API Engine** (stage s8) from the architecture spec, exposed as a
REST control plane. Each stage is triggered by its own job payload, exactly as the spec shows them; the
engines read nothing else (no blueprint). The Orchestrator is whoever calls the endpoints in order, for
example Postman.

All data is synthetic. Infrastructure target is GCP; v1 runs fully in memory.

![Architecture: REST control plane, Data Engine s3-s7, API Engine s8, ports and adapters, runtimes](docs/architecture.svg)

## Quick start (Windows)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
copy .env.example .env
.venv/Scripts/python -m factory serve
```

Control-plane docs: http://127.0.0.1:8080/factory/docs. Then call, in order:

| Step | Endpoint | Body (spec scenario S1) |
|---|---|---|
| 1 | `POST /factory/data-engine/s3` | `generate_reference_data` payload |
| 2 | `POST /factory/data-engine/s4` | `generate_members` payload |
| 3 | `POST /factory/data-engine/s5` | `generate_claims_auth` payload |
| 4 | `POST /factory/data-engine/s6` | `generate_care_knowledge` payload |
| 5 | `POST /factory/data-engine/s7` | `validate_and_scan` payload |
| 6 | `POST /factory/api-engine/s8` | `api_generate_and_deploy` payload |

The payloads are in `tests/fixtures/s1_jobs/` (extracted from the spec's own S1 code, with the spec's expected
outputs beside them). Import `postman/hf-factory-s1.postman_collection.json` for the same sequence in Postman.

After s8 the tenant's API is live on the same server: Swagger at http://127.0.0.1:8080/docs, local test
credentials at `GET /factory/runs/ma-demo-01/credentials`.

Other endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /factory/runs`, `GET /factory/runs/{tenant}` | Stage status, run settings, stage results |
| `GET /factory/runs/{tenant}/manifest` | s7 manifest: hashes, seed, per-entity counts and checksums |
| `GET /factory/runs/{tenant}/rule-report` | All rule results and the privacy scan |
| `GET /factory/runs/{tenant}/tables/{entity}?scenario_id=PCS-02&limit=5` | Peek at generated rows (find test IDs) |
| `DELETE /factory/runs/{tenant}` | Drop the in-memory run and start over |

`python -m factory doctor` shows the active profile and adapter health.

## How payloads are handled

- **A run starts at s3.** Its `root_seed`, `as_of_date`, `catalog_version` and `id_strategy` fix the run.
  The tenant (and dataset id) comes from `consumes.alloydb` (`alloy-ma-demo-01` → `ma-demo-01`), or `tenant_id`,
  or `?tenant=`. s7 has none of these, so it applies to the run in progress.
- **Later payloads must agree with the run:** a different seed, date, catalog, `denial_rate` or `scenario_mix`
  is a 409 with a diff. `stage_seed` must be FNV-1a of `"<root_seed>:<stage>"`, as in the spec (else 422).
- **Each payload's `entities` decide what is generated.** Unknown or not-yet-implemented entities are rejected (422).
- **Order:** s4 needs s3, s5 needs s4, s6 needs s5 (v1), s7 needs s3–s6, s8 needs a green s7 (else 409).
  Re-running a stage is idempotent (same checksums) and resets s7/s8, which must then be run again.
- **Per-stage `rule_checks`:** each rule the payload names: `PASS`, `FAIL`, `PASS_PARTIAL` (sub-rules that need
  later tables are checked in s7) or `DEFERRED_TO_S7`.
- **s8** uses only its payload: `use_cases`, `base_path`, `endpoints` (must match the endpoint catalog),
  `auth`, `rate_limit`, `fault_injection`, `agent_tools`. Tenant, seed and as-of date come from the s7 manifest it consumes.

### Gaps in the spec payloads and how they are filled

Every derivation is reported in the response's `notes`.

| Stage | Missing from the spec payload | What the engine does | Optional field to set it |
|---|---|---|---|
| s3 | LOB of each plan | Plans cycle Medicare Advantage, Commercial, Medicaid; s4's `lob_mix` picks plans | `distributions.lob_mix` |
| s4 | Scenario mix for member scenarios (PEV-01..04) | Members are baseline rows; PEV scenarios are not injected | `scenario_mix` |
| s4 | `member_accumulator` is listed but needs paid claims | Generated in s5 (rule F-03) | — |
| s5 | Claims history window | `history_months = claims × 12 / (members × 6)` (D-05: median 6 claims/member/year) | `history_months` |
| s6 | Use cases for FAQs and SOPs | Taken from the scenario ids in the run's `scenario_mix` | `use_cases` |
| s6 | Ordering | Runs after s5 (care records and denial-code links need claims; design doc gap 1) | — |

## One variable switches local and cloud

`FACTORY_PROFILE` in `.env` is the only setting that differs between environments:

| Profile | Tables | Blobs | Vectors | Events | LLM |
|---|---|---|---|---|---|
| `memory-offline` | DuckDB in memory | dict | NumPy | log | deterministic offline stand-in |
| `memory` | DuckDB in memory | dict | NumPy | log | **Vertex AI** (Gemini + embeddings) |
| `gcp` | AlloyDB | Cloud Storage | Vertex AI Vector Search | Pub/Sub | Vertex AI |

Each profile is a file in `config/`. Only `factory/adapters/__init__.py` maps a profile to backends;
`tests/test_portability.py` fails if engine code imports a backend library or reads the environment.
For `gcp`, resource names come from the s2 output: `python -m factory --s2 config/s2_output_gcp.example.json doctor`.

### Using Vertex AI (`memory` profile)

1. Install the gcloud CLI and run `gcloud auth application-default login`.
2. Enable the Vertex AI API in your project.
3. Set `llm.project` in `config/memory.yaml` and pin `llm.model` to a model your project can use.
4. Set `FACTORY_PROFILE=memory` and run `python -m factory doctor`.

## What is built

**Data engine (s3–s7)**, scenario S1 (PCS, PAS, PEV): 34 entities across Member, Provider, Claims,
Authorization, Care Management and Knowledge.

- **Repeatability:** every value is a pure function of (seed, entity, row key, draw name); IDs are
  `UUIDv5(dataset_id, entity, seed, row_key)`; dates come from `as_of_date`, never the clock. Checksums are
  identical for any shard count, and growing members is prefix-stable (tests prove both).
- **Scenarios:** PCS-01..04 and PAS-01..04 (and PEV-01..04 when s4 has a scenario mix) are injected and tagged on every row.
- **Rules (s7):** 120 checks: R/T/F/C/P/A families plus 45 foreign keys, written as portable SQL.
- **Knowledge (s6):** documents generated through a content-addressed cache, chunked, embedded and indexed.
- **Manifest:** data-spec hash (from the s3–s6 payloads), payload hashes, seed, versions, per-entity counts and checksums.

**API engine (s8):** one pre-built FastAPI backend; the payload switches endpoints on. S1 endpoints: claim status,
authorization status, document upload, eligibility verify, identity verify, knowledge search. OAuth2 client
credentials (local stand-in for API Gateway + Secret Manager), per-client rate limit, seeded fault injection,
NPI+TIN verification, minimum-necessary cap on bulk lookups. Publishes `openapi.json` and `tools.json`; runs 24
contract tests (4 per endpoint).

## Known gaps in v1

- Only the S1 use cases (PCS, PAS, PEV) have endpoints and scenario recipes.
- The control plane has no authentication; it binds to 127.0.0.1. One tenant API is served at a time.
- No overturned appeals or reprocessing yet; `reprocessing_history` is empty.
- GCP adapters and the Cloud Run deployer have final signatures but raise `NotImplementedError`.
- Privacy scan checks NPI Luhn validity but not real-NPI collisions (needs a pinned NPPES snapshot).
- Reference tables are small illustrative subsets. CPT is AMA-licensed; production needs a licensed table.
- Responses for endpoints beyond the spec's four sample payloads were designed from the entity model and need review.
