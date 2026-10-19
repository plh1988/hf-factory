"""Error codes of the tenant business API: each code has exactly one HTTP status.

Endpoints raise `error(ErrorCode.X, message)`; the body is always {error, message, is_synthetic}. Agents branch
on `error`, so codes are part of the API contract: add new ones here, never rename existing ones.
"""

from __future__ import annotations

from enum import StrEnum

from fastapi import HTTPException


class ErrorCode(StrEnum):
    # 401 authentication
    INVALID_CLIENT = "INVALID_CLIENT"
    INVALID_TOKEN = "INVALID_TOKEN"
    # 403 identity and scope
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    SCOPE_NOT_AUTHORIZED = "SCOPE_NOT_AUTHORIZED"
    CAREGIVER_NOT_VERIFIED = "CAREGIVER_NOT_VERIFIED"
    CONSENT_EXPIRED = "CONSENT_EXPIRED"
    # 404 not found
    MEMBER_NOT_FOUND = "MEMBER_NOT_FOUND"
    CLAIM_NOT_FOUND = "CLAIM_NOT_FOUND"
    AUTH_NOT_FOUND = "AUTH_NOT_FOUND"
    APPEAL_NOT_FOUND = "APPEAL_NOT_FOUND"
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    NO_COVERAGE = "NO_COVERAGE"
    # 409 business rule
    NO_ACTIVE_COVERAGE = "NO_ACTIVE_COVERAGE"
    NOT_PARTICIPATING = "NOT_PARTICIPATING"
    NOT_A_PCP = "NOT_A_PCP"
    ALREADY_PCP = "ALREADY_PCP"
    PANEL_CLOSED = "PANEL_CLOSED"
    OUTSIDE_ACCESS_STANDARD = "OUTSIDE_ACCESS_STANDARD"
    SLOT_UNAVAILABLE = "SLOT_UNAVAILABLE"
    ALREADY_BOOKED = "ALREADY_BOOKED"
    NOT_DENIED = "NOT_DENIED"
    NOT_APPEALABLE = "NOT_APPEALABLE"
    APPEAL_EXISTS = "APPEAL_EXISTS"
    DEADLINE_PASSED = "DEADLINE_PASSED"
    DOCUMENT_NOT_REQUESTED = "DOCUMENT_NOT_REQUESTED"
    # 422 request
    MEMBER_IDENTIFIERS_REQUIRED = "MEMBER_IDENTIFIERS_REQUIRED"
    LOCATION_REQUIRED = "LOCATION_REQUIRED"
    UNKNOWN_SPECIALTY = "UNKNOWN_SPECIALTY"
    # 429 limits
    RATE_LIMITED = "RATE_LIMITED"
    MINIMUM_NECESSARY = "MINIMUM_NECESSARY"  # too many distinct members in a short window
    # 503 fault injection
    INJECTED_FAULT = "INJECTED_FAULT"

    @property
    def status(self) -> int:
        return _STATUS[self]


_STATUS = {
    **dict.fromkeys([ErrorCode.INVALID_CLIENT, ErrorCode.INVALID_TOKEN], 401),
    **dict.fromkeys([ErrorCode.VERIFICATION_FAILED, ErrorCode.SCOPE_NOT_AUTHORIZED, ErrorCode.CAREGIVER_NOT_VERIFIED,
                     ErrorCode.CONSENT_EXPIRED], 403),
    **dict.fromkeys([ErrorCode.MEMBER_NOT_FOUND, ErrorCode.CLAIM_NOT_FOUND, ErrorCode.AUTH_NOT_FOUND, ErrorCode.APPEAL_NOT_FOUND,
                     ErrorCode.PROVIDER_NOT_FOUND, ErrorCode.NO_COVERAGE], 404),
    **dict.fromkeys([ErrorCode.NO_ACTIVE_COVERAGE, ErrorCode.NOT_PARTICIPATING, ErrorCode.NOT_A_PCP, ErrorCode.ALREADY_PCP,
                     ErrorCode.PANEL_CLOSED, ErrorCode.OUTSIDE_ACCESS_STANDARD, ErrorCode.SLOT_UNAVAILABLE, ErrorCode.ALREADY_BOOKED,
                     ErrorCode.NOT_DENIED, ErrorCode.NOT_APPEALABLE, ErrorCode.APPEAL_EXISTS, ErrorCode.DEADLINE_PASSED,
                     ErrorCode.DOCUMENT_NOT_REQUESTED], 409),
    **dict.fromkeys([ErrorCode.MEMBER_IDENTIFIERS_REQUIRED, ErrorCode.LOCATION_REQUIRED, ErrorCode.UNKNOWN_SPECIALTY], 422),
    **dict.fromkeys([ErrorCode.RATE_LIMITED, ErrorCode.MINIMUM_NECESSARY], 429),
    ErrorCode.INJECTED_FAULT: 503,
}
assert set(_STATUS) == set(ErrorCode), "every error code needs a status"


def error_body(code: ErrorCode, message: str) -> dict:
    """The API's error shape."""
    return {"error": code, "message": message, "is_synthetic": True}


def error(code: ErrorCode, message: str) -> HTTPException:
    """Exception for an error response; the status comes from the code."""
    return HTTPException(status_code=code.status, detail=error_body(code, message))
