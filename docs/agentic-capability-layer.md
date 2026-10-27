# Agentic capability layer: plan

Status: **phase 0 done** (§12); phases 1–4 planned. Owner: factory team.

## 1. Problem

The Data Engine (s3–s7) and the API Engine (s8) only support what is coded into them: the 42 catalog
entities, 40 scenario recipes, the rule set, and the endpoints of ten use cases. A stage payload that asks for
anything else is rejected (`UNKNOWN_ENTITIES`, `ENTITIES_NOT_IMPLEMENTED`, `USE_CASES_NOT_IMPLEMENTED`,
`ENDPOINTS_DIFFER`). Gemini is used only for knowledge-document text and embeddings.

**Goal:** when a payload asks for a new capability, an agentic layer builds it — schema, data generation,
rules and API — and the stage then runs, without a person writing code.

**Constraints**

- The engines never see the blueprint. Each engine receives only its own stage payload, in today's format.
- The layer works from the payloads alone. Payloads carry names (entity names, scenario ids, use-case codes,
  endpoint paths), not descriptions.
- Generated data stays deterministic and replayable: same payloads, seed and pack versions give the same rows.
- Infrastructure is GCP (Vertex AI Gemini for the LLM).

## 2. Decisions

| Topic | Decision |
|---|---|
| Trigger | **Preflight first**: the orchestrator sends all stage payloads before s3. **`202` fallback** for a stage payload whose gaps were not built by a preflight. |
| Input to agents | **Names alone.** No hints field in payloads. Agents rely on a custom **capability skill** with the data, API and domain knowledge (§6). |
| Pack scope | **Shared globally.** A pack built once is reused by every tenant. |
| Approval | **Automatic.** A pack that passes the gates (§8) is activated. End-to-end smoke tests run in later pipeline stages, after the Data and API engines, and are out of scope here. |
| What agents produce | **Declarative capability packs**, interpreted by generic engines — not generated Python. |

Why packs and not generated code: a pack can be validated mechanically (schema, foreign keys, SQL allowlist,
determinism), diffed, versioned, hashed into the manifest and replayed without calling the LLM.

## 3. Architecture

```
Orchestrator / Postman
   │  POST /factory/capabilities/preflight  (all stage payloads, optional but recommended)
   │  POST /factory/data-engine/s3..s7      (stage payloads, unchanged format)
   │  POST /factory/api-engine/s8
   ▼
┌──────────────────────────── Capability layer ────────────────────────────┐
│ 1. Gap check (deterministic): payload items vs the Capability Registry    │
│ 2. No gap  → the payload goes to the engine unchanged                     │
│ 3. Gap     → agents build packs (skill-guided) → gates → registry (active) │
└──────────────────────────────────────────────────────────────────────────┘
   ▼
Data Engine s3–s7 / API Engine s8
   at stage start: load the active packs the payload needs and run them
   next to the built-in ("core") capabilities
```

The engines know nothing about agents. They gain one ability: running capability packs from the registry.
Today's built-in catalogs (`catalog.py`, `scenarios.py`, rules, `ENDPOINTS`) become the **core pack**, so core and
generated capabilities go through one path.

Deployment: on GCP the builder is a separate Cloud Run service (builds never block the engines); in the memory
profiles it runs in-process. The registry is Cloud Storage (pack documents) plus a metadata table (versions,
status, gap signatures); in memory profiles it is a dict.

## 4. Gap detection (no LLM)

| Payload field | Stage | A new item means |
|---|---|---|
| `entities[]` | s3–s6 | a new table: schema, generator, foreign keys |
| `scenario_mix[].scenario_id` | s4, s5 | a new scenario recipe |
| `distributions.*`, `volumes.*` keys | s3–s6 | a new generation parameter |
| `rules[]` | s3–s6 | a new rule |
| `rule_sets[]` | s7 | a new rule family |
| `use_cases[]` | s8 | a new use case |
| `endpoints[]` (method, path, use case) | s8 | new endpoints; the path is a strong spec |
| `personas[]` | s4, s8 | new personas or verification behaviour |

The result is a list of gaps and a **gap signature** (sorted, canonical), e.g.
`entity:premium_invoice|scenario:MBP-02|uc:MBP`. The signature deduplicates builds across tenants and requests.

## 5. Request flow

### 5.1 Preflight (primary)

```
POST /factory/capabilities/preflight
{ "payloads": { "s3": {...}, "s4": {...}, "s5": {...}, "s6": {...}, "s7": {...}, "s8": {...} } }   # any subset
```

Response:

```json
{ "status": "READY | BUILDING | UNDERSPECIFIED | FAILED",
  "build_id": "bld_…",
  "gaps": [{"kind": "entity", "name": "premium_invoice", "stage": "s4"}, {"kind": "use_case", "name": "MBP"}],
  "packs": {"entity.premium_invoice": "1.0.0", "uc.MBP": "1.0.0"},
  "assumptions": ["premium_invoice is member-anchored (prefix-free name, billing domain): generated in s4", "…"],
  "questions": [] }
```

- With all payloads at once the agents see a whole new use case as it spans stages (an entity in s4, a claim
  link in s5, knowledge in s6, rules in s7, endpoints in s8) and build it as one coherent set of packs.
- `READY` means every gap maps to an active pack. The orchestrator then runs s3–s8 as today; each stage
  payload passes the gap check.
- `GET /factory/capabilities/builds/{build_id}` reports progress; the same build id is returned to any request
  with the same gap signature.

### 5.2 `202` fallback (a stage payload arrives with uncovered gaps)

1. `POST /factory/data-engine/s4` lists `premium_invoice`, which no active pack covers.
2. Response `202 CAPABILITY_BUILD_STARTED` with `{build_id, gaps}` and a note that a single-payload build sees
   less context than a preflight.
3. The caller polls the build, then resends the same payload (idempotent by payload hash); the stage runs.
4. `?wait=true` makes it one blocking call (local demos).

At s8, a fallback build can only design over the tables the run actually generated. If the use case needs data
that is missing, the answer is `409 DATA_MISSING_FOR_USE_CASE` naming the entity to add and the stage payload
to add it to — the entity pack is already built.

### 5.3 States and errors

| Status | Meaning |
|---|---|
| `BUILDING` | agents and gates running |
| `READY` | all packs active |
| `UNDERSPECIFIED` | a name could not be interpreted with enough confidence; `questions` say which and why. The fix is a skill update (glossary), since payloads carry no hints |
| `FAILED` | gates still failing after the fix budget; the diagnosis is attached |

The capability layer is switched on with `FACTORY_CAPABILITY_LAYER=on`. Off, the engines behave exactly as today.

## 6. The capability skill

A versioned knowledge bundle the agents load. It is what makes "names alone" workable. It is LLM-neutral
(Gemini today).

```
skills/healthcare-capability-builder/
  SKILL.md                    role; workflow (gap → plan → schema → data → api → verify);
                              hard rules; output contract
  references/
    pack-schema.json          the pack format (generated from the Pydantic models)
    generators.md             building-block library: parameters, examples, when to use each
    data-conventions.md       anchors and stages, key slots, integer cents, dates from as_of only,
                              no wall clock, scenario tags, P-01 stamps, dirty-data tags
    api-conventions.md        paths, verification factors (NPI+TIN, member id + DOB + ZIP), ErrorCode
                              catalog, dry_run, minimum necessary, caregiver scope, is_synthetic,
                              four contract tests per endpoint
    domain/
      entities.md             the entities with columns, keys and foreign keys   (generated from catalog.py)
      codes.md                coded values                                       (generated from codes.py)
      errors.md               API and control-plane error codes                   (generated from errors.py)
      standards.md            X12 270/271, 276/277, 278, 835, 837; FHIR resources; CARC/RARC; NCPDP
      use-cases.md            use-case structure: can / escalate / never, verification, handoff queues
      rules.md                rule families R/T/F/C/P/A with examples
      glossary.md             how to read names (below)
  examples/                   complete worked packs (sdoh_indicator from phase 0, MBP from phase 1, …)
```

**Reading names** (`glossary.md`), based on conventions the codebase already follows:

| Name pattern | Inferred meaning |
|---|---|
| Use-case codes: `MBP` member billing and premium, `MPA` member PA status, `MRX` member pharmacy, `PON` provider onboarding | the use case; `M…` / `P…` sets audience and verification factors |
| Entity suffixes `_header`/`_line`, `_record`, `_history`, `_schedule`, `_indicator`, `_assignment` | shape and rows per parent |
| Entity prefixes `member_`, `claim_`, `auth_`, `provider_` | anchor, hence stage (member → s4, claim/auth → s5, provider/plan → s3) |
| Scenario ids `<UC>-01` … `<UC>-04` | `-01` happy path; `-02`/`-03` edge; `-04` compliance or data |
| Endpoint paths, e.g. `GET /v1/members/{member_id}/premiums` | resource, parameters, member vs provider verification |

**Keeping it accurate**

- Domain references (entities, codes, errors, building blocks) are generated from the code by a script; CI fails
  when they drift.
- Agents always load `SKILL.md` and pull references on demand through a `read_skill(path)` tool, keeping
  prompts small.
- The skill version is recorded in every pack, so a build can be traced and reproduced.
- Activated packs can be added to `examples/` (reviewed change), which improves later builds.

## 7. Capability packs

One pack per capability, with a data part and an API part:

| Engine | Pack content | Run by |
|---|---|---|
| Data Engine | entity schema: columns, types, primary key, foreign keys, **anchor** (sets the stage), rows per parent; per-column building blocks (weighted choice, range, date offset from the anchor, parent lookup, money in cents, derived value, reference table, LLM text); new distribution and volume keys with defaults; scenario recipes with column overrides; portable SQL rules | a generic entity generator inside the existing stages, using the per-row random streams, so output is deterministic and shard-independent; s7 runs pack rules |
| API Engine | endpoint specs: parameters, join path, response mapping, verification factors, caregiver scope, error codes, `dry_run` writes; agent tool descriptions; contract-test cases | a generic router next to the coded routes; a data-driven contract-test runner |

**Global packs**

- Ids and versions: `uc.MBP@1.0.0`, `entity.premium_invoice@1.0.0`, `scenario.MBP-02@1.0.0`. A published version is
  immutable; changes publish a new version.
- Tenant-neutral: a pack holds specs only, never tenant data (checked by a gate).
- Pinned per run: the run records the pack versions it used (`capability_packs` in the manifest, included in
  `data_spec_hash`). A newer version never changes an existing run or its replay; replays load stored packs and
  make no LLM calls.
- Row keys: each pack entity gets a key slot range from the registry, so pack keys never collide with core or
  other packs (`keys.py` conventions).

## 8. Agents and gates

| Step | Type | Job |
|---|---|---|
| Gap check | deterministic | payload items vs the registry (§4) |
| Capability planner | LLM + skill | interpret names: what the capability is, anchor, links to existing entities, use-case group; confidence per name |
| Schema agent | LLM + skill | entity specs and foreign keys |
| Data agent | LLM + skill | generation specs, distributions, recipes, rules; tries them in a sandbox run |
| API agent | LLM + skill | endpoint specs over tables that exist, error codes, tool descriptions, contract tests |
| Verifier | deterministic, plus an LLM fixer | run the gates; feed failures back; retry within an iteration and cost budget |

Gemini on Vertex AI with structured outputs (Pydantic models). Every prompt and response is stored with the
build. Orchestration is a plain loop in the builder service; Google ADK is an option if flows grow.

**Gates (all must pass; then the pack is activated automatically)**

1. Pack schema valid; foreign-key targets exist; key slots free; SQL is SELECT-only on known tables, in the
   DuckDB/PostgreSQL common subset.
2. Sandbox run of the affected stages at small scale: s7 rules (core and pack), privacy scan.
3. Determinism: identical checksums on a second run and for a different shard count.
4. Regression: checksums of the core pack and every active global pack are unchanged.
5. API: contract tests pass; OpenAPI diff has no breaking change to existing endpoints.
6. Tenant-neutral pack content.

## 9. Changes to the engines

| Area | Change |
|---|---|
| `control/validation.py` | with the layer on, unknown items become gaps (`202`) instead of rejections |
| `control/` | `capability_routes.py`: preflight, builds; gap check before validation |
| `data_engine/runner.py` | at stage start, load active packs for the payload's entities; run pack entities after the core ones, by anchor |
| `data_engine/` | `packs/`: pack models, generic entity generator, building-block library |
| `data_engine/rules.py` | include pack rules |
| `api_engine/` | generic router from endpoint specs; contract tests from spec cases |
| `contracts.py`, manifest | `capability_packs` pinned per run, in `data_spec_hash` |
| new `capability/` | registry port (memory, GCS) and builder (agents, gates) |
| core catalogs | exposed as the core pack |

## 10. Phases

Demo targets are today's known gaps: **MBP, MPA, MRX, PON** (no endpoints yet).

| Phase | Scope | Accepted when |
|---|---|---|
| **0 · Pack format** | pack models; building-block library; generic entity generator; `sdoh_indicator` re-expressed as a pack; first skill files (`pack-schema.json`, one example) | the pack produces **identical checksums** to the coded `sdoh_indicator` |
| **1 · Generic engines and registry** | registry (memory and GCS); generic router; data-driven contract tests; pack rules; preflight and `202` flow; packs **written by hand** | an s4 payload listing `premium_invoice` and an s8 payload with `use_cases: [MBP]` and its endpoints run end to end with no code change |
| **2 · Agents** | capability skill complete; planner, schema, data and API agents; verifier loop; automatic activation | the same payloads with an empty registry get the packs built, gated and activated; then MPA and MRX from names alone |
| **3 · Evolution** | new fields on existing entities (migrations for AlloyDB and BigQuery, deterministic backfill); coding-agent plugins for logic the building blocks cannot express | a new column on `claim_header` with existing checksums unchanged |
| **4 · Hardening** | agent evaluation suite (known capabilities rebuilt from names, scored against the core pack), cost limits, audit trail | — |

## 11. Risks

| Risk | Mitigation |
|---|---|
| Wrong interpretation of a name | skill glossary and standards; confidence threshold returns `UNDERSPECIFIED`; downstream smoke tests |
| A bad global pack reaches every tenant | gates, including regression across all active packs; immutable versions pinned per run |
| LLM-written SQL | endpoints compiled from specs; anything else allowlisted and parsed |
| Non-determinism | packs are data; LLM outputs stored; replays never call the LLM |
| Endless fix loops, cost | iteration and cost budget per build; `FAILED` with the diagnosis |
| Concurrent builds of the same capability | deduplication by gap signature; one build, many waiters |

## 12. Progress

### Phase 0 — done

| Delivered | Where |
|---|---|
| Pack format (Pydantic): `Pack`, `EntitySpec`, `Column`, `Rows`, 13 building blocks (`const`, `anchor`, `recipe`, `choice`, `bernoulli`, `integer`, `days_before_as_of`, `map`, `any`, `all`, `not`, `if`, `row_id`), validation with reasons, canonical `digest()` | `src/factory/data_engine/packs/model.py` |
| Generic entity generator: `AnchorFrame` (anchor rows of a shard) and an evaluator; rows select first, then columns; P-01 stamps and scenario tags inherited | `src/factory/data_engine/packs/engine.py` |
| Core pack loader; `sdoh_indicator` now generated from `packs/core/sdoh_indicator.yaml` — its coded writer was removed from s4 | `src/factory/data_engine/packs/`, `stages/s4_members.py` |
| Skill v0.1: `SKILL.md`, generated `pack-schema.json` and `generators.md`, the core pack as an example | `skills/healthcare-capability-builder/`, `scripts/build_skill_refs.py` |
| Regression snapshot tool (gate 4): S1, S1 with member scenarios, S2–S4; counts, checksums, rules, contract tests | `scripts/regression_snapshot.py` |

**Acceptance.** Identical output: the regression snapshot before and after the switch matches for all five
scenario runs (every checksum and count, rules, all contract tests). `tests/test_packs.py` also compares the pack
row by row with the former coded generator (kept in the test as the reference) at three seeds, shard counts and
LOB mixes, including the transport-need scenarios; validates rejection reasons; and fails when the skill's
generated references are out of date.

**Decisions taken in phase 0**

- `random_stream: anchor` lets a core pack draw from the anchor's row streams with the draw names a former coded
  generator used — needed for identical output. Non-core packs must use `own` (independent streams), so a
  generated pack can never correlate with or disturb core data.
- Pack YAML ships as package data (`pyproject.toml`), so the installed package and the Docker image carry it.
- Phase 0 supports member-anchored entities with zero or one row per member; more rows per anchor (key slots),
  other anchors, rules, recipes and API parts are phase 1.
