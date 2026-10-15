# Healthcare AI Platform Factory: Data Engine + API Engine (v1)

First working version of the **Data Engine** (stages s3–s7) and **API Engine** (stage s8) from the
architecture spec. The interpreter, blueprint engine, orchestrator and infra engine are stubbed with a
blueprint JSON and an s2-output JSON, exactly as the spec shows each stage's input and output.

All data is synthetic. Infrastructure target is GCP; v1 runs fully in memory.

## Quick start (Windows, PowerShell or Git Bash)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
copy .env.example .env
.venv/Scripts/python -m factory up
```

`up` generates the S1 dev dataset (5,000 members), validates it, runs the API engine, prints the delivery
manifest and local test credentials, then serves the API at http://127.0.0.1:8080 (Swagger UI at `/docs`).

Other commands:

| Command | What it does |
|---|---|
| `python -m factory doctor` | Active profile and health of every adapter |
| `python -m factory generate --export out/` | Data engine only; writes Parquet tables + documents + manifest |
| `python -m factory verify` | Repro-check: regenerate with 1 and 6 shards, compare entity checksums |
| `python -m factory up --blueprint blueprints/s1_ma_demo.json --shards 8` | Full S1 (50,000 members, ~815k rows, ~10 s) |
| `python -m pytest` | Test suite |

## One variable switches local and cloud

`FACTORY_PROFILE` in `.env` is the only setting that differs between environments:

| Profile | Tables | Blobs | Vectors | Events | LLM |
|---|---|---|---|---|---|
| `memory-offline` | DuckDB in memory | dict | NumPy | log | deterministic offline stand-in |
| `memory` | DuckDB in memory | dict | NumPy | log | **Vertex AI** (Gemini + embeddings) |
| `gcp` | AlloyDB | Cloud Storage | Vertex AI Vector Search | Pub/Sub | Vertex AI |

Each profile is a file in `config/`. Only `factory/adapters/__init__.py` maps a profile to backends;
`tests/test_portability.py` fails if engine code imports a backend library or reads the environment.
For `gcp`, resource names come from the s2 output: `python -m factory --s2 blueprints/s2_output_gcp.example.json doctor`.

### Using Vertex AI (`memory` profile)

1. Install the gcloud CLI and run `gcloud auth application-default login`.
2. Enable the Vertex AI API in your project.
3. Set `llm.project` in `config/memory.yaml` and pin `llm.model` to a model your project can use.
4. Set `FACTORY_PROFILE=memory` and run `python -m factory doctor`.

## What is built

**Data engine (s3–s7), scenario S1 (PCS, PAS, PEV)**: 34 entities across Member, Provider, Claims,
Authorization, Care Management and Knowledge.

- **Repeatability:** every value is a pure function of (seed, entity, row key, draw name); IDs are
  `UUIDv5(dataset_id, entity, seed, row_key)`; dates come from `as_of_date`, never the clock.
  Checksums are identical for any shard count, and growing members is prefix-stable (tests prove both).
- **Scenarios:** PCS-01..04, PAS-01..04 and PEV-01..04 are injected at the blueprint's share and tagged on every row (`scenario_id`).
- **Rules (s7):** 113 checks: R/T/F/C/P/A families plus 45 foreign keys, written as portable SQL.
  Deliberate rule breaks (for example stale directory rows) are exempted by tag, by name.
- **Knowledge (s6):** documents generated through a content-addressed cache
  (`sha256(model, prompt_version, prompt, seed)`), chunked, embedded and indexed.
- **Manifest:** blueprint hash, data-spec hash, seed, versions, per-entity counts and checksums.

**API engine (s8)**: one pre-built FastAPI backend; the blueprint switches endpoints on.

- S1 endpoints: claim status, authorization status, document upload, eligibility verify, identity verify, knowledge search.
- OAuth2 client credentials (local stand-in for API Gateway + Secret Manager), per-client rate limit,
  seeded fault injection, NPI+TIN verification (PCS-04), minimum-necessary cap on bulk lookups (PEV-04).
- Publishes `openapi.json` and `tools.json` (agent tool definitions); runs 24 contract tests (4 per endpoint).

## Known gaps in v1 (deliberate, see the design doc)

- Only the S1 use cases (PCS, PAS, PEV) have endpoints and scenario recipes; other blueprints fail fast with a clear message.
- Stage order is s3 → s4 → s5 → s6 (s6 needs claims; design doc gap 1). Accumulators are derived in s5 (gap 2).
- No overturned appeals or reprocessing yet; `reprocessing_history` is empty.
- GCP adapters and the Cloud Run deployer have final signatures but raise `NotImplementedError`.
- Privacy scan checks NPI Luhn validity, but not real-NPI collisions (needs a pinned NPPES snapshot).
- Reference tables are small illustrative subsets. CPT is AMA-licensed; production needs a licensed table.
- Responses for endpoints beyond the spec's four sample payloads were designed from the entity model and need review.
