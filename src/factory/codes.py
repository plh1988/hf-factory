"""Domain code values shared by the data engine and the API engine.

Every value that generated rows and API logic compare against lives here, once. They are StrEnums, so a member
is also a plain str: it can be stored, compared with column values, passed to NumPy, DuckDB, Arrow and JSON
unchanged, and a typo becomes an AttributeError instead of a silently empty match.

Order matters where generators draw from a list of options: they list members explicitly, in the order the
spec's distributions give, never `list(Enum)`, so reordering a class can never change generated data.
"""

from __future__ import annotations

from enum import StrEnum


# ------------------------------------------------------------------ claims
class ClaimStatus(StrEnum):
    """claim_header.status; claim_line.line_status uses PAID, DENIED and PENDED."""

    RECEIVED = "RECEIVED"  # not yet adjudicated
    PENDED = "PENDED"
    DENIED = "DENIED"
    PAID = "PAID"
    ADJUSTED = "ADJUSTED"  # superseded by a provider-corrected replacement claim
    REPROCESSED = "REPROCESSED"  # superseded by a replacement after an overturned appeal


class ClaimType(StrEnum):
    PROFESSIONAL = "PROFESSIONAL"
    FACILITY = "FACILITY"
    PHARMACY = "PHARMACY"


class NetworkStatus(StrEnum):
    """claim_header.network_status; accumulators use IN and OON."""

    IN = "IN"
    OON = "OON"
    OON_EMERGENCY = "OON_EMERGENCY"


class DenialCategory(StrEnum):
    MED_NECESSITY = "MED_NECESSITY"
    CODING = "CODING"
    DUPLICATE = "DUPLICATE"
    TIMELY_FILING = "TIMELY_FILING"
    COB = "COB"
    NON_COVERED = "NON_COVERED"
    MISSING_INFO = "MISSING_INFO"
    AUTH = "AUTH"


class AppealStatus(StrEnum):
    OPEN = "OPEN"
    IN_REVIEW = "IN_REVIEW"
    DECIDED = "DECIDED"


class AppealOutcome(StrEnum):
    UPHELD = "UPHELD"
    OVERTURNED = "OVERTURNED"


# ------------------------------------------------------------------ authorizations
class AuthStatus(StrEnum):
    """auth_request.status; auth_determination.outcome uses APPROVED, PARTIAL and DENIED."""

    SUBMITTED = "SUBMITTED"
    IN_REVIEW = "IN_REVIEW"
    PENDED_INFO = "PENDED_INFO"  # waiting for documents
    APPROVED = "APPROVED"
    PARTIAL = "PARTIAL"
    DENIED = "DENIED"
    WITHDRAWN = "WITHDRAWN"


class AuthLineStatus(StrEnum):
    APPROVED = "APPROVED"
    MODIFIED = "MODIFIED"  # fewer units approved than requested (PARTIAL)
    DENIED = "DENIED"


class ServiceType(StrEnum):
    """auth_request.service_type."""

    OUTPATIENT = "OUTPATIENT"
    INPATIENT = "INPATIENT"
    DME = "DME"
    HOME_HEALTH = "HOME_HEALTH"
    SNF = "SNF"
    PHARMACY = "PHARMACY"
    BH = "BH"


# ------------------------------------------------------------------ members and plans
class Lob(StrEnum):
    """Line of business (plan.lob, member_demographics.lob)."""

    MEDICARE_ADV = "MEDICARE_ADV"
    COMMERCIAL = "COMMERCIAL"
    MEDICAID_MCO = "MEDICAID_MCO"


class EnrollmentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TERMED = "TERMED"
    PENDING = "PENDING"  # application in progress (scenario PEV-03); the API treats it as covered


class PcpAssignmentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


class GeoType(StrEnum):
    URBAN = "URBAN"
    SUBURBAN = "SUBURBAN"
    RURAL = "RURAL"


# ------------------------------------------------------------------ providers and network
class ProviderType(StrEnum):
    PCP = "PCP"
    SPECIALIST = "SPECIALIST"
    FACILITY = "FACILITY"


class ProviderStatus(StrEnum):
    ACTIVE = "ACTIVE"
    TERMED = "TERMED"
    SUSPENDED = "SUSPENDED"


class ProviderRole(StrEnum):
    """Provider pools claims and auths draw from: PCPs, specialists, and facilities by kind."""

    PCP = "PCP"
    SPECIALIST = "SPECIALIST"
    HOSPITAL = "HOSPITAL"
    POSTACUTE = "POSTACUTE"  # SNF and home health
    PHARMACY = "PHARMACY"


class FacilityType(StrEnum):
    HOSPITAL = "HOSPITAL"
    SNF = "SNF"
    HOME_HEALTH = "HOME_HEALTH"
    PHARMACY = "PHARMACY"


class ParticipationStatus(StrEnum):
    PARTICIPATING = "PARTICIPATING"
    NON_PARTICIPATING = "NON_PARTICIPATING"
    PENDING_ONBOARDING = "PENDING_ONBOARDING"
    TERMED = "TERMED"


class PanelStatus(StrEnum):
    OPEN = "OPEN"
    LIMITED = "LIMITED"
    CLOSED = "CLOSED"
