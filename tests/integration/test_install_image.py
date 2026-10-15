"""The image installs mcp-atlassian from the atlassian test provider into /cache."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "stdio-box:s15-install"
VOLUME = "stdio-box-s15-cache"
COMMAND = "uvx mcp-atlassian==0.23.1"


@pytest.fixture(scope="module")
def image(tmp_path_factory: pytest.TempPathFactory) -> str:
    context = tmp_path_factory.mktemp("context")
    _stage_context(context)
    compiled = subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            "--universal",
            "--generate-hashes",
            "--python-version",
            "3.12",
            "requirements.in",
            "-o",
            "requirements.txt",
        ],
        cwd=context / "packages" / "atlassian",
        check=False,
        capture_output=True,
        text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    lock = (context / "packages" / "atlassian" / "requirements.txt").read_text(encoding="utf-8")
    assert "mcp-atlassian==0.23.1" in lock
    assert "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" not in lock
    build = subprocess.run(
        ["docker", "build", "-t", IMAGE, str(context)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    return IMAGE


def test_wrong_version_exits_3(image: str) -> None:
    result = _run(
        image,
        ["-e", "BOX_PROVIDER=atlassian", "-e", "BOX_COMMAND=uvx mcp-atlassian==0.23.0"],
    )
    assert result.returncode == 3
    assert "box_command_not_locked" in result.stderr


def test_install_is_owned_by_the_installer_and_reused_offline(image: str) -> None:
    subprocess.run(["docker", "volume", "rm", "-f", VOLUME], check=False, capture_output=True)
    subprocess.run(["docker", "volume", "create", VOLUME], check=True, capture_output=True)
    try:
        first = _run(image, ["-v", f"{VOLUME}:/cache", *_provider_env()])
        assert first.returncode == 0, first.stderr
        payload = _payload(first.stdout)
        assert payload["from_cache"] is False
        key = payload["key"]
        binary = f"/cache/atlassian/{key}/venv/bin/mcp-atlassian"
        owner = _run(
            image,
            ["-v", f"{VOLUME}:/cache", "--entrypoint", "stat"],
            ["-c", "%u", binary],
        )
        assert owner.stdout.strip() == "10002"

        denied = _run(
            image,
            [
                "-v",
                f"{VOLUME}:/cache",
                "--user",
                "20000",
                "--network",
                "none",
                "--entrypoint",
                "touch",
            ],
            ["/cache/denied-by-child"],
        )
        assert denied.returncode != 0
        assert "Permission denied" in denied.stderr

        started = _run(
            image,
            [
                "-v",
                f"{VOLUME}:/cache",
                "--user",
                "20000",
                "--network",
                "none",
                "-e",
                "HOME=/tmp",
                "-e",
                "PYTHONDONTWRITEBYTECODE=1",
                "--entrypoint",
                binary,
            ],
            ["--help"],
        )
        assert "Permission denied" not in started.stderr

        second = _run(image, ["--network", "none", "-v", f"{VOLUME}:/cache", *_provider_env()])
        assert second.returncode == 0, second.stderr
        assert _payload(second.stdout)["from_cache"] is True
    finally:
        subprocess.run(["docker", "volume", "rm", "-f", VOLUME], check=False, capture_output=True)


def _provider_env() -> list[str]:
    return ["-e", "BOX_PROVIDER=atlassian", "-e", f"BOX_COMMAND={COMMAND}"]


def _payload(stdout: str) -> dict[str, object]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def _run(
    image: str, options: list[str], command: list[str] | None = None
) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "run", "--rm", *options, image]
    if command:
        argv.extend(command)
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=300)


def _stage_context(context: Path) -> None:
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
    packages.mkdir()
    shutil.copytree(ROOT / "tests" / "data" / "packages" / "atlassian", packages / "atlassian")
    provider = packages / "atlassian"
    # The checked-in lock is a stub hash. uv keeps hashes already present in
    # the output file, so the stub must be removed before compile.
    (provider / "requirements.txt").unlink()
    (provider / "requirements.in").write_text("mcp-atlassian==0.23.1\n", encoding="utf-8")
