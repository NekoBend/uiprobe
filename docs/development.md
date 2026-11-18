# Development

With [uv](https://docs.astral.sh/uv/):

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

CI runs these checks and the tests on Linux and Windows with Python 3.12, 3.13 and 3.14. On both systems it also builds the wheel with Python 3.12, installs it outside the checkout, imports `uiprobe` from the installed wheel and runs `uiprobe --help`. macOS is not tested in CI yet.

The version is written in two places: `version` in `pyproject.toml` and `__version__` in `src/uiprobe/__init__.py`. Change both together; `tests/test_version.py` fails when they differ.

The tests need no device and no adb. They use a fake adb server on a loopback port, and a guard makes any test that tries to reach another address or start another program fail. The guard allows only a launch of a program by absolute path under the test's temporary folder, with the single argument `start-server`, `ADB_MDNS=0` in its environment and no adb address variables. The start-server tests launch their fake adb under that rule on purpose and check how it was invoked. These tests, and the other tests that use a fake adb shell script (in `tests/test_start_server.py`, `tests/test_adb.py` and `tests/test_network_guard.py`), are skipped on Windows. For a stronger guarantee on Linux, run the tests inside a network namespace that has no route out:

```sh
uv sync
unshare -rn uv run --offline pytest
```

To make the test session refuse to start when the process still has a route out of the machine, set `UIPROBE_TEST_REQUIRE_OFFLINE=1`. The session then stops with a message unless every route in `/proc/net/route` and `/proc/net/ipv6_route` goes through the loopback interface:

```sh
UIPROBE_TEST_REQUIRE_OFFLINE=1 unshare -rn uv run --offline pytest
```

The network rules are in [network.md](network.md).
