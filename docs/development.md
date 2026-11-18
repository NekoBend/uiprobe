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

## File access in the tests

The offline test guard is a review aid, not a defense against deliberate evasion (its known limits are listed in `tests/test_offline_guard.py`). It flags every reference in `src/uiprobe` to a name that can open or change a file, except the call sites on its allowlist. It approves `tomllib` as an import in every module, like `json` and `re`, because it only parses text it is given, and it allows two file calls, in `settings.py` only: the create-only `open(path, "x", ...)` and the one read-only `os.open(path, _READ_FLAGS)` of the reader. The flags are a module constant that the guard checks by AST (only `O_RDONLY`, `O_NONBLOCK`, `O_CLOEXEC`, `O_NOCTTY`, `O_BINARY` and `O_NOINHERIT`, combined with `|`) and at runtime (none of `O_WRONLY`, `O_RDWR`, `O_CREAT`, `O_TRUNC`, `O_APPEND`, `O_EXCL`, `O_TEMPORARY`).
