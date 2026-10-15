"""Choose the profile for one call. Nothing here is cached."""

import math

from stdio_box._vendor.keystone_identity import KeystoneIdentity
from stdio_box.provider import Provider
from stdio_box.refusals import (
    NO_CREDENTIALS_PROFILE,
    PROFILE_NEEDS_REENTRY,
    Refusal,
)
from stdio_box.store.repo import Binding, Profile, Store


def no_credentials_refusal(provider: Provider) -> Refusal:
    title = provider.title
    return Refusal(
        NO_CREDENTIALS_PROFILE,
        (
            f"There is no {title} account for you in this box. "
            "Ask an administrator to bind one to you or to your role."
        ),
        "Ask an administrator to bind a profile to you or to your role.",
    )


def reentry_refusal(profile: Profile) -> Refusal:
    if profile.state == "awaiting_secrets":
        message = f"The account {profile.id} has no secrets yet."
        hint = "Enter the account secrets, then retry the call."
    else:
        message = f"The account {profile.id} needs its secrets entered again."
        hint = "Enter the account secrets again, then retry the call."
    return Refusal(PROFILE_NEEDS_REENTRY, message, hint)


class Resolution:
    """The profile chosen for this call, or the refusal that stops it.

    ``not_used`` lists role bindings that also matched the caller and sit
    lower in the administrator's order. Profile state does not change which
    binding wins: a profile that needs its secrets again is still the result,
    and the call is refused instead of falling through.
    """

    def __init__(
        self,
        profile_id: str | None,
        tier: str | None,
        binding: Binding | None,
        not_used: tuple[Binding, ...],
        refusal: Refusal | None,
        profile: Profile | None,
    ) -> None:
        self.profile_id = profile_id
        self.tier = tier
        self.binding = binding
        self.not_used = not_used
        self.refusal = refusal
        self.profile = profile


def resolve(store: Store, provider: Provider, identity: KeystoneIdentity) -> Resolution:
    """Apply user, then ordered roles, then default. Roles are read once, here."""
    roles: tuple[str, ...] = ()
    if identity.rbac != "off":
        roles = identity.roles
    match = store.lookup(provider.name, identity.sub, roles)
    if match is None:
        return Resolution(None, None, None, (), no_credentials_refusal(provider), None)
    refusal = None
    if match.profile.state in {"needs_reentry", "awaiting_secrets"}:
        refusal = reentry_refusal(match.profile)
    return Resolution(
        match.profile.id,
        match.tier,
        match.binding,
        tuple(match.not_used),
        refusal,
        match.profile,
    )


def retry_after_seconds(remaining: float) -> int:
    """Whole seconds a caller should wait. Any positive remainder rounds up."""
    if remaining <= 0:
        return 0
    return max(1, math.ceil(remaining))
