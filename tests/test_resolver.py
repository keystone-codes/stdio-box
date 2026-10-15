"""Profile choice: user, ordered roles, default. Nothing is cached."""

import sqlite3
from pathlib import Path

from tests.support.routing import ECHO_DIR, open_store, person, seed

from stdio_box.provider import load_provider
from stdio_box.refusals import NO_CREDENTIALS_PROFILE, PROFILE_NEEDS_REENTRY
from stdio_box.resolver import resolve


def test_user_role_and_default_table(tmp_path: Path) -> None:
    provider = load_provider(ECHO_DIR)
    store = open_store(tmp_path)
    user_profile = seed(store, "user", "token-user-aaaa", "user:Ann@Example.com")
    role_profile = seed(store, "role", "token-role-aaaa", None)
    other_profile = seed(store, "other", "token-other-aaa", None)
    default_profile = seed(store, "shared", "token-shared-aa", "default")
    store.bind("echo", "role:writer", role_profile, "tester", position=1)
    store.bind("echo", "role:reader", role_profile, "tester", position=2)

    only_user = resolve(store, provider, person("Ann@Example.com", roles=("writer",)))
    assert only_user.profile_id == user_profile
    assert only_user.tier == "user"
    assert only_user.binding is not None
    assert only_user.binding.subject == "user:ann@example.com"
    assert only_user.refusal is None

    service = resolve(
        store,
        provider,
        person("svc:backend", roles=(), sub_type="service", rbac="enforce"),
    )
    # No user:svc:backend binding yet: fall through. Bind it and resolve again.
    assert service.tier == "default"
    store.bind("echo", "user:svc:backend", user_profile, "tester", replace=True)
    service = resolve(
        store,
        provider,
        person("svc:backend", roles=("writer",), sub_type="service"),
    )
    assert service.tier == "user"
    assert service.profile_id == user_profile
    assert service.binding is not None
    assert service.binding.subject == "user:svc:backend"

    only_role = resolve(store, provider, person("other@example.com", roles=("reader",)))
    assert only_role.tier == "role"
    assert only_role.profile_id == role_profile
    assert only_role.binding is not None
    assert only_role.binding.subject == "role:reader"
    assert only_role.not_used == ()

    two_roles_one_profile = resolve(
        store, provider, person("other@example.com", roles=("reader", "writer"))
    )
    assert two_roles_one_profile.profile_id == role_profile
    assert two_roles_one_profile.binding is not None
    assert two_roles_one_profile.binding.subject == "role:writer"
    assert [item.subject for item in two_roles_one_profile.not_used] == ["role:reader"]
    assert two_roles_one_profile.refusal is None

    store.bind("echo", "role:reader", other_profile, "tester", replace=True, position=2)
    two_profiles = resolve(store, provider, person("other@example.com", roles=("reader", "writer")))
    assert two_profiles.profile_id == role_profile
    assert two_profiles.binding is not None
    assert two_profiles.binding.position == 1
    assert [(item.subject, item.profile_id) for item in two_profiles.not_used] == [
        ("role:reader", other_profile)
    ]

    store.bind("echo", "role:reader", other_profile, "tester", replace=True, position=1)
    reordered = resolve(store, provider, person("other@example.com", roles=("reader", "writer")))
    assert reordered.profile_id == other_profile
    assert reordered.binding is not None
    assert reordered.binding.subject == "role:reader"
    assert [item.subject for item in reordered.not_used] == ["role:writer"]

    off = resolve(
        store,
        provider,
        person("nobody@example.com", roles=("writer", "reader"), rbac="off"),
    )
    assert off.tier == "default"
    assert off.profile_id == default_profile
    assert off.refusal is None

    store.close()


def test_needs_reentry_does_not_fall_through(tmp_path: Path) -> None:
    provider = load_provider(ECHO_DIR)
    store = open_store(tmp_path)
    first = seed(store, "first", "token-first-aaa", None)
    second = seed(store, "second", "token-second-aa", None)
    store.bind("echo", "role:first", first, "tester", position=1)
    store.bind("echo", "role:second", second, "tester", position=2)
    raw = sqlite3.connect(store.path)
    raw.execute(
        "UPDATE profile_secrets SET key_id = ? WHERE profile_id = ?",
        ("spoiled", first),
    )
    raw.commit()
    raw.close()

    resolution = resolve(store, provider, person("person@example.com", roles=("first", "second")))
    assert resolution.profile_id == first
    assert resolution.refusal is not None
    assert resolution.refusal.code == PROFILE_NEEDS_REENTRY
    assert second not in (resolution.profile_id or "")
    assert [item.profile_id for item in resolution.not_used] == [second]
    store.close()


def test_awaiting_secrets_message_and_no_account(tmp_path: Path) -> None:
    provider = load_provider(ECHO_DIR)
    store = open_store(tmp_path)
    store.create_profile(
        "echo/empty", "echo", "token", {"ECHO_URL": "https://echo.invalid"}, None, "tester"
    )
    store.bind("echo", "role:empty", "echo/empty", "tester")
    waiting = resolve(store, provider, person("person@example.com", roles=("empty", "other")))
    assert waiting.profile_id == "echo/empty"
    assert waiting.refusal is not None
    assert waiting.refusal.code == PROFILE_NEEDS_REENTRY
    assert waiting.refusal.message == "The account echo/empty has no secrets yet."

    missing = resolve(store, provider, person("stranger@example.com", roles=()))
    assert missing.profile_id is None
    assert missing.refusal is not None
    assert missing.refusal.code == NO_CREDENTIALS_PROFILE
    assert missing.refusal.message == (
        "There is no Echo test server account for you in this box. "
        "Ask an administrator to bind one to you or to your role."
    )
    store.close()
