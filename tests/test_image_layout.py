"""The image carries provider data, the two service users, uv, and npm."""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "stdio-box:s01-layout"


def test_image_layout(tmp_path: Path) -> None:
    context = tmp_path / "context"
    _stage_context(context)
    build = subprocess.run(
        ["docker", "build", "-t", IMAGE, str(context)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr

    assert _run(["test", "-f", "/opt/box/packages/echo/provider.yaml"]).returncode == 0
    listed = _run(
        [
            "python",
            "-c",
            "import os,sys\n"
            "root='/opt/box/packages'\n"
            "hits=[]\n"
            "for dirpath, _dirs, files in os.walk(root):\n"
            "    for name in files:\n"
            "        if name in {'keystone-service.yaml','policy.example.yaml','README.md'}:\n"
            "            hits.append(os.path.join(dirpath, name))\n"
            "print('\\n'.join(hits))\n"
            "sys.exit(1 if hits else 0)\n",
        ]
    )
    assert listed.returncode == 0, listed.stdout

    box = _run_text(["id", "box"])
    assert box.startswith("uid=10001(box)")
    installer = _run_text(["id", "box-install"])
    assert installer.startswith("uid=10002(box-install)")
    assert _run_text(["uv", "--version"]).startswith("uv ")
    assert _run_text(["npm", "--version"]).strip()[:1].isdigit()

    launch = subprocess.run(
        ["docker", "run", "--rm", IMAGE], check=False, capture_output=True, text=True
    )
    assert launch.returncode == 2
    assert "provider install is not implemented yet" in launch.stderr
    checked = _run(["stdio-box", "validate-provider", "/opt/box/packages/echo"])
    assert checked.returncode == 0, checked.stdout + checked.stderr


def _stage_context(context: Path) -> None:
    context.mkdir()
    for name in (
        "Dockerfile",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        ".dockerignore",
    ):
        shutil.copy2(ROOT / name, context / name)
    shutil.copytree(
        ROOT / "src", context / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    shutil.copytree(ROOT / "schemas", context / "schemas")
    packages = context / "packages"
    shutil.copytree(ROOT / "tests" / "data" / "packages", packages)
    echo = packages / "echo"
    (echo / "keystone-service.yaml").write_text("name: echo\n", encoding="utf-8")
    (echo / "policy.example.yaml").write_text("version: '2.0.0'\n", encoding="utf-8")
    (echo / "README.md").write_text("not in the image\n", encoding="utf-8")


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", args[0], IMAGE, *args[1:]],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_text(args: list[str]) -> str:
    result = _run(args)
    assert result.returncode == 0, result.stderr
    return result.stdout
