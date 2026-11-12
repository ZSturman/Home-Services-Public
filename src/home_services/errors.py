"""Service errors with stable machine-readable codes.

Every API error is returned as
`{"error": {"code": ..., "message": ..., "details": {...}}}`. Codes are part
of the v1 contract; messages are for people and may change.
"""
from __future__ import annotations


class ServiceError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict | None = None, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code
        if status:
            self.status = status

    def as_dict(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class Unauthenticated(ServiceError):
    status, code = 401, "unauthenticated"


class Forbidden(ServiceError):
    status, code = 403, "forbidden"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class Conflict(ServiceError):
    status, code = 409, "conflict"


class CapabilityUnavailable(ServiceError):
    """The feature exists in the contract but this host cannot provide it.
    Never returned as an empty success."""
    status, code = 409, "capability_unavailable"


class VolumeUnavailable(ServiceError):
    """A required volume is missing, unmounted, or the wrong disk. Writes
    fail closed; nothing falls back to system storage."""
    status, code = 503, "volume_unavailable"


class InsufficientSpace(ServiceError):
    status, code = 507, "insufficient_space"


class PathNotAllowed(ServiceError):
    status, code = 403, "path_not_allowed"


class ContractVersionUnsupported(ServiceError):
    status, code = 400, "contract_version_unsupported"
