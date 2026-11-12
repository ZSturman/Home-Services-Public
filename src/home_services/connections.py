"""Home Services adapter for the reusable protocol-1 connection engine."""
from . import errors
from .auth import PRESETS, SCOPES
from .pairing_core import ConnectionStore, base_url, INVITE_SECONDS, PROTOCOL

class Connections(ConnectionStore):
    ServiceError = errors.ServiceError
    Unauthenticated = errors.Unauthenticated
    Forbidden = errors.Forbidden
    NotFound = errors.NotFound
    Conflict = errors.Conflict
    def __init__(self, services):
        self.development = services.config.environment != "production"
        super().__init__(services, PRESETS, SCOPES)
    def camera_choices(self):
        return [{"id": d.id, "name": d.name} for d in self.s.config.cameras.devices]

    def _validate_preferences(self, preferences: dict, allowed: set[str]):
        if not set(preferences) <= {"upload_folder", "camera_id"}:
            raise self.ServiceError("unknown connection preference", code="invalid_connection_preferences")
        if any(not isinstance(value, str) for value in preferences.values()):
            raise self.ServiceError("connection preferences must be strings; omit a key to clear it", code="invalid_connection_preferences")
        folder = preferences.get("upload_folder")
        if folder is not None:
            if "transfers:write" not in allowed:
                raise self.Forbidden("this app has no file upload permission")
            # Validate against the actual guarded files root; never create it here.
            if not isinstance(folder, str) or len(folder) > 512 or "\\" in folder:
                raise self.ServiceError("choose a relative upload folder", code="invalid_connection_preferences")
            self.s.storage.list(folder, limit=1)
        camera = preferences.get("camera_id")
        if camera is not None:
            if not isinstance(camera, str) or "cameras:read" not in allowed or camera not in {d.id for d in self.s.config.cameras.devices}:
                raise self.ServiceError("choose a camera available to this app", code="invalid_connection_preferences")

