"""Schema and semantic checks for provider directories."""

import shutil
from pathlib import Path

import pytest
import yaml

from stdio_box.provider import ProviderError, load_provider

ECHO_DIR = Path(__file__).resolve().parent / "data" / "packages" / "echo"
ATLASSIAN_DIR = Path(__file__).resolve().parent / "data" / "packages" / "atlassian"


def test_echo_and_atlassian_are_valid() -> None:
    echo = load_provider(ECHO_DIR)
    assert echo.name == "echo"
    assert echo.directory.name == "echo"
    assert echo.identity_to_child == "none"
    assert [variant.name for variant in echo.variants] == ["token", "full"]

    atlassian = load_provider(ATLASSIAN_DIR)
    assert atlassian.name == "atlassian"
    assert atlassian.identity_to_child == "none"
    assert len(atlassian.variants) == 6
    cloud = atlassian.variants[0]
    assert cloud.name == "cloud"
    assert len(cloud.checks) == 2
    assert cloud.checks[0].http is not None
    assert cloud.checks[0].http.auth is not None
    assert cloud.checks[0].http.auth.basic == ("JIRA_USERNAME", "JIRA_API_TOKEN")


def test_name_must_match_directory(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path, "other")
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("must equal directory name 'other'" in message for message in exc.value.messages)


def test_requirements_lock_requires_uvx(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["server"]["command"] = "npx echo-server@0.0.0"
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any(
        "requires a command starting with 'uvx'" in message for message in exc.value.messages
    )


def test_package_lock_requires_npx(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["server"]["lock"] = "package-lock.json"
    (dest / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (dest / "package.json").write_text("{}\n", encoding="utf-8")
    (dest / "requirements.txt").unlink()
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any(
        "requires a command starting with 'npx'" in message for message in exc.value.messages
    )


def test_package_lock_requires_package_json(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["server"]["command"] = "npx echo-server@0.0.0"
    data["server"]["lock"] = "package-lock.json"
    (dest / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (dest / "requirements.txt").unlink()
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("package.json must exist" in message for message in exc.value.messages)


def test_lock_file_must_exist(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    (dest / "requirements.txt").unlink()
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("does not exist" in message for message in exc.value.messages)


def test_duplicate_field_names(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["fields"][1]["name"] = "ECHO_URL"
    data["variants"][0]["fields"] = ["ECHO_URL"]
    data["variants"][1]["fields"] = ["ECHO_URL", "ECHO_EXTRA"]
    data["discovery"]["env"] = {"ECHO_URL": "https://echo.invalid"}
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("duplicate field name 'ECHO_URL'" in message for message in exc.value.messages)


def test_duplicate_variant_names(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["variants"][1]["name"] = "token"
    data["variants"][1]["fields"] = ["ECHO_URL", "ECHO_EXTRA"]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("duplicate variant name 'token'" in message for message in exc.value.messages)


def test_variant_field_must_exist(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["variants"][0]["fields"] = ["ECHO_URL", "NO_SUCH"]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("unknown field 'NO_SUCH'" in message for message in exc.value.messages)


def test_check_field_must_belong_to_variant(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["variants"][0]["checks"] = [
        {
            "http": {
                "method": "GET",
                "url": "{ECHO_EXTRA}",
                "expect_status": [200],
            }
        }
    ]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("is not part of variant 'token'" in message for message in exc.value.messages)


def test_interpolation_rejects_secret_fields(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["variants"][0]["checks"] = [
        {
            "http": {
                "method": "GET",
                "url": "https://echo.invalid/{ECHO_TOKEN}",
                "expect_status": [200],
            }
        }
    ]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any(
        "secret field 'ECHO_TOKEN' cannot be interpolated" in message
        for message in exc.value.messages
    )


def test_auth_credential_must_be_secret(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["variants"][0]["checks"] = [
        {
            "http": {
                "method": "GET",
                "url": "{ECHO_URL}/who",
                "auth": {"bearer": "ECHO_URL"},
                "expect_status": [200],
            }
        }
    ]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("must be a secret" in message for message in exc.value.messages)


def test_secret_field_cannot_be_an_argument(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["fields"][1]["to"] = {"arg": "--token"}
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any(
        "secret field ECHO_TOKEN cannot be passed as an argument" in message
        for message in exc.value.messages
    )


def test_allowed_hosts_require_url_field(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["variants"].append(
        {
            "name": "secret_only",
            "fields": ["ECHO_TOKEN"],
            "allowed_hosts": ["*.invalid"],
            "checks": [{"tool": {"name": "who"}}],
        }
    )
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any(
        "allowed_hosts requires a field of type url" in message for message in exc.value.messages
    )


def test_variants_must_not_share_field_set(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["variants"][1]["fields"] = ["ECHO_TOKEN", "ECHO_URL"]
    data["variants"][1]["tools"] = ["who"]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any(
        "has the same fields as variant 'token'" in message for message in exc.value.messages
    )


def test_placeholder_discovery_must_cover_a_variant(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    del data["discovery"]["env"]["ECHO_TOKEN"]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("at least one variant" in message for message in exc.value.messages)


def test_placeholder_url_host_must_end_with_invalid(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    data["discovery"]["env"]["ECHO_URL"] = "https://example.com"
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("host must end with .invalid" in message for message in exc.value.messages)


@pytest.mark.parametrize(
    "mutate",
    ["root", "server", "field", "variant", "tool", "discovery", "tools"],
)
def test_extra_property_rejected(tmp_path: Path, mutate: str) -> None:
    dest, data = _copy_echo(tmp_path)
    if mutate == "root":
        data["nope"] = True
    elif mutate == "server":
        data["server"]["nope"] = True
    elif mutate == "field":
        data["fields"][0]["nope"] = True
    elif mutate == "variant":
        data["variants"][0]["nope"] = True
    elif mutate == "tool":
        data["variants"][0]["checks"][0]["tool"]["nope"] = True
    elif mutate == "discovery":
        data["discovery"]["nope"] = True
    else:
        data["tools"] = {"normalize": "none", "nope": True}
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("Additional properties" in message for message in exc.value.messages)


def test_extra_property_on_http_check_rejected(tmp_path: Path) -> None:
    dest = tmp_path / "atlassian"
    shutil.copytree(ATLASSIAN_DIR, dest)
    data = yaml.safe_load((dest / "provider.yaml").read_text(encoding="utf-8"))
    data["variants"][0]["checks"][0]["http"]["nope"] = True
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    assert any("Additional properties" in message for message in exc.value.messages)


def test_nine_variants_rejected_by_schema(tmp_path: Path) -> None:
    dest, data = _copy_echo(tmp_path)
    seed = data["variants"][0]
    data["variants"] = [dict(seed, name=f"v{index}") for index in range(9)]
    _dump(dest, data)
    with pytest.raises(ProviderError) as exc:
        load_provider(dest)
    blob = "\n".join(exc.value.messages)
    assert "variants" in blob
    assert "same fields" not in blob


def _copy_echo(tmp_path: Path, name: str = "echo") -> tuple[Path, dict]:
    dest = tmp_path / name
    shutil.copytree(ECHO_DIR, dest)
    data = yaml.safe_load((dest / "provider.yaml").read_text(encoding="utf-8"))
    return dest, data


def _dump(dest: Path, data: dict) -> None:
    (dest / "provider.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
