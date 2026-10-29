"""pin_digest.py writes IMAGE and one image line per selected manifest."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ROOT / "scripts" / "pin_digest.py"
_ZERO = "0" * 64
_OLD = "a" * 64
_NEW = "b" * 64
_FOREIGN = "c" * 64
_REF = f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_NEW}"
_RELEASE_PACKAGES = (
    "atlassian",
    "notion",
    "tavily",
    "yandex-tracker",
    "yandex-wiki",
)


def _run(tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        cwd=tmp,
        capture_output=True,
        text=True,
        check=False,
    )


def _pyproject(tmp: Path, version: str = "0.1.0") -> None:
    (tmp / "pyproject.toml").write_text(
        f'[project]\nversion = "{version}"\n',
        encoding="utf-8",
    )


def _manifest(name: str, image: str, *, version: str = "1.0.0") -> str:
    return (
        f"# keep {name}\n"
        "schema_version: 1\n"
        f"name: {name}\n"
        f"version: {version}\n"
        "containers:\n"
        "  mcp:\n"
        f'    image: "{image}" # stay\n'
        "needs: []\n"
    )


def _write_package(tmp: Path, name: str, image: str, *, version: str = "1.0.0") -> Path:
    package = tmp / "packages" / name
    package.mkdir(parents=True)
    manifest = package / "keystone-service.yaml"
    manifest.write_text(_manifest(name, image, version=version), encoding="utf-8")
    (package / "README.md").write_text("leave me\n", encoding="utf-8")
    return manifest


def _changed_lines(before: bytes, after: bytes) -> list[int]:
    old = before.splitlines(keepends=True)
    new = after.splitlines(keepends=True)
    assert old != new
    assert len(old) == len(new)
    return [
        index for index, (left, right) in enumerate(zip(old, new, strict=True)) if left != right
    ]


def test_pin_real_manifests_change_only_the_image_line(tmp_path: Path) -> None:
    packages = ROOT / "packages"
    _pyproject(tmp_path)
    (tmp_path / "IMAGE").write_bytes((ROOT / "IMAGE").read_bytes())
    before: dict[Path, bytes] = {}
    expected: list[str] = []
    for name in _RELEASE_PACKAGES:
        source = packages / name / "keystone-service.yaml"
        version = re.search(r"^version: (\S+)$", source.read_text(encoding="utf-8"), re.M)
        assert version is not None
        expected.append(f"{name} {version.group(1)}")
        dest_dir = tmp_path / "packages" / name
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "keystone-service.yaml"
        dest.write_bytes(source.read_bytes())
        (dest_dir / "README.md").write_bytes(b"leave me\n")
        before[dest] = dest.read_bytes()

    completed = _run(tmp_path, _REF)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == expected
    assert (tmp_path / "IMAGE").read_bytes() == f"{_REF}\n".encode()
    placeholder_text = b"# placeholder until the stdio-box image release records the digest"
    for dest, old in before.items():
        new = dest.read_bytes()
        assert placeholder_text not in old, (
            "fixture carries the placeholder comment again; "
            "see test_pin_drops_the_placeholder_comment_above_the_image_line instead"
        )
        assert _changed_lines(old, new) == [
            index
            for index, line in enumerate(old.splitlines(keepends=True))
            if re.match(rb"^[ \t]*image:[ \t]", line)
        ]
        assert _REF.encode() in new
        assert (b'"' + _REF.encode() + b'"') in new
        assert placeholder_text not in new
        assert (dest.parent / "README.md").read_bytes() == b"leave me\n"


def test_default_selects_zero_placeholder_and_previous_stdio_box(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    (tmp_path / "IMAGE").write_text("keep\n", encoding="utf-8")
    zero = f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}"
    previous = f"ghcr.io/keystone-codes/stdio-box:0.0.9@sha256:{_OLD}"
    foreign = f"ghcr.io/example/other:1.2.3@sha256:{_FOREIGN}"
    atlassian = _write_package(tmp_path, "atlassian", zero)
    notion = _write_package(tmp_path, "notion", previous)
    other = _write_package(tmp_path, "otherbox", foreign, version="9.9.9")
    other_before = other.read_bytes()
    readme_before = (tmp_path / "packages" / "otherbox" / "README.md").read_bytes()

    completed = _run(tmp_path, _REF)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["atlassian 1.0.0", "notion 1.0.0"]
    for manifest in (atlassian, notion):
        text = manifest.read_text(encoding="utf-8")
        assert f'image: "{_REF}" # stay' in text
        assert text.startswith("# keep ")
        assert "needs: []\n" in text
    assert other.read_bytes() == other_before
    assert (tmp_path / "packages" / "otherbox" / "README.md").read_bytes() == readme_before


def test_packages_flag_leaves_everyone_else(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    zero = f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}"
    atlassian = _write_package(tmp_path, "atlassian", zero)
    notion = _write_package(tmp_path, "notion", zero)
    before = atlassian.read_bytes()

    completed = _run(tmp_path, _REF, "--packages", "notion")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["notion 1.0.0"]
    assert atlassian.read_bytes() == before
    assert _REF in notion.read_text(encoding="utf-8")


def test_explicit_package_on_another_image_changes_only_that_line(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    foreign = f"ghcr.io/example/other:1.2.3@sha256:{_FOREIGN}"
    manifest = _write_package(tmp_path, "otherbox", foreign, version="9.9.9")
    before = manifest.read_bytes()

    completed = _run(tmp_path, _REF, "--packages", "otherbox")
    assert completed.returncode == 0, completed.stderr
    assert _changed_lines(before, manifest.read_bytes()) == [
        index
        for index, line in enumerate(before.splitlines(keepends=True))
        if re.match(rb"^[ \t]*image:[ \t]", line)
    ]
    assert manifest.read_bytes().splitlines()[0] == b"# keep otherbox"


def test_pin_drops_the_placeholder_comment_above_the_image_line(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    (tmp_path / "IMAGE").write_text("keep\n", encoding="utf-8")
    package = tmp_path / "packages" / "atlassian"
    package.mkdir(parents=True)
    manifest = package / "keystone-service.yaml"
    old = f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}"
    manifest.write_text(
        "schema_version: 1\n"
        "name: atlassian\n"
        "version: 1.0.0\n"
        "containers:\n"
        "  mcp:\n"
        "    # placeholder until the stdio-box image release records the digest\n"
        f'    image: "{old}"\n'
        "needs: []\n",
        encoding="utf-8",
    )
    completed = _run(tmp_path, _REF)
    assert completed.returncode == 0, completed.stderr
    text = manifest.read_text(encoding="utf-8")
    assert "placeholder until the stdio-box image release records the digest" not in text
    assert f'image: "{_REF}"' in text
    assert text == (
        "schema_version: 1\n"
        "name: atlassian\n"
        "version: 1.0.0\n"
        "containers:\n"
        "  mcp:\n"
        f'    image: "{_REF}"\n'
        "needs: []\n"
    )


def test_pin_keeps_a_comment_that_is_not_the_exact_placeholder(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    (tmp_path / "IMAGE").write_text("keep\n", encoding="utf-8")
    package = tmp_path / "packages" / "atlassian"
    package.mkdir(parents=True)
    manifest = package / "keystone-service.yaml"
    old = f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}"
    manifest.write_text(
        "schema_version: 1\n"
        "name: atlassian\n"
        "version: 1.0.0\n"
        "containers:\n"
        "  mcp:\n"
        "    # pinned by release automation, do not edit by hand\n"
        f'    image: "{old}"\n'
        "needs: []\n",
        encoding="utf-8",
    )
    completed = _run(tmp_path, _REF)
    assert completed.returncode == 0, completed.stderr
    text = manifest.read_text(encoding="utf-8")
    assert "# pinned by release automation, do not edit by hand" in text
    assert f'image: "{_REF}"' in text


def test_pin_preserves_crlf_around_the_image_line(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    package = tmp_path / "packages" / "atlassian"
    package.mkdir(parents=True)
    manifest = package / "keystone-service.yaml"
    old = f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}"
    manifest.write_bytes(
        (
            "# keep\r\n"
            "schema_version: 1\r\n"
            "name: atlassian\r\n"
            "version: 1.0.0\r\n"
            "containers:\r\n"
            "  mcp:\r\n"
            f'    image: "{old}" # stay\r\n'
            "needs: []\r\n"
        ).encode()
    )
    before = manifest.read_bytes()
    completed = _run(tmp_path, _REF)
    assert completed.returncode == 0, completed.stderr
    after = manifest.read_bytes()
    assert _changed_lines(before, after) == [6]
    assert after.splitlines(keepends=True)[0] == b"# keep\r\n"
    assert after.splitlines(keepends=True)[6] == f'    image: "{_REF}" # stay\r\n'.encode()


def test_pin_rejects_a_bad_ref_without_writing(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    (tmp_path / "IMAGE").write_text("keep\n", encoding="utf-8")
    manifest = _write_package(
        tmp_path,
        "atlassian",
        f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}",
    )
    before = manifest.read_bytes()
    completed = _run(tmp_path, "sha256:" + _NEW)
    assert completed.returncode == 2
    assert "image ref must look like" in completed.stderr
    assert manifest.read_bytes() == before
    assert (tmp_path / "IMAGE").read_text(encoding="utf-8") == "keep\n"


def test_pin_rejects_a_version_that_does_not_match_pyproject(tmp_path: Path) -> None:
    _pyproject(tmp_path, version="0.1.0")
    (tmp_path / "IMAGE").write_text("keep\n", encoding="utf-8")
    _write_package(
        tmp_path,
        "atlassian",
        f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}",
    )
    ref = f"ghcr.io/keystone-codes/stdio-box:0.2.0@sha256:{_NEW}"
    completed = _run(tmp_path, ref)
    assert completed.returncode == 1
    assert "does not match" in completed.stderr
    assert (tmp_path / "IMAGE").read_text(encoding="utf-8") == "keep\n"


def test_pin_fails_when_the_manifest_has_no_single_image_line(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    (tmp_path / "IMAGE").write_text("keep\n", encoding="utf-8")
    package = tmp_path / "packages" / "atlassian"
    package.mkdir(parents=True)
    manifest = package / "keystone-service.yaml"
    manifest.write_text("name: atlassian\nversion: 1.0.0\n", encoding="utf-8")
    missing = _run(tmp_path, _REF)
    assert missing.returncode == 1
    assert "exactly one" in missing.stderr
    assert (tmp_path / "IMAGE").read_text(encoding="utf-8") == "keep\n"

    body = _manifest(
        "atlassian",
        f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}",
    )
    manifest.write_text(body + body, encoding="utf-8")
    before = manifest.read_bytes()
    doubled = _run(tmp_path, _REF)
    assert doubled.returncode == 1
    assert "exactly one" in doubled.stderr
    assert manifest.read_bytes() == before


def test_pin_fails_for_an_unknown_package_without_writing(tmp_path: Path) -> None:
    _pyproject(tmp_path)
    (tmp_path / "IMAGE").write_text("keep\n", encoding="utf-8")
    manifest = _write_package(
        tmp_path,
        "atlassian",
        f"ghcr.io/keystone-codes/stdio-box:0.1.0@sha256:{_ZERO}",
    )
    before = manifest.read_bytes()
    completed = _run(tmp_path, _REF, "--packages", "missing")
    assert completed.returncode == 1
    assert "unknown package missing" in completed.stderr
    assert manifest.read_bytes() == before
    assert (tmp_path / "IMAGE").read_text(encoding="utf-8") == "keep\n"
