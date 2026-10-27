# API conventions (use-case packs)

- **Paths.** Exactly the paths the s8 payload lists for the use case, without the base path (`/v1`):
  `/members/{member_id}/<resource>` (list) and `/members/{member_id}/<resource>/{<item>}` (one row). The engine
  checks the member (404 MEMBER_NOT_FOUND) and a caregiver's `on_behalf_of` scope (403 SCOPE_NOT_AUTHORIZED etc.)
  before reading anything.
- **Tables.** An endpoint reads one table (`entity`), which must have a member column (`member_column`, usually
  member_id). It may be a pack entity or a core table that has member_id (e.g. auth_request, claim_header,
  plan_enrollment, pcp_assignment, care_gap). List every table read in `needs`. **There are no joins:** a field is
  `{name, column}` only. To show a related name (pharmacy, prescriber), the entity pack stores it as a column
  (filled with `lookup`, same draw as the id column).
- **Fields.** Name response fields for agents: snake_case, plain words (`due_date`, not `due_dt`). Map each to a
  real column of the table (core columns: `domain/entities.md`). Include the business identifier, dates, amounts
  and status; leave out internal ids except the item identifier.
- **Lists.** Order newest first (date column, desc). Offer a `status`-like filter with its allowed `values`
  (others return 422 INVALID_PARAMETER). Default limit 10-20, max 100. Add summaries an agent would otherwise
  compute: totals (`sum` with `where`), counts of exceptions (`count` with `where`).
- **Items.** `item: {param: <identifier>, column: <identifier column>}`; unknown items return 404 RESOURCE_NOT_FOUND.
- **Content.** `can` (3), `escalate` (2-3), `never` (2-3) for the agent SOP; `faqs` one list of 2-3 question
  variants per `can` item, as a member would ask; `verification` for members: member ID, date of birth, ZIP code.
  `handoff_queue` names the human team (UPPER_SNAKE). `caregiver_scope`: see the glossary.
- **Read only.** Phase 1 endpoints are GET only; actions (pay, change, cancel) are escalations, not endpoints.
- **Tests.** The engine generates four contract tests per endpoint (happy path, unknown member, no token, filter or
  limit / unknown item). They must pass in the sandbox, so every field must exist and the table must have rows.
