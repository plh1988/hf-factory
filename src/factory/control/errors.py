"""Rejection codes of the control plane: each code has exactly one HTTP status."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class RejectCode(StrEnum):
    # 403
    USE_SECRET_MANAGER = "USE_SECRET_MANAGER"  # credentials are not served by the cloud profile
    # 404
    NO_RUN = "NO_RUN"
    NO_MANIFEST = "NO_MANIFEST"
    NO_REPORT = "NO_REPORT"
    NO_TABLE = "NO_TABLE"
    NOT_DEPLOYED = "NOT_DEPLOYED"
    # 409 order and consistency
    STAGE_ORDER = "STAGE_ORDER"
    RUN_SETTINGS_DIFFER = "RUN_SETTINGS_DIFFER"
    PAYLOAD_DIFFERS_FROM_RUN = "PAYLOAD_DIFFERS_FROM_RUN"
    ENDPOINTS_DIFFER = "ENDPOINTS_DIFFER"
    ENTITY_STAGE = "ENTITY_STAGE"
    MISSING_UPSTREAM_DATA = "MISSING_UPSTREAM_DATA"
    NO_PLAN_FOR_LOB = "NO_PLAN_FOR_LOB"
    DATA_MISSING_FOR_USE_CASE = "DATA_MISSING_FOR_USE_CASE"
    DATA_QUALITY_GATE_FAILED = "DATA_QUALITY_GATE_FAILED"
    # 413
    PAYLOAD_TOO_LARGE_FOR_PROFILE = "PAYLOAD_TOO_LARGE_FOR_PROFILE"
    # 422 payload content
    TENANT_REQUIRED = "TENANT_REQUIRED"
    WRONG_STAGE = "WRONG_STAGE"
    WRONG_JOB = "WRONG_JOB"
    MISSING_RUN_SETTINGS = "MISSING_RUN_SETTINGS"
    BAD_STAGE_SEED = "BAD_STAGE_SEED"
    BAD_VOLUME = "BAD_VOLUME"
    BAD_BASE_PATH = "BAD_BASE_PATH"
    VOLUME_REQUIRED = "VOLUME_REQUIRED"
    LOB_MIX_REQUIRED = "LOB_MIX_REQUIRED"
    ENTITIES_REQUIRED = "ENTITIES_REQUIRED"
    UNKNOWN_ENTITIES = "UNKNOWN_ENTITIES"
    ENTITIES_NOT_IMPLEMENTED = "ENTITIES_NOT_IMPLEMENTED"
    UNKNOWN_RULE_SETS = "UNKNOWN_RULE_SETS"
    UNSUPPORTED_AUTH = "UNSUPPORTED_AUTH"
    UNSUPPORTED_CATALOG = "UNSUPPORTED_CATALOG"
    UNSUPPORTED_ID_STRATEGY = "UNSUPPORTED_ID_STRATEGY"
    USE_CASES_NOT_IMPLEMENTED = "USE_CASES_NOT_IMPLEMENTED"
    # 500
    STAGE_FAILED = "STAGE_FAILED"

    @property
    def status(self) -> int:
        return _STATUS[self]


_GROUPS = {
    403: ["USE_SECRET_MANAGER"],
    404: ["NO_RUN", "NO_MANIFEST", "NO_REPORT", "NO_TABLE", "NOT_DEPLOYED"],
    409: ["STAGE_ORDER", "RUN_SETTINGS_DIFFER", "PAYLOAD_DIFFERS_FROM_RUN", "ENDPOINTS_DIFFER", "ENTITY_STAGE", "MISSING_UPSTREAM_DATA",
          "NO_PLAN_FOR_LOB", "DATA_MISSING_FOR_USE_CASE", "DATA_QUALITY_GATE_FAILED"],
    413: ["PAYLOAD_TOO_LARGE_FOR_PROFILE"],
    500: ["STAGE_FAILED"],
}
_STATUS = {c: next((s for s, names in _GROUPS.items() if c.name in names), 422) for c in RejectCode}


class JobRejected(Exception):
    """A payload the control plane refuses; becomes an HTTP error with a code (e.g. STAGE_ORDER) and details."""

    def __init__(self, code: RejectCode, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.details = code.status, code, message, details
