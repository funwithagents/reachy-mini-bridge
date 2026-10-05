# Testing your project against the bridge

If your project depends on `reachy-mini-bridge`, you test your robot code the same way the
bridge tests itself: **unit tests against the `fake` backend, e2e tests against a live
`sim` or `real` daemon.** The bridge ships the e2e harness as importable code
(`reachy_mini_bridge.testing`), so you get the daemon lifecycle, its platform gotchas, and
the capability-gating for free — you don't re-derive any of it.

For the design behind this, see [specs/testing/testing_support.md](../specs/testing/testing_support.md).

## Backends → tiers → extras

| Your tier | Backend | Install | Daemon |
|---|---|---|---|
| Unit / integration | `fake` | base (`reachy-mini-bridge`) | none — offline, deterministic |
| Live / e2e | `sim` | `reachy-mini-bridge[sim,test]` | MuJoCo, harness-managed |
| Live / e2e | `real` | `reachy-mini-bridge[test]` | your robot at host/port |

Importing the package pulls in `reachy_mini` (a base dependency) — that needs its native
libs installed, **not** a running daemon. The `fake` path needs no daemon and no extra.

The `sim` extra installs MuJoCo 3.3.x itself rather than through `reachy_mini[mujoco]`,
which pins 3.3.0; nothing to add on your side, but don't install that upstream extra next to
it (see [running-the-sim-daemon.md](running-the-sim-daemon.md) "The MuJoCo version").

## Unit tests — the `fake` backend

Construct `ReachyMiniBridge("fake")` directly. The fake records every command it receives on
`robot.commands` (a list of `(name, args)` tuples), every pose the motion loop streams to it
on `robot.targets` (a list of `(head, antennas, body_yaw)` tuples, one per tick), and returns
synthetic perception, with no daemon, network, or hardware. Assert through the `bridge.robot`
escape hatch — narrow it to `FakeReachyMini` first, since `bridge.robot` is typed as the
real-or-fake union.

Motion is asserted on `targets`, never on `commands`: the bridge plays emotions and every
idle move through its own motion loop, which streams `set_target` at 60 Hz, so no
`async_play_move` command ever reaches the robot. The fake's emotions are short stand-in
trajectories (a 0.3 s rise and fall of the head, 10 mm high) — the real recordings only play
on `sim` / `real` — and its breathing lifts the head at most 5 mm, which is what a test
measures against:

```python
import asyncio

from reachy_mini_bridge import ReachyMiniBridge
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini


async def my_greeting(bridge: ReachyMiniBridge) -> None:
    await bridge.play_emotion("happy")  # your code under test — whatever greets


def test_my_greeting_lifts_the_head():
    async def run() -> list[float]:
        async with ReachyMiniBridge("fake") as bridge:
            await bridge.set_motors_state("enabled")
            robot = bridge.robot  # the escape hatch: the FakeReachyMini
            assert isinstance(robot, FakeReachyMini)  # narrows the union for pyright
            before = len(robot.targets)
            await my_greeting(bridge)
            heads = [head for head, _antennas, _yaw in robot.targets[before:] if head is not None]
            return [float(head[2, 3]) for head in heads]  # the head's height over the greeting

    heights = asyncio.run(run())
    assert max(heights) > 0.008  # the emotion lifted the head; breathing alone never gets there
```

`len(robot.targets) > 0` would not do: the idle move streams targets whether or not the
greeting ran.

The bridge's own fast tier drives coroutines with `asyncio.run` and needs no pytest-asyncio;
use that plugin if you prefer `async def` tests.

## E2E tests — `sim` / `real`

Opt into the shipped pytest plugin from your **root** `conftest.py`:

```python
# conftest.py
pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]
```

That gives you the `live_bridge` fixture: one bridge session per test file, over one daemon
per `pytest` run (spawned by the first file that needs it, stopped at the end — or borrowed, if
one is already running at the address). Gate each test on the capabilities it
needs with `requires_caps` — a test skips (never fails) where the current target can't meet
its needs, so one test runs unchanged on the headless sim, the headfull viewer, or a real
robot:

```python
from reachy_mini_bridge.testing import require_env, requires_caps


def test_it_speaks(live_bridge):
    requires_caps(live_bridge, "audio")
    bridge, _caps = live_bridge
    live_bridge.run(bridge.say("hello", my_synth))


def test_it_nods(live_bridge):
    requires_caps(live_bridge, "motion")
    bridge, _caps = live_bridge
    ...
```

`live_bridge.run(...)` executes a coroutine on the one event loop the harness runs the
bridge on, from its `start()` to its `stop()`, and returns the result. Use it for
everything you await on the bridge rather than `asyncio.run`: the bridge is loop-bound
(its detection loop is an asyncio task on the loop that started it, its observables
publish there), so a coroutine run on a second loop would leave any task it starts to
die with that loop. `BridgeLoop`, from the same package, is the mechanism for a fixture
of your own — a second bridge session, say.

(A test that needs the robot to *see* something gates on `camera`, and on `faces` when it
uses the sim's portrait — see "Testing tracking without a person" below.)

`require_env("SOME_API_KEY")` is the credential counterpart: it skips the test when the
variable is unset, so a contributor with no keys is never broken.

Keep e2e tests in their own directory that your default `pytest` run doesn't collect (the
bridge uses a separate `tests-e2e/` and points `testpaths` at `tests/`), so the normal dev
loop stays fast and daemon-free.

### Capabilities

`requires_caps(live_bridge, ...)` accepts the capabilities the harness probes against the live
daemon at setup:

| Capability | Meaning | sim headless | sim headfull | real robot |
|---|---|---|---|---|
| `motion` | the backend reports a status | ✅ | ✅ | ✅ |
| `audio` | recording yields a mic sample | ✅ | ✅ | ✅ |
| `camera` | a camera frame comes back (needs a GL context) | ⚠️ not on headless macOS | ✅ | ✅ |
| `gravity_compensation` | hardware daemon on the Placo kinematics engine | ❌ | ❌ | ✅ with `reachy-mini[placo_kinematics]` |
| `faces` | the daemon runs the bridge's test scene (every sim the harness spawns does), which has a pool of portraits (bodies of kind `face`) — hidden until spawned; the bridge's `yunet` detector, which `live_bridge` configures, finds it in the rendered camera | ✅ (nothing looks at it: no camera) | ✅ | ❌ |
| `face_markers` | the daemon draws the faces the bridge sends it and returns them (`/api/sim/displays/face_markers`): every viewer sim the harness spawns; a test reads back where the bridge placed a face | ❌ no viewer | ✅ | ❌ |
| `doa` | mic-array direction of arrival | ❌ | ❌ | ✅ (reserved) |

Capabilities are **probed, not assumed** from the backend type — environment quirks decide
what actually works.

**Audio devices.** Every target picks the audio card named "Reachy Mini Audio" when one is
plugged in, so the `sim` target plays through (and records from) a USB-connected robot
too; without one it uses the machine's default speaker and mic. Don't stop and restart the
media pipeline in a test (`bridge.robot.media.stop_recording()` then `start_recording()`): on
macOS the restarted pipeline reopens on the system defaults, so the rest of the module's
audio silently leaves the robot.

## Configuration (environment variables)

The `live_bridge` fixture reads the same knobs the bridge's own tier uses:

| Variable | Default | Meaning |
|---|---|---|
| `REACHY_MINI_E2E_TARGET` | `sim` | `sim` or `real` |
| `REACHY_MINI_HOST` | `127.0.0.1` | daemon host (borrow one already running, or your robot; loopback lets the harness start a USB robot's daemon) |
| `REACHY_MINI_PORT` | `8000` | daemon port |
| `REACHY_MINI_E2E_SIM_VIEWER` | unset | `1` to launch the headfull MuJoCo viewer (local; needs a GUI/GL context) |

**Testing tracking without a person.** Every sim the harness spawns runs the bridge's test
scene: upstream's empty scene plus a pool of portraits (`face_1` … `face_3`) that stay hidden until a test spawns them, so
tests that don't use it are unaffected, and `faces` is probed on every spawned sim. With
`REACHY_MINI_E2E_SIM_VIEWER=1` (the camera needs the viewer), the `sim_scene` fixture (from the same
plugin module) hands you a `SimSceneClient` to spawn portraits — as many at once as the
pool holds — move and despawn them while your code runs, and `clear()` the scene between
tests; the bridge's own detector (the `yunet` detector `live_bridge` configures) finds
them in the rendered camera stream and the bridge's tracker does the rest, so the head
converges on the face as a robot's does and a test can assert how the head moves and where
it settles, not only that it moved. The **convergence kit**, `reachy_mini_bridge.testing.gaze`,
is what the bridge's own tracking tests assert with — `track_onto` samples the head until it
holds still, `assert_tracked` checks it went toward the face, past it by a bounded amount at
most once, never back, and settled on the yaw the face's position implies with the face at
the image centre; `arm_tracking` turns tracking on and waits for the previous aim to be
released; the thresholds are module constants measured on the viewer sim — so a test reads:

```python
from reachy_mini_bridge.testing.gaze import (
    LATERAL_M, arm_tracking, assert_tracked, face_at, track_onto,
)


def test_it_looks_at_whoever_is_there(live_bridge, face_scene):
    # `face_scene` (the same plugin module) gates on `camera` + `faces` and hands you the
    # scene with nobody in view, cleared again after the test
    bridge, _caps = live_bridge

    async def scenario():
        await bridge.set_motors_state("enabled")
        await arm_tracking(bridge)  # tracking on, nobody followed
        face = face_scene.spawn(face_at(LATERAL_M))  # 0.15 m to the robot's left -> "face_1"
        left = await track_onto(bridge, "the face on the left", LATERAL_M)
        face_scene.place(face, face_at(-LATERAL_M), duration=1.0)  # it walks to the right
        right = await track_onto(bridge, "the face on the right", -LATERAL_M, min_seconds=5.0)
        face_scene.despawn(face)  # nobody there: the head is handed back to the idle move
        return left, right

    left, right = live_bridge.run(scenario())
    assert_tracked(left)  # settled at ~+18° yaw, the tracked face near the image centre
    assert_tracked(right, pitch_ahead=left.pitch)  # the same pitch: it only moved sideways
```

(`face_scene.spawn((0.60, 0.0, 0.20))` puts a second, smaller face farther away;
`bridge.head_tracking.value.track_id` says which one the head follows. A test that plays a
recorded move takes the `emotions_library` fixture, which fetches the library into the
Hugging Face cache on a fresh machine and skips when it cannot.)

See [specs/testing/sim_scene.md](../specs/testing/sim_scene.md) for the scene's geometry, the pool, the endpoint,
and the angles the head settles at. For trying things by hand with *yourself* in front of the
sim, a sim config with `"daemon": {"camera": {"source": "webcam"}}` uses the computer's
webcam as the robot's camera ([running-the-sim-daemon.md](running-the-sim-daemon.md)).

**Own it or borrow it:** the fixture reuses a daemon already reachable at the address
(never tears it down); otherwise it spawns one and owns its teardown — a MuJoCo daemon for
`sim`, and for `real` on a loopback address (a robot plugged into this machine over USB)
the hardware daemon (upstream's `reachy-mini-daemon`, run through the bridge's real daemon launcher — [../specs/daemon/real_daemon.md](../specs/daemon/real_daemon.md)), which finds the robot's serial port itself, wakes
the robot, and puts it to sleep when the fixture stops it. A wireless robot runs its own
daemon: point `REACHY_MINI_HOST` at it. When it can't bring one up (missing sim extra, busy
port, no robot answering), the test **skips** rather than failing.

**Gravity compensation** needs the daemon's Placo kinematics engine. Install
`reachy-mini[placo_kinematics]` and a harness-spawned `real` daemon uses it automatically; a
daemon you start yourself needs `--kinematics-engine Placo`. Without it,
`bridge.set_motors_state("gravity_compensation")` raises `GravityCompensationUnsupportedError`
(sending the mode would make the robot daemon close the connection), so gate such tests on
`requires_caps(live_bridge, "gravity_compensation")`. See
[running-the-sim-daemon.md](running-the-sim-daemon.md) for the launch recipes and the
macOS viewer notes.

**Outside pytest,** the same lifecycle is available to your application: a
`ReachyMiniConfig` with `"backend": "sim"` (or `"real"`, for a robot plugged in over USB)
and `"daemon": {"spawn": "auto"}` makes `async with ReachyMiniBridge(config)` spawn (or
borrow) the daemon itself — see
[specs/core/config.md](../specs/core/config.md) and [specs/daemon/daemon.md](../specs/daemon/daemon.md).
