"""The command must match the provider lock, including hashes."""

import shutil
from pathlib import Path

import pytest
import yaml

from stdio_box.boxcommand import BoxCommandError, parse_box_command
from stdio_box.install import verify_against_lock
from stdio_box.provider import load_provider

ECHO_DIR = Path(__file__).resolve().parent / "data" / "packages" / "echo"
LOCKS = Path(__file__).resolve().parent / "data" / "locks"


def test_matching_python_and_node_locks(tmp_path: Path) -> None:
    python_dir = _provider(tmp_path, "python-ok", "uvx Demo_Server==0.1.0", "demo-server")
    info = verify_against_lock(
        parse_box_command("uvx Demo_Server==0.1.0"), python_dir, load_provider(python_dir)
    )
    assert info.runner == "uvx"
    assert info.version == "0.1.0"
    assert info.package_json_bytes == b""

    node_dir = _provider(tmp_path, "node-ok", "npx -y tiny-bin@1.0.0", "tiny-bin")
    node = verify_against_lock(
        parse_box_command("npx -y tiny-bin@1.0.0"), node_dir, load_provider(node_dir)
    )
    assert node.runner == "npx"
    assert node.package == "tiny-bin"
    assert node.package_json_bytes


@pytest.mark.parametrize(
    ("fixture", "command", "fragment"),
    [
        ("python-other-version", "uvx demo-server==0.1.0", "0.9.0"),
        ("python-missing-hash", "uvx demo-server==0.1.0", "has no sha256 hash"),
        ("node-range", "npx -y tiny-bin@1.0.0", "^1.0.0"),
        ("node-no-integrity", "npx -y tiny-bin@1.0.0", "node_modules/left-pad"),
    ],
)
def test_lock_mismatches(tmp_path: Path, fixture: str, command: str, fragment: str) -> None:
    directory = _provider(tmp_path, fixture, command, "demo-server")
    with pytest.raises(BoxCommandError) as exc:
        verify_against_lock(parse_box_command(command), directory, load_provider(directory))
    assert exc.value.code == "box_command_not_locked"
    assert fragment in exc.value.message
    assert (
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in exc.value.message
    )
    assert "KEEPTHIS" not in exc.value.message


def _provider(tmp_path: Path, fixture: str, command: str, binary: str) -> Path:
    dest = tmp_path / fixture / "echo"
    shutil.copytree(ECHO_DIR, dest)
    data = yaml.safe_load((dest / "provider.yaml").read_text(encoding="utf-8"))
    data["server"]["command"] = command
    data["server"]["bin"] = binary
    source = LOCKS / fixture
    if (source / "requirements.txt").is_file():
        data["server"]["lock"] = "requirements.txt"
        shutil.copyfile(source / "requirements.txt", dest / "requirements.txt")
    else:
        data["server"]["lock"] = "package-lock.json"
        (dest / "requirements.txt").unlink()
        shutil.copyfile(source / "package.json", dest / "package.json")
        shutil.copyfile(source / "package-lock.json", dest / "package-lock.json")
    (dest / "provider.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return dest
