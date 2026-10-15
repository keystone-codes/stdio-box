"""Checks shared by every provider directory under packages/.

Later packages are picked up by the directory scan. Notion-specific cases
belong in this file beside this test, not in a second copy of the scan.
"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from stdio_box.catalog import _published_name, build_catalog
from stdio_box.creds_tools import TOOLS
from stdio_box.provider import Provider, load_provider

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ROOT / "packages"
CHANGING_CREDS = (
    "creds_profile_create",
    "creds_profile_rotate",
    "creds_profile_delete",
    "creds_bind",
    "creds_unbind",
)


def _package_dirs() -> list[Path]:
    return sorted(path for path in PACKAGES.iterdir() if path.is_dir())


@pytest.mark.parametrize("package", _package_dirs(), ids=lambda path: path.name)
def test_provider_package(package: Path) -> None:
    validated = subprocess.run(
        ["uv", "run", "stdio-box", "validate-provider", str(package)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert validated.returncode == 0, validated.stdout + validated.stderr

    provider = load_provider(package)
    assert provider.name == package.name
    manifest = yaml.safe_load((package / "keystone-service.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == package.name
    namespace = manifest["mcp"]["namespace"]
    assert namespace == package.name.replace("-", "_")
    defaults = {item["name"]: item.get("default") for item in manifest["containers"]["mcp"]["env"]}
    assert defaults["BOX_PROVIDER"] == package.name
    assert defaults["BOX_COMMAND"] == provider.server.command

    snapshot = json.loads((package / "tools.snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["server"]["command"] == provider.server.command
    published = [tool.name for tool in build_catalog(snapshot, provider).published_tools()]
    creds = [tool.name for tool in TOOLS]
    assert manifest["mcp"]["tools"] == published + creds
    assert manifest["mcp"]["critical"] == _critical(snapshot, provider) + list(CHANGING_CREDS)
    for name in manifest["mcp"]["tools"]:
        assert len(f"{namespace}_{name}") <= 64

    _assert_policy(package, namespace)
    _assert_lint(package)


def _critical(snapshot: dict, provider: Provider) -> list[str]:
    hinted: list[str] = []
    saw_hint = False
    for tool in snapshot["tools"]:
        child = tool["name"]
        published = _published_name(child, provider.tools)
        annotations = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        if "destructiveHint" in annotations:
            saw_hint = True
        if annotations.get("destructiveHint") is True and published is not None:
            hinted.append(published)
    if saw_hint:
        return hinted
    return [
        published
        for tool in snapshot["tools"]
        if "delete" in tool["name"]
        for published in [_published_name(tool["name"], provider.tools)]
        if published is not None
    ]


def _assert_policy(package: Path, namespace: str) -> None:
    rendered = _policy_example(package, namespace)
    assert (package / "policy.example.yaml").read_bytes() == rendered
    document = yaml.safe_load(rendered.decode("utf-8"))
    _assert_reader_writer(document, namespace)


def _assert_reader_writer(document: dict, namespace: str) -> None:
    roles = document["roles"]
    forbidden = {f"{namespace}_*", f"{namespace}_creds_*"}
    allowed_creds = {f"{namespace}_creds_whoami", f"{namespace}_creds_providers"}
    for role in (f"{namespace}_reader", f"{namespace}_writer"):
        allow = roles[role]["allow"]
        assert forbidden.isdisjoint(allow)
        for item in allow:
            if str(item).startswith(f"{namespace}_creds_"):
                assert item in allowed_creds


def _policy_example(package: Path, namespace: str) -> bytes:
    """Stdout of ``policy-example`` for this package, unchanged."""
    result = subprocess.run(
        [
            "uv",
            "run",
            "stdio-box",
            "policy-example",
            "--namespace",
            namespace,
            "--provider-dir",
            str(package),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _assert_lint(package: Path) -> None:
    result = subprocess.run(
        ["uvx", "keystonectl@0.1.0", "lint", str(package)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "0 errors, 0 warnings"
