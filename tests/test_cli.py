"""CLI exit codes for the commands this revision implements."""

import shutil
from pathlib import Path

import pytest

from stdio_box.cli import credsctl_main, main

ECHO_DIR = Path(__file__).resolve().parent / "data" / "packages" / "echo"


def test_validate_provider_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as good:
        main(["validate-provider", str(ECHO_DIR)])
    assert good.value.code == 0
    assert capsys.readouterr().out == ""

    dest = tmp_path / "echo"
    shutil.copytree(ECHO_DIR, dest)
    text = (
        (dest / "provider.yaml").read_text(encoding="utf-8").replace("name: echo", "name: other", 1)
    )
    (dest / "provider.yaml").write_text(text, encoding="utf-8")
    with pytest.raises(SystemExit) as bad:
        main(["validate-provider", str(dest)])
    assert bad.value.code == 1
    captured = capsys.readouterr()
    assert "name: must equal directory name 'echo'" in captured.out


def test_launch_reports_install_is_not_implemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["launch"])
    assert exc.value.code == 2
    assert "provider install is not implemented yet" in capsys.readouterr().err


def test_credsctl_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        credsctl_main([])
    assert exc.value.code == 2
    assert capsys.readouterr().err.startswith("usage: credsctl")
