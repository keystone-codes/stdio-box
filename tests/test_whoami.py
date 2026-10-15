"""creds_whoami reports the choice and never refuses."""

from pathlib import Path

from tests.support.routing import ECHO_DIR, body, open_store, person, seed

from stdio_box.forward import render_whoami
from stdio_box.provider import load_provider
from stdio_box.resolver import resolve


def _who(store: object, provider: object, identity: object) -> dict:
    resolution = resolve(store, provider, identity)  # type: ignore[arg-type]
    result = render_whoami(provider, identity, resolution)  # type: ignore[arg-type]
    assert result.isError is False
    return body(result)


def test_each_tier_and_refusals(tmp_path: Path) -> None:
    provider = load_provider(ECHO_DIR)
    store = open_store(tmp_path)
    user_profile = seed(store, "user", "token-user-aaaa", "user:ann@example.com")
    role_profile = seed(store, "role", "token-role-aaaa", None)
    other_profile = seed(store, "other", "token-other-aaa", None)
    default_profile = seed(store, "shared", "token-shared-aa", "default")
    store.bind("echo", "role:writer", role_profile, "tester", position=1)
    store.bind("echo", "role:reader", other_profile, "tester", position=2)
    store.bind("echo", "user:svc:backend", user_profile, "tester")

    user = _who(store, provider, person("ann@example.com", roles=("writer", "reader")))
    assert user["tier"] == "user"
    assert user["profile"] == user_profile
    assert user["sub_type"] == "human"
    assert user["not_used"] == []
    assert user["would_refuse"] is None
    assert user["hint"] is None
    assert user["rbac"] == "enforce"
    assert "who" in user["tools"]

    role = _who(store, provider, person("other@example.com", roles=("writer", "reader")))
    assert role["tier"] == "role"
    assert role["profile"] == role_profile
    assert role["binding"]["subject"] == "role:writer"
    assert role["not_used"][0]["subject"] == "role:reader"
    assert role["not_used"][0]["profile"] == other_profile
    assert role["would_refuse"] is None
    assert role["hint"] is not None
    assert "role:writer" in role["hint"]
    assert "creds_bind role:reader" in role["hint"]
    assert "--replace --position 1" in role["hint"]

    shared = _who(store, provider, person("nobody@example.com", roles=()))
    assert shared["tier"] == "default"
    assert shared["profile"] == default_profile
    assert shared["not_used"] == []
    assert shared["hint"] is None

    service = _who(
        store,
        provider,
        person("svc:backend", roles=("writer",), sub_type="service"),
    )
    assert service["sub"] == "svc:backend"
    assert service["sub_type"] == "service"
    assert service["tier"] == "user"
    assert service["profile"] == user_profile

    store.unbind("echo", "default")
    missing = _who(store, provider, person("stranger@example.com", roles=()))
    assert missing["profile"] is None
    assert missing["would_refuse"] == "no_credentials_profile"
    assert missing["tools"] is None

    store.create_profile(
        "echo/empty", "echo", "token", {"ECHO_URL": "https://echo.invalid"}, None, "tester"
    )
    store.bind("echo", "user:empty@example.com", "echo/empty", "tester")
    waiting = _who(store, provider, person("empty@example.com", roles=()))
    assert waiting["profile"] == "echo/empty"
    assert waiting["would_refuse"] == "profile_needs_reentry"
    assert waiting["tier"] == "user"
    store.close()
