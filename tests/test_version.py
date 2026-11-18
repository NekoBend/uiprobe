"""The version is written in pyproject.toml and in the package; they match."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import uiprobe
from uiprobe import cli

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_the_package_version_matches_pyproject():
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert uiprobe.__version__ == project["version"]


def test_version_prints_one_line_and_exits_zero(capsys):
    assert cli.run(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"uiprobe {uiprobe.__version__}\n"
    assert captured.err == ""


def test_version_ignores_json(capsys):
    assert cli.run(["--json", "--version"]) == 0
    assert capsys.readouterr().out == f"uiprobe {uiprobe.__version__}\n"


@pytest.mark.parametrize("command", ["devices", "dump"])
def test_version_after_a_command_is_a_usage_error(capsys, command):
    assert cli.run([command, "--version"]) == 2
    assert "--version" in capsys.readouterr().err
