"""SQLite profile store: migrations, encryption, ordered bindings, audit."""

from stdio_box.store.errors import (
    BindingExists,
    BindingNotFound,
    LinkNotFound,
    ProfileExists,
    ProfileInUse,
    ProfileNotFound,
    SecretUnreadable,
    StoreError,
    StoreSchemaNewer,
)
from stdio_box.store.repo import (
    Binding,
    Bindings,
    BindResult,
    FormLink,
    Match,
    Profile,
    Store,
    ToolSnapshot,
)

__all__ = [
    "BindResult",
    "Binding",
    "BindingExists",
    "BindingNotFound",
    "Bindings",
    "FormLink",
    "LinkNotFound",
    "Match",
    "Profile",
    "ProfileExists",
    "ProfileInUse",
    "ProfileNotFound",
    "SecretUnreadable",
    "Store",
    "StoreError",
    "StoreSchemaNewer",
    "ToolSnapshot",
]
