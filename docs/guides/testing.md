# Testing your project against the bridge

If your project depends on `reachy-mini-bridge`, you test your robot code the same way the
bridge tests itself: **unit tests against the `fake` backend, e2e tests against a live
`sim` or `real` daemon.** The bridge ships the e2e harness as importable code
(`reachy_mini_bridge.testing`), so you get the daemon lifecycle, its platform gotchas, and
the capability-gating for free — you don't re-derive any of it.

For the design behind this, see [specs/testing/testing_support.md](../../specs/testing/testing_support.md).

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
it (see [running-daemons.md](running-daemons.md) "The MuJoCo version").

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

**Run the live tier serially.** Every live test file shares the one daemon at `REACHY_MINI_HOST:REACHY_MINI_PORT`, and pytest-xdist workers would race to spawn it, borrow one another's and stop it under each other. The plugin forces no worker count (that would reach your whole run), so if your project runs xdist by default, pass `-n 0` for the live tier — `pytest tests-e2e -n 0 -rs` — or force it from that tier's own `conftest.py`, as the bridge's does:

```python
# tests-e2e/conftest.py
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    config.option.numprocesses = 0  # loads only when this directory is collected
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
uses the sim's portrait — see "Testing tracking without a person" below. `live_bridge`
starts with detection and tracking off: a test turns on what it needs, and the
`face_scene` fixture turns both off again after it, so a test that measures the head is
never pulled off its pose by someone standing in front of the robot.)

`require_env("SOME_API_KEY")` is the credential counterpart: it skips the test when the
variable is unset, so a contributor with no keys is never broken. An optional dependency
gets the same treatment with pytest's own `pytest.importorskip("module")`: the bridge's
three TTS provider tests skip on the missing provider module before they check the key,
so a sync without the `tts` dependency group skips them rather than fail.

Keep e2e tests in their own directory that your default `pytest` run doesn't collect (the
bridge uses a separate `tests-e2e/` and points `testpaths` at `tests/`), so the normal dev
loop stays fast and daemon-free.

### Capabilities

`requires_caps(live_bridge, ...)` accepts the capabilities the harness probes against the live
daemon at setup:

| Capability | What the probe checks |
|---|---|
| `motion` | the backend reports a status |
| `audio` | recording yields a mic sample (a sound device — or a PulseAudio null sink, below) |
| `camera` | a camera frame comes back on `bridge.camera` (needs a GL context: offscreen through EGL on Linux, a window elsewhere) |
| `gravity_compensation` | the hardware daemon runs the Placo kinematics engine — a harness-spawned one does with `REACHY_MINI_E2E_KINEMATICS=placo` (the `placo` extra); never a sim, which accepts the mode and does nothing |
| `faces` | the daemon runs the bridge's test scene (every sim the harness spawns does), which has a pool of portraits (bodies of kind `face`) — hidden until spawned; the bridge's `yunet` detector, which `live_bridge` configures, finds it in the rendered camera |
| `face_markers` | the daemon draws the faces the bridge sends it and returns them (`/api/sim/displays/face_markers`): every viewer sim the harness spawns; a test reads back where the bridge placed a face |
| `doa` | mic-array direction of arrival (reserved: no test uses it yet) |

Which setup provides which — a headless sim's camera on Linux but not on macOS, gravity compensation on a robot only, the portrait invisible to a webcam — is the one matrix in [../reference/backends-and-capabilities.md](../reference/backends-and-capabilities.md#the-matrix); read a probe's absence as "those tests skip".

**A Linux box without a sound card** (a server, a CI runner) has no default source or sink, so the daemon's audio comes up unavailable and `audio` probes absent: every audio test skips. A PulseAudio null sink, started before the daemon, makes it present — the daemon takes the sink and its monitor as the default devices, `say` and `play_sound` stream to a sink nobody hears, the mic tap reads silence, and the audio tests assert on the pipeline, not on what is heard:

```
pulseaudio --start --exit-idle-time=-1
pactl load-module module-null-sink sink_name=ci
pactl set-default-sink ci
pactl set-default-source ci.monitor
```

This is what the bridge's own CI does ([../specs/testing/ci.md](../../specs/testing/ci.md)); the Linux packages and the webrtc plugin a daemon needs are in [linux.md](linux.md).

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
| `REACHY_MINI_E2E_REQUIRED_CAPS` | unset | comma-separated capabilities the run must have (`motion,audio,camera,faces` in the bridge's CI): a missing one, or a daemon the harness cannot bring up, **fails** the fixture instead of skipping |
| `REACHY_MINI_E2E_MEDIA_BACKEND` | by host | the `media_backend` the fixture's bridge connects with; by default `local` on a loopback host and upstream's `default` (WebRTC) for a remote robot |
| `REACHY_MINI_E2E_KINEMATICS` | `analytical` | the kinematics engine of the daemon the harness spawns, sim or real: `analytical`, `placo` (needs the `placo` extra) or `nn` — the same suite runs on each, and the sim solves every target through the engine chosen ([configuration.md](../reference/configuration.md#kinematics-engines)); a borrowed daemon runs its own, so set this to match it. Another word fails the run |

**Testing tracking without a person.** Every sim the harness spawns runs the bridge's test
scene: upstream's empty scene plus a pool of portraits (`face_1` … `face_3`) that stay hidden until a test spawns them, so
tests that don't use it are unaffected, and `faces` is probed on every spawned sim. With a
camera — the viewer (`REACHY_MINI_E2E_SIM_VIEWER=1`), or a Linux headless sim, which renders
it offscreen — the `sim_scene` fixture (from the same plugin module) hands you a `SimSceneClient` to spawn portraits — as many at once as the
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

See [specs/testing/sim_scene.md](../../specs/testing/sim_scene.md) for the scene's geometry, the pool, the endpoint,
and the angles the head settles at. For trying things by hand with *yourself* in front of the
sim, a sim config with `"daemon": {"camera": {"source": "webcam"}}` uses the computer's
webcam as the robot's camera ([running-daemons.md](running-daemons.md)).

**Own it or borrow it:** the fixture reuses a daemon already reachable at the address
(never tears it down); otherwise it spawns one and owns its teardown — a MuJoCo daemon for
`sim`, and for `real` on a loopback address (a robot plugged into this machine over USB)
the hardware daemon (upstream's `reachy-mini-daemon`, run through the bridge's real daemon launcher — [../specs/daemon/real_daemon.md](../../specs/daemon/real_daemon.md)), which finds the robot's serial port itself, wakes
the robot, and puts it to sleep when the fixture stops it. A wireless robot runs its own
daemon: point `REACHY_MINI_HOST` at it — the fixture then leaves the media backend to
upstream's default, the WebRTC stream the robot serves (a local daemon gets the IPC path,
`media_backend="local"`). When it can't bring one up (missing sim extra, busy port, no robot
answering), the test **skips** rather than failing — unless `REACHY_MINI_E2E_REQUIRED_CAPS`
names capabilities the run must have, in which case it fails, as it does when a required
capability isn't probed (what the bridge's CI does with `motion,audio,camera,faces`).

**Gravity compensation** needs the daemon's Placo kinematics engine, which is a choice, not
an install: with the `placo` extra installed, `REACHY_MINI_E2E_KINEMATICS=placo` makes a
harness-spawned `real` daemon run it (the default run keeps upstream's analytical engine), and
a daemon you start yourself needs `--kinematics-engine Placo`. On any other engine
`bridge.set_motors_state("gravity_compensation")` raises `GravityCompensationUnsupportedError`
(sending the mode would make the robot daemon close the connection), so gate such tests on
`requires_caps(live_bridge, "gravity_compensation")`. A sim never probes it, whatever its engine:
it accepts the mode and does nothing. See
[running-daemons.md](running-daemons.md) for the launch recipes and the
macOS viewer notes.

**Outside pytest,** the same lifecycle is available to your application: a
`ReachyMiniConfig` with `"backend": "sim"` (or `"real"`, for a robot plugged in over USB)
and `"daemon": {"spawn": "auto"}` makes `async with ReachyMiniBridge(config)` spawn (or
borrow) the daemon itself — see
[specs/core/config.md](../../specs/core/config.md) and [specs/daemon/daemon.md](../../specs/daemon/daemon.md).
