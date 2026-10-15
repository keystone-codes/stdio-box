"""Errors raised by the profile store."""


class StoreError(Exception):
    """Base class for store failures callers can handle."""


class StoreSchemaNewer(StoreError):
    """The database was written by a newer build. Nothing was written."""

    def __init__(self, store_version: int, code_version: int) -> None:
        self.store_version = store_version
        self.code_version = code_version
        super().__init__(
            f"store schema version {store_version} is newer than code version {code_version}"
        )


class SecretUnreadable(StoreError):
    """The secret row cannot be opened with the current key."""


class ProfileNotFound(StoreError):
    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id
        super().__init__(f"profile not found: {profile_id}")


class ProfileExists(StoreError):
    def __init__(self, profile_id: str) -> None:
        self.profile_id = profile_id
        super().__init__(f"profile already exists: {profile_id}")


class ProfileInUse(StoreError):
    def __init__(self, subjects: list[str]) -> None:
        self.subjects = list(subjects)
        super().__init__("profile is in use: " + ", ".join(self.subjects))


class BindingExists(StoreError):
    def __init__(self, current: str) -> None:
        self.current = current
        super().__init__(f"binding already exists for profile {current}")


class BindingNotFound(StoreError):
    def __init__(self, subject: str) -> None:
        self.subject = subject
        super().__init__(f"binding not found: {subject}")


class LinkNotFound(StoreError):
    """The form link is missing, expired, or already extinguished."""
