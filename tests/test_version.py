"""The version is written in pyproject.toml, the package and the README."""

from __future__ import annotations

import io
import re
import tomllib
from pathlib import Path

import pytest

import uiprobe
from uiprobe import cli

ROOT = Path(__file__).resolve().parent.parent


class _ClosedPipe(io.StringIO):
    """A stdout whose reader is gone: every write fails."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


def test_the_package_version_matches_pyproject():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert uiprobe.__version__ == project["version"]


def test_the_readme_names_the_version():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    named = re.findall(r"\bversion (\d[0-9A-Za-z.+]*[0-9A-Za-z])", readme)
    assert named
    assert set(named) == {uiprobe.__version__}


def test_version_prints_one_line_and_exits_zero(capsys):
    assert cli.run(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"uiprobe {uiprobe.__version__}\n"
    assert captured.err == ""


def test_version_stays_one_line_in_a_narrow_terminal(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "18")
    assert cli.run(["--version"]) == 0
    assert capsys.readouterr().out == f"uiprobe {uiprobe.__version__}\n"


def test_version_ignores_json(capsys):
    assert cli.run(["--json", "--version"]) == 0
    assert capsys.readouterr().out == f"uiprobe {uiprobe.__version__}\n"


def test_version_to_a_closed_output_exits_one(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdout", _ClosedPipe())
    assert cli.run(["--version"]) == 1
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", ["devices", "dump"])
def test_version_after_a_command_is_a_usage_error(capsys, command):
    assert cli.run([command, "--version"]) == 2
    assert "--version" in capsys.readouterr().err
