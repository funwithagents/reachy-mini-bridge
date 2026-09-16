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

Construct `ReachyMiniApi("fake")` directly. The fake records every command it receives on
`robot.commands` (a list of `(name, args)` tuples) and returns synthetic perception, with no
daemon, network, or hardware. Assert through the `api.robot` escape hatch — narrow it to
`FakeReachyMini` first, since `api.robot` is typed as the real-or-fake union:

```python
import asyncio

from reachy_mini_bridge import ReachyMiniApi
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


def test_my_greeting_plays_an_emotion():
    async def run() -> list[str]:
        async with ReachyMiniApi("fake") as api:
            await api.set_motors_state("enabled")
            await my_greeting(api)  # your code under test
            robot = api.robot  # the escape hatch: the FakeReachyMini
            assert isinstance(robot, FakeReachyMini)  # narrows the union for pyright
            return [name for name, _args in robot.commands]

    assert "async_play_move" in asyncio.run(run())
```

The bridge's own fast tier drives coroutines with `asyncio.run` and needs no pytest-asyncio;
use that plugin if you prefer `async def` tests.

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
| `gravity_compensation` | hardware daemon on the Placo kinematics engine | ❌ | ❌ | ✅ with `reachy-mini[placo_kinematics]` |
| `doa` | mic-array direction of arrival | ❌ | ❌ | ✅ (reserved) |

Capabilities are **probed, not assumed** from the backend type — environment quirks decide
what actually works.

**Audio devices.** Every target picks the audio card named "Reachy Mini Audio" when one is
plugged in, so the `sim` target plays through (and records from) a USB-connected robot
too; without one it uses the machine's default speaker and mic. Don't stop and restart the
media pipeline in a test (`api.robot.media.stop_recording()` then `start_recording()`): on
macOS the restarted pipeline reopens on the system defaults, so the rest of the module's
audio silently leaves the robot.

## Configuration (environment variables)

The `live_api` fixture reads the same knobs the bridge's own tier uses:

| Variable | Default | Meaning |
|---|---|---|
| `REACHY_MINI_E2E_TARGET` | `sim` | `sim` or `real` |
| `REACHY_MINI_HOST` | `127.0.0.1` | daemon host (borrow one already running, or your robot; loopback lets the harness start a USB robot's daemon) |
| `REACHY_MINI_PORT` | `8000` | daemon port |
| `REACHY_MINI_E2E_SIM_VIEWER` | unset | `1` to launch the headfull MuJoCo viewer (local; needs a GUI/GL context) |

**Own it or borrow it:** the fixture reuses a daemon already reachable at the address
(never tears it down); otherwise it spawns one and owns its teardown — a MuJoCo daemon for
`sim`, and for `real` on a loopback address (a robot plugged into this machine over USB)
the hardware daemon, `reachy-mini-daemon`, which finds the robot's serial port itself, wakes
the robot, and puts it to sleep when the fixture stops it. A wireless robot runs its own
daemon: point `REACHY_MINI_HOST` at it. When it can't bring one up (missing sim extra, busy
port, no robot answering), the test **skips** rather than failing.

**Gravity compensation** needs the daemon's Placo kinematics engine. Install
`reachy-mini[placo_kinematics]` and a harness-spawned `real` daemon uses it automatically; a
daemon you start yourself needs `--kinematics-engine Placo`. Without it,
`api.set_motors_state("gravity_compensation")` raises `GravityCompensationUnsupportedError`
(sending the mode would make the robot daemon close the connection), so gate such tests on
`requires_caps(live_api, "gravity_compensation")`. See
[running-the-sim-daemon.md](running-the-sim-daemon.md) for the launch recipes and the
macOS viewer notes.

**Outside pytest,** the same lifecycle is available to your application: a
`ReachyMiniConfig` with `"backend": "sim"` (or `"real"`, for a robot plugged in over USB)
and `"daemon": {"spawn": "auto"}` makes `async with ReachyMiniApi(config)` spawn (or
borrow) the daemon itself — see
[specs/config.md](../specs/config.md) and [specs/daemon.md](../specs/daemon.md).
