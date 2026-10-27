# Glossary: reading names

Payloads give names, never descriptions. Read them with these conventions; state every inference as an assumption
in the pack description. If a name cannot be read with reasonable confidence, say so (the build becomes
UNDERSPECIFIED) rather than guessing.

## Use-case codes

Three letters. The first letter is the audience: `M` member-facing (member or caregiver, verified with member ID +
date of birth + ZIP), `P` provider-facing (verified with NPI + TIN). The other two letters name the topic.

| Code | Meaning | Typical data |
|---|---|---|
| PCS | Provider claim status | claim_header, claim_line, claim_denial, claim_pend, claim_payment |
| PAS | Provider authorization status | auth_request, auth_line, auth_determination, documentation_requirement |
| PEV | Provider eligibility verification | member_demographics, plan_enrollment, plan |
| MBN | Member benefits navigation | plan, plan_enrollment, member_accumulator |
| MCI | Member claims inquiry (EOB) | claim_header, claim_line |
| MPS | Member provider search | provider, practice_location, network_participation |
| MPC | Member PCP change | pcp_assignment, provider |
| MAS | Member appointment scheduling | availability_schedule, provider |
| PAP | Provider appeals | appeal_record, claim_denial |
| PPI | Provider payment inquiry | claim_payment, claim_adjustment |
| MBP | Member billing and premiums | premium invoices and payments (pack: premium_invoice) |
| MPA | Member prior-authorization status | auth_request, auth_determination (core tables, member_id on auth_request) |
| MRX | Member pharmacy / prescriptions | the member's prescriptions: drug, NDC, tier, days supply, refills, pharmacy, copay |
| PON | Provider onboarding / network enrollment | network_participation (onboarding_stage), provider; provider-facing (not supported in phase 1) |

Other letters: B billing, C claims, P PCP or prior auth, S search, A appointments or appeals, X/RX pharmacy,
N network or benefits, ON onboarding, EV eligibility. Caregiver scopes in use: BENEFITS, CLAIMS, PCP_CHANGE,
APPOINTMENTS, BILLING, PA_STATUS; a new member use case picks one of these or a new UPPER_SNAKE scope.

## Entity names

- Prefix: `member_*` member-anchored; `claim_*` claim-level; `auth_*` authorization; `provider_*` provider; `plan_*` plan.
  Phase 1 packs are member-anchored, so a member-facing entity is anchored on members even without the prefix
  (`premium_invoice`, `prescription`).
- Suffix: `_header`/`_line` a document and its lines; `_record` one event per row; `_history` time series;
  `_schedule` recurring slots; `_indicator` a flag or screening result; `_assignment` a member-to-provider link;
  `_invoice` monthly bills (`many` rows, one per month); `_requirement` something requested.
- Plural nouns in endpoint paths name the table's rows: `/members/{member_id}/premiums` lists premium invoices,
  `/members/{member_id}/prescriptions` lists prescriptions, `/members/{member_id}/authorizations` lists auths.

## Endpoint paths

`/v1/members/{member_id}/<resource>` lists a member's rows; `/v1/members/{member_id}/<resource>/{<item>}` returns one,
where `<item>` names the business identifier column (e.g. `{invoice_number}`, `{auth_number}`, `{rx_number}`).

## Scenario ids

`<UC>-01` happy path, `-02`/`-03` edge cases, `-04` compliance or data quality. Phase 1 packs cannot add scenario
recipes; read a member's existing scenario facts with `{op: recipe, flag: ...}`.
