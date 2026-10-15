"""snapshot-tools records tools/list and checks it without the timestamp."""

import json
import sys
from pathlib import Path

import pytest

from stdio_box.snapshot import snapshot_main

ECHO_DIR = Path(__file__).resolve().parent / "data" / "packages" / "echo"
ECHO_SERVER = Path(__file__).resolve().parent / "support" / "echo_server.py"


def test_echo_snapshot_and_check(tmp_path: Path) -> None:
    provider = tmp_path / "echo"
    provider.mkdir()
    for name in ("provider.yaml", "requirements.txt"):
        (provider / name).write_bytes((ECHO_DIR / name).read_bytes())
    override = f"{sys.executable} {ECHO_SERVER}"
    environ = {
        "BOX_DEBUG": "1",
        "BOX_CHILD_ARGV_OVERRIDE": override,
        "BOX_PROVIDER_DIR": str(provider),
    }
    out = tmp_path / "taken.json"
    assert snapshot_main(["--provider-dir", str(provider), "--out", str(out)], environ) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert document["server"]["command"] == "uvx echo-server==0.0.0"
    assert document["taken_at"]
    names = [tool["name"] for tool in document["tools"]]
    assert "who" in names
    assert "spam" in names

    (provider / "tools.snapshot.json").write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    assert snapshot_main(["--provider-dir", str(provider), "--check"], environ) == 0

    changed = json.loads(out.read_text(encoding="utf-8"))
    changed["tools"][0]["name"] = "renamed"
    (provider / "tools.snapshot.json").write_text(json.dumps(changed), encoding="utf-8")
    assert snapshot_main(["--provider-dir", str(provider), "--check"], environ) == 1


def test_check_prints_a_difference(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    provider = tmp_path / "echo"
    provider.mkdir()
    for name in ("provider.yaml", "requirements.txt"):
        (provider / name).write_bytes((ECHO_DIR / name).read_bytes())
    (provider / "tools.snapshot.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "server": {"command": "uvx echo-server==0.0.0"},
                "taken_at": "2000-01-01T00:00:00Z",
                "tools": [],
            }
        ),
        encoding="utf-8",
    )
    environ = {
        "BOX_DEBUG": "1",
        "BOX_CHILD_ARGV_OVERRIDE": f"{sys.executable} {ECHO_SERVER}",
    }
    code = snapshot_main(["--provider-dir", str(provider), "--check"], environ)
    assert code == 1
    assert capsys.readouterr().out.strip()
