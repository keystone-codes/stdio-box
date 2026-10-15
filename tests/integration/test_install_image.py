"""The image installs mcp-atlassian from the atlassian test provider into /cache.

``launch`` installs, then splits into the spawner and the wrapper and stays up.
The install record is the ``install`` log line, not a process that prints JSON
and exits 0.
"""

import json
import shutil
import subprocess
import time
import uuid
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
    first_name = ""
    second_name = ""
    try:
        first_name = _start(image, ["-v", f"{VOLUME}:/cache", *_provider_env()])
        payload = _wait_install(first_name)
        assert payload["from_cache"] is False
        key = payload["key"]
        binary = f"/cache/atlassian/{key}/venv/bin/mcp-atlassian"
        owner = _exec(first_name, ["stat", "-c", "%u", binary])
        assert owner.returncode == 0, owner.stderr
        assert owner.stdout.strip() == "10002"

        denied = _exec(first_name, ["touch", "/cache/denied-by-child"], user="20000")
        assert denied.returncode != 0
        assert "Permission denied" in denied.stderr

        started = _exec(
            first_name,
            [binary, "--help"],
            user="20000",
            env={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert "Permission denied" not in started.stderr
        _remove(first_name)
        first_name = ""

        second_name = _start(
            image, ["--network", "none", "-v", f"{VOLUME}:/cache", *_provider_env()]
        )
        assert _wait_install(second_name, timeout=90)["from_cache"] is True
    finally:
        if first_name:
            _remove(first_name)
        if second_name:
            _remove(second_name)
        subprocess.run(["docker", "volume", "rm", "-f", VOLUME], check=False, capture_output=True)


def _provider_env() -> list[str]:
    return ["-e", "BOX_PROVIDER=atlassian", "-e", f"BOX_COMMAND={COMMAND}"]


def _payload(stdout: str) -> dict[str, object]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def _start(image: str, options: list[str]) -> str:
    name = f"stdio-box-s15-{uuid.uuid4().hex[:8]}"
    result = subprocess.run(
        ["docker", "run", "-d", "--name", name, *options, image],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return name


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)


def _exec(
    name: str,
    args: list[str],
    user: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "exec"]
    if user is not None:
        command.extend(["-u", user])
    for key, value in (env or {}).items():
        command.extend(["-e", f"{key}={value}"])
    command.append(name)
    command.extend(args)
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)


def _wait_install(name: str, timeout: int = 300) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        logs = subprocess.run(
            ["docker", "logs", name],
            check=False,
            capture_output=True,
            text=True,
        )
        last = logs.stdout + logs.stderr
        payload = _install_event(last)
        if payload is not None:
            return payload
        inspected = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}} {{.State.ExitCode}}", name],
            check=False,
            capture_output=True,
            text=True,
        )
        state = inspected.stdout.strip()
        if state.startswith("exited"):
            raise AssertionError(f"container exited before install: {state}\n{last}")
        time.sleep(0.5)
    raise AssertionError(f"install did not finish\n{last}")


def _install_event(text: str) -> dict[str, object] | None:
    found: dict[str, object] | None = None
    for line in text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            payload = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == "install":
            found = payload
    return found


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
