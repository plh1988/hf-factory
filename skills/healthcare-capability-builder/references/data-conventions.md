# Data conventions (entity packs)

- **Anchor.** Phase 1: `anchor: member`, generated in stage s4. Read member facts with `{op: anchor, field: ...}`
  (member_id, member_number, lob, plan_id, sex, premium_status). lob is MEDICARE_ADV, COMMERCIAL or MEDICAID_MCO;
  premium_status is PAID, DUE, PAST_DUE, or NA for Medicaid.
- **Rows.** `zero_or_one` for one fact per member (a screening, a preference); `many` for repeated facts (monthly
  invoices, prescriptions, events). With `many`, set `count` (constant or drawn) and `slots` larger than any count;
  never change `slots` later. Use `rows.when` to skip members the fact does not apply to.
- **Keys and numbers.** Primary key `<entity>_id` = `{op: row_id}`. Business identifiers come from `format`,
  e.g. `"INV-{member_number}-{index:02d}"`, `"RX-{member_number}-{index:02d}"`. Template fields are only anchor
  fields, earlier columns and `index`; never invent placeholders. Random codes (an 11-digit NDC, a reference number)
  come from `{op: digits, draw: ndc, length: 11}`, as a column of their own or an earlier column used by `format`. Foreign keys use `references` and
  must point at the primary key of the target (member_id -> member_demographics.member_id, plan_id -> plan.plan_id).
  To reference rows of a reference table (a pharmacy, a prescriber, a plan), pick them with `lookup`, e.g.
  `{op: lookup, draw: pharmacy, table: provider, column: npi, where: {facility_type: [PHARMACY]}}` or
  `where: {provider_type: [PCP, SPECIALIST]}` for prescribers. Only tables of earlier stages can be looked up (s3:
  plan, provider, network_participation, practice_location, ...). Never invent ids for a foreign key. Lookups with the
  same draw, table and where pick the same row: use that to store a name next to its id (pharmacy_npi with
  `column: npi` and pharmacy_name with `column: org_name`, both `draw: pharmacy`), because endpoints cannot join.
- **Money.** Column type `money`, building block `money` with dollar bounds (integer cents internally). To vary an amount
  by category (tier, LOB), map the bounds: `low: {op: map, of: ..., values: {"1": 5, "2": 15}}` (map values are
  constants, never expressions). A value
  that must equal another column uses `{op: column}`; amounts never negative.
- **Dates.** Relative to the run's as-of date only: `days_before_as_of`, `months_before_as_of` (first of a month;
  `{op: row_index}` gives one month per row) and `add_days`. Never produce dates after the as-of date for things that
  already happened; due dates may be in the near future.
- **Realism.** Weighted choices with realistic US payer proportions; most rows ordinary, a few exceptions
  (e.g. 3-5% past due). Values constant for a member use `per: anchor`.
- **Scenarios.** Rows inherit the member's scenario tag. To force a fact for a scenario, test its recipe flag.
- **Rules.** State what must always hold, as violations: `SELECT count(*) FROM <entity> WHERE <bad>`.
  Typical: amounts consistent (paid <= due), dates ordered, status and dates agree, no rows for members the
  fact does not apply to. Ids: `<F|T|R|C>-<ENTITY>-NN`. A rule that fails in the sandbox fails the pack: only state
  rules the generator guarantees. Write `AS_OF_DATE` for the run's as-of date (`WHERE fill_dt > AS_OF_DATE`); never
  CURRENT_DATE or now().
- **When something cannot be expressed.** If the building blocks cannot produce a column the way you want, simplify
  it (a plain value, or drop the column) and say so in the description. Never loop over the same problem.
- **Size.** Keep packs small and coherent: 6-12 columns, 1-3 rules.
