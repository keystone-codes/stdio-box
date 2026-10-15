"""Install from a lock file into a cache directory, without a network."""

import base64
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest
import yaml

from stdio_box.boxcommand import parse_box_command
from stdio_box.config import ConfigError, InstallSettings, load_settings
from stdio_box.install import (
    INSTALLER_VERSION,
    InstalledServer,
    InstallFailed,
    cache_key,
    ensure_installed,
    install_status,
    installed_payload,
    installer_environment,
    mask_install_text,
)
from stdio_box.launch import install_step, run_launch
from stdio_box.provider import load_provider

ECHO_DIR = Path(__file__).resolve().parent / "data" / "packages" / "echo"
SECRET_URL = "https://example.invalid/supersecretvalue-index"
TOKEN = "ATATTaaaaaaaaaaaaaaaaaaaa"


def test_python_install_reuses_one_previous_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    wheel = build_wheel(wheel_dir)
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    provider_dir = _python_provider(tmp_path / "provider", digest, comment="")
    cache = tmp_path / "cache"
    calls = _count_popen(monkeypatch)
    python = Path(sys.executable).resolve()
    mode_before = python.stat().st_mode

    first = _install(provider_dir, cache, find_links=wheel_dir)
    assert first.from_cache is False
    assert ".tmp-" not in first.argv0
    assert first.key in first.argv0
    assert calls["n"] > 0
    ran = subprocess.run([first.argv0], check=False, capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == "ok\n"
    _assert_not_writable(cache / "echo" / first.key)
    assert python.stat().st_mode == mode_before

    after_first = calls["n"]
    second = _install(provider_dir, cache, find_links=wheel_dir)
    assert second.from_cache is True
    assert second.argv0 == first.argv0
    assert calls["n"] == after_first

    time.sleep(0.05)
    _rewrite_comment(provider_dir, "second")
    third = _install(provider_dir, cache, find_links=wheel_dir)
    assert third.key != first.key
    assert (cache / "echo" / first.key).is_dir()
    assert (cache / "echo" / third.key).is_dir()

    time.sleep(0.05)
    _rewrite_comment(provider_dir, "third")
    fourth = _install(provider_dir, cache, find_links=wheel_dir)
    assert not (cache / "echo" / first.key).exists()
    assert (cache / "echo" / third.key).is_dir()
    assert (cache / "echo" / fourth.key).is_dir()
    assert python.stat().st_mode == mode_before


def test_node_install_uses_the_lock_and_drops_the_npm_cache(tmp_path: Path) -> None:
    fixture = _node_fixture(tmp_path)
    provider_dir = _node_provider(tmp_path / "prov", fixture)
    cache = tmp_path / "cache"
    installed = _install(provider_dir, cache, command="npx -y tiny-bin@1.0.0")
    script = Path(installed.args[0])
    assert script.is_file()
    assert not (cache / "echo" / installed.key / ".npm").exists()
    ran = subprocess.run(
        [installed.argv0, *installed.args], check=False, capture_output=True, text=True
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == "ok\n"
    _assert_not_writable(cache / "echo" / installed.key)
    again = _install(provider_dir, cache, command="npx -y tiny-bin@1.0.0")
    assert again.from_cache is True
    assert again.key == installed.key


def test_killed_install_leaves_no_complete_marker(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sleeper = bin_dir / "uv"
    sleeper.write_text("#!/bin/sh\nexec /bin/sleep 60\n", encoding="utf-8")
    sleeper.chmod(0o755)
    cache = tmp_path / "cache"
    script = tmp_path / "hang_install.py"
    script.write_text(
        textwrap.dedent(
            """\
            import os
            import sys
            from pathlib import Path

            from stdio_box.boxcommand import parse_box_command
            from stdio_box.install import ensure_installed, installer_environment
            from stdio_box.provider import load_provider

            provider = Path(sys.argv[1])
            cache = Path(sys.argv[2])
            env = installer_environment(cache)
            env["PATH"] = sys.argv[3]
            ensure_installed(
                parse_box_command("uvx echo-server==0.0.0"),
                load_provider(provider),
                provider,
                cache,
                run_as=None,
                timeout=30,
                env=env,
            )
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script), str(ECHO_DIR), str(cache), str(bin_dir)],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and proc.poll() is None:
        if any(path.name.startswith(".tmp-") for path in cache.rglob("*")):
            break
        time.sleep(0.05)
    assert proc.poll() is None
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)
    assert list(cache.rglob(".complete")) == []


def test_installer_failure_masks_secrets(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        f'echo "index=$UV_INDEX_URL token={TOKEN} ordinary sk-learn secret_santa" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    uv.chmod(0o755)
    cache = tmp_path / "cache"
    env = installer_environment(cache, pip_index_url=SECRET_URL)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    with pytest.raises(InstallFailed) as exc:
        ensure_installed(
            parse_box_command("uvx echo-server==0.0.0"),
            load_provider(ECHO_DIR),
            ECHO_DIR,
            cache,
            run_as=None,
            timeout=30,
            env=env,
        )
    assert "supersecretvalue" not in exc.value.reason
    assert TOKEN not in exc.value.reason
    assert "sk-learn" in caplog.text
    assert "secret_santa" in caplog.text
    assert TOKEN not in caplog.text
    assert list(cache.rglob(".complete")) == []


def test_mask_keeps_ordinary_words() -> None:
    masked = mask_install_text(f"sk-learn secret_santa {TOKEN}", (SECRET_URL,))
    assert "sk-learn" in masked
    assert "secret_santa" in masked
    assert TOKEN not in masked
    assert SECRET_URL not in masked


def test_installer_environment_is_closed() -> None:
    env = installer_environment(
        Path("/cache"),
        pip_index_url="https://pypi.example/simple",
        npm_registry="https://npm.example/registry",
    )
    assert set(env) == {"PATH", "HOME", "LANG", "UV_INDEX_URL", "npm_config_registry"}
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert env["HOME"] == "/cache/.home"
    assert env["UV_INDEX_URL"] == "https://pypi.example/simple"


def test_install_settings_reject_bad_timeout_and_http_mirror() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(
            {"BOX_INSTALL_TIMEOUT_SECONDS": "10", "BOX_PIP_INDEX_URL": "http://example.invalid"}
        )
    text = "\n".join(exc.value.messages)
    assert "30" in text
    assert "https://" in text


def test_launch_requires_a_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_launch({}) == 2
    assert "config error: BOX_COMMAND is required" in capsys.readouterr().err


def test_launch_exits_3_when_the_command_is_not_locked(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    code = run_launch(
        {
            "BOX_COMMAND": "uvx echo-server==9.9.9",
            "BOX_PROVIDER": "echo",
            "BOX_PROVIDER_DIR": str(ECHO_DIR),
        }
    )
    assert code == 3
    assert "box_command_not_locked" in caplog.text
    assert "provider_install_failed" in caplog.text


def test_install_step_drops_uid_only_when_setuid_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    monkeypatch.setenv("BOX_STORE_KEY", "k" * 40)
    seen: dict[str, object] = {}

    def fake_ensure(*args: object, **kwargs: object) -> InstalledServer:
        del args
        seen["run_as"] = kwargs["run_as"]
        seen["env"] = kwargs["env"]
        return InstalledServer(
            argv0="/cache/echo/abcd/venv/bin/echo-server",
            key="abcd",
            from_cache=False,
            duration_ms=5,
            at="2026-01-01T00:00:00Z",
            args=(),
        )

    monkeypatch.setattr("stdio_box.launch.ensure_installed", fake_ensure)
    monkeypatch.setattr("stdio_box.launch.prepare_cache_root", lambda *_a, **_k: None)
    monkeypatch.setattr("stdio_box.launch.setuid_available", lambda: False)
    settings = _settings(tmp_path)
    install_step(settings)
    assert seen["run_as"] is None
    assert "install_isolation_unavailable" in caplog.text
    env = seen["env"]
    assert isinstance(env, dict)
    assert "BOX_STORE_KEY" not in env
    assert "k" * 40 not in "".join(env.values())

    monkeypatch.setattr("stdio_box.launch.setuid_available", lambda: True)
    install_step(settings)
    assert seen["run_as"] == 10002


def test_status_payload_shape() -> None:
    server = InstalledServer(
        argv0="/cache/echo/abcd/venv/bin/echo-server",
        key="abcd",
        from_cache=True,
        duration_ms=12,
        at="2026-01-01T00:00:00Z",
        args=("--flag",),
    )
    assert install_status(server)["key"] == "abcd"
    assert install_status(server)["duration_ms"] == 12
    assert install_status(server)["from_cache"] is True
    payload = installed_payload(server)
    assert '"path": "/cache/echo/abcd/venv/bin/echo-server"' in payload
    assert '"from_cache": true' in payload
    key = cache_key(b"lock", b"", "uvx", INSTALLER_VERSION)
    assert len(key) == 16
    assert key != cache_key(b"lock-2", b"", "uvx", INSTALLER_VERSION)


def _install(
    provider_dir: Path,
    cache: Path,
    *,
    find_links: Path | None = None,
    command: str | None = None,
) -> InstalledServer:
    provider = load_provider(provider_dir)
    text = command or provider.server.command
    env = installer_environment(cache)
    bins: list[str] = []
    for tool in ("uv", "npm"):
        found = shutil.which(tool)
        if found:
            directory = str(Path(found).parent)
            if directory not in bins:
                bins.append(directory)
    env["PATH"] = ":".join([*bins, "/usr/bin", "/bin"])
    return ensure_installed(
        parse_box_command(text),
        provider,
        provider_dir,
        cache,
        run_as=None,
        timeout=60,
        env=env,
        find_links=find_links,
        python_executable=sys.executable,
    )


def _count_popen(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"n": 0}
    real = subprocess.Popen

    def spy(*args: object, **kwargs: object) -> subprocess.Popen[str]:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return calls


def _python_provider(root: Path, digest: str, *, comment: str) -> Path:
    dest = root / "echo"
    shutil.copytree(ECHO_DIR, dest)
    data = yaml.safe_load((dest / "provider.yaml").read_text(encoding="utf-8"))
    data["server"]["command"] = "uvx demo-server==0.1.0"
    data["server"]["bin"] = "demo-server"
    data["server"]["lock"] = "requirements.txt"
    (dest / "provider.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    prefix = f"# {comment}\n" if comment else ""
    (dest / "requirements.txt").write_text(
        f"{prefix}demo-server==0.1.0 --hash=sha256:{digest}\n",
        encoding="utf-8",
    )
    return dest


def _rewrite_comment(provider_dir: Path, comment: str) -> None:
    path = provider_dir / "requirements.txt"
    body = path.read_text(encoding="utf-8")
    lines = [line for line in body.splitlines() if not line.startswith("#")]
    path.write_text(f"# {comment}\n" + "\n".join(lines) + "\n", encoding="utf-8")


def _node_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "package.json").write_text(
        '{"name":"tiny-bin","version":"1.0.0","bin":{"tiny-bin":"index.js"}}\n',
        encoding="utf-8",
    )
    script = fixture / "index.js"
    script.write_text("#!/usr/bin/env node\nprocess.stdout.write('ok\\n')\n", encoding="utf-8")
    script.chmod(0o755)
    packed = subprocess.run(
        ["npm", "pack", "--silent"],
        cwd=fixture,
        check=False,
        capture_output=True,
        text=True,
    )
    assert packed.returncode == 0, packed.stderr
    tarball = fixture / "tiny-bin-1.0.0.tgz"
    staged = tmp_path / "staged"
    staged.mkdir()
    subprocess.run(
        [
            "npm",
            "install",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--package-lock-only",
            f"tiny-bin@file:{tarball}",
        ],
        cwd=staged,
        check=True,
        capture_output=True,
        text=True,
    )
    _pin_file_dependency(staged, tarball)
    return staged


def _pin_file_dependency(staged: Path, tarball: Path) -> None:
    import json

    package = json.loads((staged / "package.json").read_text(encoding="utf-8"))
    package["dependencies"] = {"tiny-bin": "1.0.0"}
    (staged / "package.json").write_text(json.dumps(package), encoding="utf-8")
    lock = json.loads((staged / "package-lock.json").read_text(encoding="utf-8"))
    entry = lock["packages"]["node_modules/tiny-bin"]
    entry["resolved"] = f"file:{tarball}"
    root = lock["packages"].get("")
    if isinstance(root, dict) and "dependencies" in root:
        root["dependencies"] = {"tiny-bin": "1.0.0"}
    (staged / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")


def _node_provider(root: Path, staged: Path) -> Path:
    dest = root / "echo"
    shutil.copytree(ECHO_DIR, dest)
    data = yaml.safe_load((dest / "provider.yaml").read_text(encoding="utf-8"))
    data["server"]["command"] = "npx -y tiny-bin@1.0.0"
    data["server"]["bin"] = "tiny-bin"
    data["server"]["lock"] = "package-lock.json"
    (dest / "requirements.txt").unlink()
    shutil.copyfile(staged / "package.json", dest / "package.json")
    shutil.copyfile(staged / "package-lock.json", dest / "package-lock.json")
    (dest / "provider.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return dest


def _settings(tmp_path: Path) -> InstallSettings:
    return InstallSettings(
        command="uvx echo-server==0.0.0",
        provider="echo",
        provider_dir=ECHO_DIR,
        cache_dir=tmp_path / "cache",
        install_timeout_seconds=30,
        pip_index_url="",
        npm_registry="",
        debug=False,
        child_argv_override="",
    )


def _assert_not_writable(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        assert current.stat().st_mode & 0o777 == 0o755
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            path = current / name
            if path.is_symlink():
                continue
            assert path.stat().st_mode & 0o022 == 0


def build_wheel(dest: Path, *, name: str = "demo-server", version: str = "0.1.0") -> Path:
    """Write a pure-Python wheel with one console script and return its path."""
    distribution = name.replace("-", "_")
    info = f"{distribution}-{version}.dist-info"
    module = f"{distribution}/__init__.py"
    files = {
        module: "def main() -> None:\n    print('ok')\n",
        f"{info}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n"),
        f"{info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: stdio-box-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{info}/entry_points.txt": f"[console_scripts]\n{name} = {distribution}:main\n",
    }
    record_lines: list[str] = []
    for path, text in files.items():
        data = text.encode("utf-8")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")
        record_lines.append(f"{path},sha256={digest},{len(data)}")
    record_lines.append(f"{info}/RECORD,,")
    files[f"{info}/RECORD"] = "\n".join(record_lines) + "\n"
    wheel = dest / f"{distribution}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return wheel
