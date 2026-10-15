"""Notion provider package checks (shared catalog tests live in test_packages.py from S09)."""

import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from stdio_box.catalog import build_catalog
from stdio_box.cli import main
from stdio_box.creds_tools import TOOLS
from stdio_box.provider import load_provider

ROOT = Path(__file__).resolve().parents[2]
NOTION = ROOT / "packages" / "notion"
KEYSTONectl = Path("/Users/skynet/keystone/keystonectl")
_PUBLISHED = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CREDS = [tool.name for tool in TOOLS]


def test_validate_provider(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["validate-provider", str(NOTION)])
    assert exc.value.code == 0
    assert capsys.readouterr().out == ""


def test_manifest_matches_snapshot_and_provider() -> None:
    provider = load_provider(NOTION)
    snapshot = json.loads((NOTION / "tools.snapshot.json").read_text(encoding="utf-8"))
    catalog = build_catalog(snapshot, provider)
    published = [tool.name for tool in catalog.published_tools()]
    for name in published:
        assert _PUBLISHED.fullmatch(name)
        assert len(f"notion_{name}") <= 64

    manifest = yaml.safe_load((NOTION / "keystone-service.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == "notion"
    assert manifest["mcp"]["namespace"] == "notion"
    mcp_tools = manifest["mcp"]["tools"]
    assert mcp_tools == published + _CREDS

    env = {item["name"]: item.get("default") for item in manifest["containers"]["mcp"]["env"]}
    assert env["BOX_PROVIDER"] == "notion"
    assert env["BOX_COMMAND"] == provider.server.command

    assert snapshot["server"]["command"] == provider.server.command


def test_lockfile_pins_server_version() -> None:
    lock = json.loads((NOTION / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] == 3
    pkg = lock["packages"]["node_modules/@notionhq/notion-mcp-server"]
    assert pkg["version"] == "2.5.2"


def test_keystonectl_lint_manifest() -> None:
    if not (KEYSTONectl / "pyproject.toml").is_file():
        return
    result = subprocess.run(
        ["uv", "run", "keystonectl", "lint", str(NOTION / "keystone-service.yaml")],
        cwd=KEYSTONectl,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 errors" in result.stdout.lower() or result.returncode == 0
