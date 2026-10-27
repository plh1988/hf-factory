# Healthcare standards (orientation for naming and values)

Synthetic data mirrors real payer concepts. Use these as vocabulary, never as real data.

| Concept | Standard | Factory entities / values |
|---|---|---|
| Eligibility inquiry / response | X12 270/271 | PEV: plan_enrollment, plan |
| Claim status inquiry / response | X12 276/277 | PCS: claim_header status (RECEIVED, PENDED, DENIED, PAID, ...) |
| Prior authorization | X12 278, FHIR ClaimResponse / Da Vinci PAS | auth_request, auth_line, auth_determination |
| Claims | X12 837P / 837I (professional / institutional), NCPDP D.0 (pharmacy) | claim_header, claim_line, pharmacy_claim_detail |
| Remittance / payment | X12 835 | claim_payment (EFT trace), claim_adjustment |
| Adjustment reasons | CARC (CO = provider liability, PR = patient responsibility), RARC | claim_adjustment.carc, claim_denial.carc |
| Premium billing | X12 820 (premium payment) | premium_invoice (pack) |
| Drugs | NDC (11 digits), formulary tiers 1-5, days supply 30/90 | pharmacy_claim_detail.ndc, formulary_tier |
| Providers | NPI (10 digits, Luhn), TIN (9 digits), NUCC taxonomy | provider.npi, provider.tin, taxonomy codes |
| Coverage, member | FHIR Coverage, Patient, RelatedPerson (caregivers) | plan_enrollment, member_demographics, family_relationship |
| Care gaps | HEDIS measures (BCS, COL, CBP, HBD, SPC, TRC) | care_gap.measure |
| Diagnoses / procedures | ICD-10-CM, CPT/HCPCS | primary_dx, cpt_hcpcs |

Identifiers in packs are synthetic and formatted, e.g. `INV-MBR-0000123-00`, `RX-MBR-0000123-01`; never real NPIs,
NDCs from a live formulary, or real names.
