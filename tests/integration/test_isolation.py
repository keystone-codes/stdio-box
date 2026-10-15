"""Linux and Docker: wrapper uid, per-slot children, reap, and the process limit."""

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
BASE_IMAGE = "stdio-box:s04"
ECHO_IMAGE = "stdio-box:s04-echo"


@pytest.fixture(scope="module")
def echo_image() -> str:
    base = subprocess.run(
        ["docker", "build", "-t", BASE_IMAGE, str(ROOT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert base.returncode == 0, base.stderr
    echo = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(ROOT / "tests/integration/Dockerfile.echo"),
            "-t",
            ECHO_IMAGE,
            str(ROOT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert echo.returncode == 0, echo.stderr
    return ECHO_IMAGE


def test_uids_environ_and_volumes(echo_image: str) -> None:
    container = _start(echo_image)
    try:
        _wait_ready(container)
        pid, uid = _wrapper(container)
        assert uid == "10001"
        denied = _python(container, f"open('/proc/{pid}/environ','rb').read()", user="10001:10001")
        assert denied.returncode != 0
        assert "Permission denied" in denied.stderr
        denied_other = _python(
            container, f"open('/proc/{pid}/environ','rb').read()", user="20000:20000"
        )
        assert denied_other.returncode != 0
        assert "Permission denied" in denied_other.stderr

        first = _debug(
            container,
            [
                "--slot",
                "0",
                "--env",
                "ECHO_TOKEN=slot-zero-token",
                "--env",
                "ECHO_URL=https://zero.invalid",
            ],
        )
        second = _debug(
            container,
            [
                "--slot",
                "1",
                "--env",
                "ECHO_TOKEN=slot-one-token",
                "--env",
                "ECHO_URL=https://one.invalid",
            ],
        )
        assert first["uid"] == 20000
        assert second["uid"] == 20001
        cross = _python(
            container,
            f"open('/proc/{second['pid']}/environ','rb').read()",
            user="20000:20000",
        )
        assert cross.returncode != 0
        assert "Permission denied" in cross.stderr
        data = _python(container, "import os; os.listdir('/data')", user="20000:20000")
        assert data.returncode != 0
        assert "Permission denied" in data.stderr
        for user in ("20000:20000", "10001:10001"):
            write = _python(container, "open('/cache/probe','w').write('x')", user=user)
            assert write.returncode != 0
            assert "Permission denied" in write.stderr
    finally:
        _remove(container)


def test_slot_reuse_kills_escaped_descendants(echo_image: str) -> None:
    container = _start(echo_image)
    try:
        _wait_ready(container)
        started = _debug(
            container,
            [
                "--slot",
                "0",
                "--env",
                "ECHO_TOKEN=escape-token",
                "--env",
                "ECHO_URL=https://escape.invalid",
            ],
        )
        assert started["uid"] == 20000
        escaped = _debug(container, ["--slot", "0", "--tool", "escape"])
        child = escaped["structured"]["child_pid"]
        assert child > 0
        _wait_for(container, f"test -e /tmp/escape-{child} && test -e /dev/shm/escape-{child}")
        stopped = _debug(container, ["--slot", "0", "--stop"])
        assert stopped["ok"] is True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not _pids_with_uid(container, 20000) and not _escape_files(container):
                break
            time.sleep(0.2)
        else:
            raise AssertionError(
                f"slot uid still present: {_pids_with_uid(container, 20000)} {_escape_files(container)}"
            )
        again = _debug(
            container,
            [
                "--slot",
                "0",
                "--env",
                "ECHO_TOKEN=next-token",
                "--env",
                "ECHO_URL=https://next.invalid",
            ],
        )
        assert again["uid"] == 20000
        assert again["pid"] != started["pid"]
    finally:
        _remove(container)


def test_nproc_limit(echo_image: str) -> None:
    container = _start(echo_image)
    try:
        _wait_ready(container)
        bombed = _debug(
            container,
            ["--slot", "0", "--tool", "forkbomb", "--arguments", json.dumps({"n": 600})],
            timeout=120,
        )
        structured = bombed["structured"]
        assert structured["forked"] < 600
        assert "errno" in structured
        other = _debug(container, ["--slot", "1", "--tool", "who"])
        assert other["ok"] is True
        assert other["structured"]["uid"] == 20001
        status = _exec(container, ["credsctl", "status"])
        assert status.returncode == 0, status.stderr
        payload = json.loads(status.stdout)
        assert payload["status"] == "ok"
        _pid, uid = _wrapper(container)
        assert uid == "10001"
    finally:
        _remove(container)


def test_cap_drop_starts_children_and_warns(echo_image: str) -> None:
    container = _start(echo_image, "--cap-drop", "SETUID", "--cap-drop", "SETGID")
    try:
        _wait_ready(container)
        started = _debug(container, ["--slot", "0", "--tool", "who"])
        assert started["ok"] is True
        logs = subprocess.run(
            ["docker", "logs", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "child_isolation_unavailable" in logs.stderr + logs.stdout
        status = json.loads(_exec(container, ["credsctl", "status"]).stdout)
        assert status["isolation"] == "shared_uid"
    finally:
        _remove(container)


def _start(image: str, *extra: str) -> str:
    name = f"stdio-box-s04-{uuid.uuid4().hex[:8]}"
    result = subprocess.run(
        ["docker", "run", "-d", "--name", name, *extra, image],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return name


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, text=True)


def _exec(
    name: str, args: list[str], user: str | None = None, timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    command = ["docker", "exec"]
    if user is not None:
        command.extend(["-u", user])
    command.append(name)
    command.extend(args)
    return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)


def _python(name: str, code: str, user: str | None = None) -> subprocess.CompletedProcess[str]:
    return _exec(name, ["python3", "-c", code], user=user)


def _wait_ready(name: str) -> None:
    deadline = time.monotonic() + 30
    last = ""
    while time.monotonic() < deadline:
        result = _exec(name, ["stdio-box", "debug-spawn", "--status"])
        if result.returncode == 0:
            return
        last = result.stderr
        time.sleep(0.3)
    logs = subprocess.run(["docker", "logs", name], check=False, capture_output=True, text=True)
    raise AssertionError(f"wrapper did not become ready: {last}\n{logs.stderr}")


def _debug(name: str, args: list[str], timeout: int = 60) -> dict:
    result = _exec(name, ["stdio-box", "debug-spawn", *args], timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _wrapper(name: str) -> tuple[str, str]:
    result = _python(
        name,
        "import os\n"
        "for name in os.listdir('/proc'):\n"
        "    if not name.isdigit():\n"
        "        continue\n"
        "    try:\n"
        "        raw = open(f'/proc/{name}/cmdline','rb').read()\n"
        "        status = open(f'/proc/{name}/status',encoding='utf-8').read()\n"
        "    except OSError:\n"
        "        continue\n"
        "    cmd = raw.replace(b'\\0', b' ').decode()\n"
        "    if 'stdio-box' in cmd and ' serve ' in f' {cmd} ':\n"
        "        uid = status.split('Uid:')[1].split()[0]\n"
        "        print(name, uid)\n",
    )
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip().splitlines()[0]
    pid, uid = line.split()
    return pid, uid


def _pids_with_uid(name: str, uid: int) -> list[str]:
    result = _python(
        name,
        "import os\n"
        f"uid = '{uid}'\n"
        "found = []\n"
        "for name in os.listdir('/proc'):\n"
        "    if not name.isdigit():\n"
        "        continue\n"
        "    try:\n"
        "        text = open(f'/proc/{name}/status',encoding='utf-8').read()\n"
        "    except OSError:\n"
        "        continue\n"
        "    for line in text.splitlines():\n"
        "        if line.startswith('Uid:') and line.split()[1] == uid:\n"
        "            found.append(name)\n"
        "print(' '.join(found))\n",
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout.strip()
    return text.split() if text else []


def _escape_files(name: str) -> list[str]:
    result = _python(
        name,
        "import os\n"
        "names = []\n"
        "for root in ('/tmp', '/dev/shm'):\n"
        "    for name in os.listdir(root):\n"
        "        if name.startswith('escape-'):\n"
        "            names.append(root + '/' + name)\n"
        "print('\\n'.join(names))\n",
    )
    assert result.returncode == 0, result.stderr
    return [line for line in result.stdout.splitlines() if line]


def _wait_for(name: str, shell: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = _exec(name, ["sh", "-c", shell])
        if result.returncode == 0:
            return
        time.sleep(0.2)
    raise AssertionError(f"condition not met: {shell}")
