---
name: healthcare-capability-builder
description: Design capability packs for the healthcare synthetic-data factory from the names in stage payloads (entity names, use-case codes, endpoint paths, scenario ids). Use when a payload asks for something the engines do not support yet.
version: 0.2.0 (phase 1)
---

# Healthcare capability builder

You turn names the engines do not know (an entity `premium_invoice` in an s4 payload, a use case `MBP` with its
endpoints in an s8 payload) into **capability packs**: declarative specs the generic engines run. You never write
Python. The plan is in `docs/agentic-capability-layer.md`.

**Scope (phase 1).**
- `entity` packs: one table anchored on members (stage s4), zero or one row per member, or several with key slots;
  foreign keys and data rules.
- `use_case` packs: member-facing, read-only endpoints under `/members/{member_id}/...` (lists with filters,
  ordering, limits and summaries; single items), plus FAQ and agent-SOP content.
- Not yet: other anchors (claims, providers), writes, provider-facing use cases, new scenario recipes. If a name
  needs these, say so in the description instead of approximating.

## Workflow

1. **Read the names.** From each gap (entity, use case, endpoint path) decide what the capability is, which anchor it
   hangs off and how many rows a member has. Write every assumption into the pack `description`.
2. **Reuse before adding.** Read `references/` and `examples/`. Never redefine a column the anchor already has; read
   it with `{op: anchor, field: ...}`. Endpoints may read core tables as well as pack tables.
3. **Entity pack.** Columns with types and one building block each (`references/generators.md`); realistic US payer
   values in UPPER_SNAKE_CASE; foreign keys with `references`; rules that state what must always hold.
4. **Use-case pack.** One endpoint per path in the payload, with the exact path (without the base path), fields,
   filters and summaries an agent needs; can / escalate / never lists; FAQs (one list of variants per `can` item);
   `needs` lists every table the endpoints read.
5. **Emit** YAML that validates against `references/pack-schema.json`. Entity packs are published before the
   use-case packs that need them.

## Hard rules

- **Deterministic.** All randomness comes from building blocks with a `draw` name, used once per pack unless two
  values must match on purpose. Use `per: anchor` for values constant per member (a premium amount, an autopay flag).
- **No clock.** Dates come from `days_before_as_of`, `months_before_as_of` and `add_days`, never from today.
  Dates must not fall after the as-of date (for the current month use the first of the month or earlier).
- **Own random stream.** Leave `random_stream` at `own`; `anchor` is for core packs only.
- **Keys.** Primary key: a string column `{op: row_id}`. For `many` rows pick `slots` above any realistic count and
  never change it later (it is part of every row key).
- **Rules** are `SELECT count(*) FROM <table> ...` returning the number of violating rows; rule ids start with the
  family letter (R/T/F/C/P/A) and are unique, e.g. `F-PREMIUM-01`. No writes, no functions that read files.
- **Synthetic only.** No real people, NPIs, addresses or member data; packs hold specs, never rows.
- **Engine columns.** Do not add `is_synthetic`, `tenant_id` or `scenario_id`.
- **API.** Member endpoints check the member and caregiver scope automatically; set `caregiver_scope` to the scope a
  caregiver must hold (e.g. BILLING). Values agents filter on get `values` (others return 422).

## Output contract

```yaml
# entity pack
id: entity.<name>
version: 1.0.0
kind: entity
description: <what it is; assumptions made from the name>
entity:
  name: <name>
  anchor: member
  primary_key: <name>_id
  rows: {per_anchor: zero_or_one | many, when: <Expr>, count: <Expr>, slots: <int>}
  columns:
    - {name: <name>_id, type: string, gen: {op: row_id}}
    - {name: member_id, type: string, gen: {op: anchor, field: member_id}, references: {entity: member_demographics, column: member_id}}
rules:
  - {id: F-<NAME>-01, description: <what must hold>, sql: "SELECT count(*) FROM <name> WHERE <violation>"}
---
# use-case pack
id: uc.<CODE>
version: 1.0.0
kind: use_case
use_case:
  code: <CODE>
  name: <name>
  caregiver_scope: <SCOPE>
  personas: [...]; can: [...]; escalate: [...]; never: [...]; verification: [...]; handoff_queue: <QUEUE>
  faqs: [[<variants for can[0]>], ...]
  needs: [<tables>]
  endpoints:
    - {path: /members/{member_id}/<resource>, operation_id: <snake_case>, summary: ..., entity: <table>,
       fields: [{name: ..., column: ...}], query_params: [...], order_by: [...], aggregates: [...]}
```

## How packs are checked

Publishing runs gates in a sandbox: G1 static checks (schema, foreign-key targets, rule SQL, column names),
G2 a small run whose s7 quality gate must pass (your rules included), G3 identical output on another shard count,
G4 no other entity changes, G5 every generated contract test passes (four per endpoint). A failed gate leaves
nothing active; read its detail, fix the pack and publish again.

## References

| File | Content | Source |
|---|---|---|
| `references/pack-schema.json` | the pack format (JSON Schema), both kinds | generated from `factory/data_engine/packs/model.py` |
| `references/generators.md` | building blocks, anchor fields, column types, row modes | generated |
| `examples/sdoh_indicator.yaml` | core entity pack (zero or one row per member) | `factory/data_engine/packs/core/` |
| `examples/mbp/premium_invoice.yaml` | entity pack with many rows, per-member values, foreign keys, rules | `examples/capability-packs/mbp/` |
| `examples/mbp/uc_mbp.yaml` | use-case pack: list and item endpoints, filter, summaries | `examples/capability-packs/mbp/` |

Planned (phase 2): `data-conventions.md`, `api-conventions.md`, `domain/entities.md`, `domain/codes.md`,
`domain/errors.md`, `domain/standards.md`, `domain/use-cases.md`, `domain/rules.md`, `domain/glossary.md`.
Generated files are rebuilt with `python scripts/build_skill_refs.py`; a test fails when they are out of date.
