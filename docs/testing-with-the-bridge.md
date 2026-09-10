# Testing your project against the bridge

If your project depends on `reachy-mini-bridge`, you test your robot code the same way the
bridge tests itself: **unit tests against the `fake` backend, e2e tests against a live
`sim` or `real` daemon.** The bridge ships the e2e harness as importable code
(`reachy_mini_bridge.testing`), so you get the daemon lifecycle, its platform gotchas, and
the capability-gating for free — you don't re-derive any of it.

For the design behind this, see [specs/testing_support.md](../specs/testing_support.md).

## Backends → tiers → extras

| Your tier | Backend | Install | Daemon |
|---|---|---|---|
| Unit / integration | `fake` | base (`reachy-mini-bridge`) | none — offline, deterministic |
| Live / e2e | `sim` | `reachy-mini-bridge[sim,test]` | MuJoCo, harness-managed |
| Live / e2e | `real` | `reachy-mini-bridge[test]` | your robot at host/port |

Importing the package pulls in `reachy_mini` (a base dependency) — that needs its native
libs installed, **not** a running daemon. The `fake` path needs no daemon and no extra.

## Unit tests — the `fake` backend

Construct `ReachyMiniApi("fake")` directly. It records the commands it receives and returns
synthetic perception, with no daemon, network, or hardware. Assert through the `api.robot`
escape hatch:

```python
import pytest
from reachy_mini_bridge.api import ReachyMiniApi


@pytest.mark.asyncio  # or drive the coroutine with asyncio.run(...)
async def test_my_greeting_moves_the_head():
    async with ReachyMiniApi("fake") as api:
        await my_greeting(api)  # your code under test
        # assert on what the fake recorded, via the escape hatch:
        assert api.robot.recorded_commands  # shape depends on your code
```

## E2E tests — `sim` / `real`

Opt into the shipped pytest plugin from your **root** `conftest.py`:

```python
# conftest.py
pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]
```

That gives you the module-scoped `live_api` fixture. Gate each test on the capabilities it
needs with `requires_caps` — a test skips (never fails) where the current target can't meet
its needs, so one test runs unchanged on the headless sim, the headfull viewer, or a real
robot:

```python
import asyncio

from reachy_mini_bridge.testing import require_env, requires_caps


def test_it_speaks(live_api):
    requires_caps(live_api, "audio")
    api, _caps = live_api
    asyncio.run(api.say("hello", my_synth))


def test_it_follows_a_face(live_api):
    requires_caps(live_api, "motion")
    api, _caps = live_api
    ...
```

`require_env("SOME_API_KEY")` is the credential counterpart: it skips the test when the
variable is unset, so a contributor with no keys is never broken.

Keep e2e tests in their own directory that your default `pytest` run doesn't collect (the
bridge uses a separate `tests-e2e/` and points `testpaths` at `tests/`), so the normal dev
loop stays fast and daemon-free.

### Capabilities

`requires_caps(live_api, ...)` accepts the capabilities the harness probes against the live
daemon at setup:

| Capability | Meaning | sim headless | sim headfull | real robot |
|---|---|---|---|---|
| `motion` | the backend reports a status | ✅ | ✅ | ✅ |
| `audio` | recording yields a mic sample | ✅ | ✅ | ✅ |
| `camera` | a camera frame comes back (needs a GL context) | ⚠️ not on headless macOS | ✅ | ✅ |
| `doa` | mic-array direction of arrival | ❌ | ❌ | ✅ (reserved) |

Capabilities are **probed, not assumed** from the backend type — environment quirks decide
what actually works.

## Configuration (environment variables)

The `live_api` fixture reads the same knobs the bridge's own tier uses:

| Variable | Default | Meaning |
|---|---|---|
| `REACHY_MINI_E2E_TARGET` | `sim` | `sim` or `real` |
| `REACHY_MINI_HOST` | `127.0.0.1` | daemon host (borrow one already running, or your robot) |
| `REACHY_MINI_PORT` | `8000` | daemon port |
| `REACHY_MINI_E2E_SIM_VIEWER` | unset | `1` to launch the headfull MuJoCo viewer (local; needs a GUI/GL context) |

**Own it or borrow it:** the fixture reuses a daemon already reachable at the address
(never tears it down); otherwise, for `sim` only, it spawns a MuJoCo daemon and owns its
teardown — it never spawns for `real`. When it can't bring one up (missing sim extra, busy
port, robot unreachable), the test **skips** rather than failing. See
[running-the-sim-daemon.md](running-the-sim-daemon.md) for the launch recipes and the
macOS viewer notes.
