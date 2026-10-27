---
name: healthcare-capability-builder
description: Design capability packs for the healthcare synthetic-data factory from the names in a stage payload (entity names, scenario ids, use-case codes, endpoint paths). Use when a payload asks for something the engines do not support yet.
version: 0.1.0 (phase 0)
---

# Healthcare capability builder

You turn a name the engines do not know (for example an entity `premium_invoice` in an s4 payload) into a
**capability pack**: a declarative spec the generic engines run. You never write Python. The plan is in
`docs/agentic-capability-layer.md`.

**Phase 0 scope.** Packs can declare one entity anchored on members (stage s4), with zero or one row per member.
Rows per anchor > 1, other anchors (claims, providers), rules, scenario recipes and API endpoints arrive in
later phases. If a name needs more than phase 0 supports, say so instead of approximating.

## Workflow

1. **Read the name.** Decide what the entity is, which anchor it hangs off (phase 0: `member`) and how many rows
   a member has. State every assumption.
2. **Reuse before adding.** Check `references/` and the examples; never redefine a column the anchor already has
   (read it with `{op: anchor, field: ...}`).
3. **Design columns.** Name, type and one building block per column (`references/generators.md`). Realistic
   value sets and weights for US healthcare payers; code values in UPPER_SNAKE_CASE.
4. **Use scenarios.** Where a member scenario should force a fact, test its recipe flag with
   `{op: recipe, flag: ...}` (flags are fields of `Scenario` in `factory/data_engine/scenarios.py`).
5. **Emit the pack** as YAML that validates against `references/pack-schema.json`.

## Hard rules

- **Deterministic.** All randomness comes from building blocks with a `draw` name. Each draw name is used once
  per pack, unless two columns must share the same random value on purpose.
- **No clock.** Dates come from `days_before_as_of` (relative to the run's as-of date), never from today.
- **Own random stream.** Leave `random_stream` at `own`. `anchor` is reserved for core packs that reproduce a
  former coded generator.
- **Primary key** is a string column with `{op: row_id}`, named `<entity>_id` or matching the catalog.
- **Synthetic only.** No real names, NPIs, addresses or member data in a pack; packs hold specs, never rows.
- **Do not add** `is_synthetic`, `tenant_id` or `scenario_id`: the engine stamps them.

## Output contract

```yaml
id: entity.<name>          # must match entity.name
version: 1.0.0
kind: entity
description: <one paragraph: what the entity is, and the assumptions you made from its name>
entity:
  name: <name>
  anchor: member
  primary_key: <name>_id
  rows: {per_anchor: zero_or_one, when: <Expr or omit for every member>}
  columns:
    - {name: <name>_id, type: string, gen: {op: row_id}}
    - {name: member_id, type: string, gen: {op: anchor, field: member_id}}
    - ...
```

## References

| File | Content | Source |
|---|---|---|
| `references/pack-schema.json` | the pack format (JSON Schema) | generated from `factory/data_engine/packs/model.py` |
| `references/generators.md` | building blocks and anchor fields | generated |
| `examples/sdoh_indicator.yaml` | a complete core pack (SDOH screening) | copied from `factory/data_engine/packs/core/` |

Planned (phase 2): `data-conventions.md`, `api-conventions.md`, `domain/entities.md`, `domain/codes.md`,
`domain/errors.md`, `domain/standards.md`, `domain/use-cases.md`, `domain/rules.md`, `domain/glossary.md`.
Generated files are rebuilt with `python scripts/build_skill_refs.py`; a test fails when they are out of date.
