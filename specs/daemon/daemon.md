---
code:
  - src/reachy_mini_bridge/daemon.py
  - src/reachy_mini_bridge/errors.py
tests:
  - tests/test_daemon.py
---

# Daemon lifecycle (`daemon.py`)

**Status:** Implemented

## Purpose

The bridge owns bringing up — and tearing down — a local `reachy-mini-daemon`: a MuJoCo one for the `sim` backend, and a hardware one for a robot plugged into this machine over USB (Reachy Mini Lite), so that a `ReachyMiniBridge` whose config asks for it ([config.md](../core/config.md) `daemon.spawn`) produces a working simulated or USB-attached robot in one call, and so that the shipped testing harness ([testing_support.md](../testing/testing_support.md)) — which brings up either kind — and any consumer's own tooling reuse one implementation of the launch recipes in [../docs/guides/running-daemons.md](../../docs/guides/running-daemons.md).

Upstream's `ReachyMini` is a *client*: it needs a separately running daemon (hardware, or the MuJoCo simulation) and connects to it in its constructor (see [../docs/internals/upstream-sdk-notes.md](../../docs/internals/upstream-sdk-notes.md)). This module is the piece between "a config that says `sim` (or `real`, for a robot on this machine)" and "a daemon that is ready to accept that client": it launches the right variant, waits for actual readiness, keeps the child's environment sane, and stops exactly what it started.

## Core concepts / Decided

### Public surface

```python
class DaemonError(BridgeError): ...


@dataclass(frozen=True)
class DaemonHandle:
    host: str
    port: int
    # True when start_daemon spawned the process; stop() then stops it
    owned: bool
    # the spawned process, when owned
    pid: int | None

    def stop(self) -> None: ...  # terminate, grace, kill an owned daemon; a no-op on a borrowed one


def is_daemon_ready(host: str, port: int) -> bool: ...


def launch_command(
    config: DaemonConfig,
    *,
    backend: str = "sim",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> list[str]: ...


def start_daemon(
    config: DaemonConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    backend: str = "sim",
) -> DaemonHandle: ...


@contextmanager
def managed_daemon(
    config: DaemonConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    backend: str = "sim",
) -> Iterator[DaemonHandle]: ...
```

`backend` is `"sim"` (the default) or `"real"` and selects the launch recipe below; anything else is a `ValueError`. `start_daemon` and `DaemonHandle.stop()` are the lifecycle pair — the daemon's counterpart of the `start()` / `stop()` every session of the bridge has ([bridge.md](../core/bridge.md) "Lifecycle"), **synchronous** because they are subprocess and socket work; the bridge runs each off the event loop with `asyncio.to_thread`. `managed_daemon` is the context-manager form over the pair — `start_daemon` on entry, the handle's `stop()` on exit, on every exit path — for the testing harness and any synchronous caller. `DaemonConfig` is [config.md](../core/config.md)'s block — `daemon.py` imports `config.py`, a one-way dependency.

### Own it or borrow it

`start_daemon` (and so `managed_daemon`) follows `config.spawn`:

- **`auto`** — if a daemon is already ready at `host:port`, yield it **borrowed** (`owned=False`) and never stop it: a viewer sim the user started by hand, or a daemon another process owns. If the port is open but the daemon is not yet ready (still booting), wait for readiness up to `startup_timeout`, then borrow it. If the port is free, **spawn** and own it.
- **`always`** — spawn and own; the port already open is a `DaemonError` (the caller asked for a fresh daemon and something else holds the address).
- **`never`** — not a `start_daemon` mode: the bridge simply connects, and this module is not involved.

`host` must be a loopback address — the bridge only ever spawns on the local machine (enforced at config time, [config.md](../core/config.md)).

### Readiness means the backend is up

`is_daemon_ready(host, port)` reads the daemon's status over plain HTTP — `GET /api/daemon/status` (`status_url(host, port)` builds the URL, an IPv6 host bracketed), a 3 s timeout, the JSON body's `backend_status` — and reports `backend_status is not None`. This is stronger than an open port: the daemon serves nothing until it has woken, and the status carries a `backend_status` only once the backend runs — a MuJoCo backend that failed to start (no GL context, for instance) leaves it `None`. Only a non-`None` backend status means a client can drive the robot. Any exception during the probe, and any body that is not a JSON object, reads as "not ready".

**The probe has no side effect on the daemon.** It is one HTTP read, never an SDK client: a `reachy_mini.ReachyMini` built with `media_backend="no_media"` (upstream 1.10 / 1.11) calls `release_media()` on the daemon at construction and `acquire_media()` at exit, and the daemon answers each pair by stopping and rebuilding its whole media pipeline — WebRTC peers dropped, audio interrupted, and on macOS the camera drawn again by the unstable `avfvideosrc` index ([real_daemon.md](real_daemon.md)). A readiness probe that rebuilt the daemon's media on every successful poll was one of the two ways a real robot's camera came up dead.

After spawning, `start_daemon` polls `is_daemon_ready` once per second until `startup_timeout`. If the child exits first, it raises `DaemonError` with the exit code and the launch command; on timeout it stops the child and raises `DaemonError` (the message includes the viewer hint below for a `sim` daemon with `headless` `false`).

### The launch command

`launch_command(config, backend=..., host=..., port=...)` builds the argv — `host` / `port` the address `start_daemon` was given, which every recipe ends with as the **address flags** (below). Every `sim` daemon runs through the bridge's **sim daemon launcher** ([sim_daemon.md](sim_daemon.md)) — upstream's daemon with the choice of camera source (rendered eye camera or a host webcam) and the viewer's camera overlay — from the recipes in [../docs/guides/running-daemons.md](../../docs/guides/running-daemons.md):

- **headless** (`config.headless`, the default): `<this interpreter> -m reachy_mini_bridge.sim_daemon --headless --[no-]preload-datasets [--scene <scene>] [camera flags] [address flags]` — real MuJoCo physics, no viewer, runs anywhere (CI's target, [../testing/ci.md](../testing/ci.md)); with the default `sim` camera the eye camera renders offscreen on Linux and not at all on macOS ([sim_daemon.md](sim_daemon.md) "The headless camera"), with a `webcam` camera the host camera's frames flow headless on either.
- **viewer** (`headless: false`): `<viewer interpreter> -m reachy_mini_bridge.sim_daemon --[no-]preload-datasets [--scene <scene>] [camera flags] [display flags] [address flags]` — opens the MuJoCo viewer, which supplies the render's GL context and lets a person watch the sim. The **viewer interpreter** is `mjpython` on macOS (the passive viewer runs only under it there) and `<this interpreter>` on any other platform. It needs an unlocked, interactive GUI session; a locked screen or a non-GUI process tree makes it hang or crash, and the `DaemonError` on that path says so.
- **camera flags** come from `config.camera` ([config.md](../core/config.md)): nothing for the default `sim` source; `--camera webcam [--webcam-device <device>] --webcam-hfov <degrees>` for `webcam` (the device only when set).
- **display flags** come from `config.sim_displays`: `--sim-display <name>` for each display that is on, after the camera flags, in `SIM_DISPLAYS` order — `camera_overlay`, `robot_gaze`, `face_markers` ([sim_daemon.md](sim_daemon.md) "Sim displays"). The config only lets a display on with `headless: false`, so they only ever reach a viewer recipe.

For `real` — a robot attached to this machine (USB):

- **hardware**: `<this interpreter> -m reachy_mini_bridge.real_daemon [--kinematics-engine Placo] --[no-]preload-datasets [address flags]` — the bridge's **real daemon launcher** ([real_daemon.md](real_daemon.md)): upstream's hardware daemon, run in-process with the macOS camera check that makes the robot's camera open reliably. No `--sim`; the daemon finds the robot's serial port itself, wakes the robot on start and puts it to sleep on stop. `--kinematics-engine Placo` is passed (through the launcher, verbatim) when the `placo` package is importable (`reachy-mini[placo_kinematics]`): the daemon's default engine rejects gravity compensation, and rejecting it drops the client's connection ([bridge.md](../core/bridge.md) "Motors"). `headless` and `scene` are sim knobs and play no part.

- **scene file** (`config.scene` ending in `.xml`, either launch mode): `<viewer interpreter> -m reachy_mini_bridge.testing.sim_scene --scene-path <abs> --[no-]preload-datasets [camera flags] [display flags] [address flags]` for the viewer, `<this interpreter> -m reachy_mini_bridge.testing.sim_scene --scene-path <abs> --headless --[no-]preload-datasets [camera flags] [address flags]` headless — the test scene's launcher (shipped in the testing package), which is the sim daemon launcher with the scene's extension installed: upstream's daemon on a scene *file* the bridge wrote (hidden-by-default props — a portrait plane today — with a director and an HTTP endpoint to show/place/hide them: [sim_scene.md](../testing/sim_scene.md)), with the same corrections and camera choice as every other sim. The path is made absolute at launch; the launcher checks the file exists. Any other `scene` value is an upstream scene *name*, passed as `--scene`.

`--preload-datasets` is passed when `config.preload_datasets` is `true` (the default) and `--no-preload-datasets` when it is `false` — always one of the two, because the daemon's own default is not to preload; the preload runs in the background and does not delay readiness. `--scene` (sim) when `config.scene` is set. Media stays **on** (no `--no-media`) so audio — and the camera, under the viewer or headless on Linux — are available; a consumer that wants a motion-only daemon runs its own.

The **address flags** `--fastapi-host <host> --fastapi-port <port>` end every recipe: upstream's daemon binds its HTTP API (the status, the SDK websocket, the sim routes) where they say, and `start_daemon` polls that same address for readiness. Without them the daemon binds upstream's defaults — port 8000, and a host that is loopback on a Lite and every interface on the wireless robot — whatever the caller asked, so a spawn on any other port waits out `startup_timeout` against an address nothing answers while the daemon sits on 8000 for someone else to borrow. The launchers forward the two flags to upstream unchanged (their `parse_known_args` passthrough — [sim_daemon.md](sim_daemon.md), [real_daemon.md](real_daemon.md), [sim_scene.md](../testing/sim_scene.md)). A `ReachyMiniBridge` that spawns its own daemon ([config.md](../core/config.md) `daemon.spawn`) therefore binds it to its `robot.host:port`, and two sims on one machine take two ports — HTTP ports, which keep their APIs apart and nothing more: upstream's media server binds a fixed UDP port and a fixed camera socket path whatever the HTTP address, so two media-on daemons on one host are not isolated from each other; the second runs `--no-media` ([../../docs/guides/running-daemons.md](../../docs/guides/running-daemons.md)).

The sim launchers (`mjpython` for the viewer on macOS; every `sim` recipe also needs `reachy-mini-daemon` present, as the sign the sim extra is installed) are resolved on `PATH`; a missing one is a `DaemonError` naming the `sim` extra (`reachy-mini-bridge[sim]`). The `real` recipe needs nothing on `PATH`: it runs in the bridge's own interpreter, and `reachy_mini` is a base dependency. The `DaemonError`s on the startup path name the backend (`sim daemon` / `real daemon`).

### The child's environment is scrubbed

The spawned process runs with the GStreamer-bundle variables removed from its inherited environment: `GST_PLUGIN_PATH_1_0`, `GST_PLUGIN_SYSTEM_PATH_1_0`, `GST_REGISTRY_1_0`, `GST_PLUGIN_SCANNER_1_0`, `GI_TYPELIB_PATH`, `PYGI_DLL_DIRS`, `XDG_DATA_DIRS`, `XDG_CONFIG_DIRS`. `reachy_mini`'s `gstreamer_bundle.pth` *prepends* to these at every Python startup, so a parent that has imported `reachy_mini` (every bridge process — `robot.py` imports it at module load) would hand the child already-set values that its own `.pth` doubles into paths GStreamer cannot exec, and the daemon segfaults in the in-process plugin scan. Scrubbing lets the child set fresh, correct values — the same thing upstream's own app launcher does.

### The child's output reaches the bridge's log

The child's stdout and stderr are merged into one pipe that a reader thread drains line by line, re-emitting each through the `reachy_mini_bridge.daemon.child` logger: a line carrying a level tag (`… - WARNING - …`, `ERROR`, `CRITICAL` — the daemon's logging format) at that level, every other line at `DEBUG`. A working daemon is therefore quiet in a default bridge log, while a problem inside it — a camera that will not open, a serial port in use, a port conflict — surfaces where the person running the bridge sees it, which a discarded stream never did. The thread also keeps the last lines, and the `DaemonError` of a launch that exits or never becomes ready quotes them (after the exit code and the command), so a failed startup carries the daemon's own words rather than an invitation to re-run it by hand.

### The child runs in its own session

The spawned daemon is started with `start_new_session=True` (`setsid()` in the child — the bridge's hosts are macOS and Linux) and its stdin detached (`DEVNULL`), so it belongs to neither the terminal's session nor its foreground process group. A terminal's Ctrl+C delivers `SIGINT` to every process in the foreground group; a daemon sharing that group would shut down *at the same moment* as the bridge process — closing its WebSocket clients with `1012 service restart` — and the bridge's ordered teardown ([bridge.md](../core/bridge.md) "Lifecycle") would then run against a daemon that is already gone: the motion loop's every tick failing (a warning per tick), upstream's wobbler printing tracebacks for the speech offsets still scheduled for a playing sound, the camera pipeline reporting end-of-stream, and the robot left wherever the dying daemon dropped it. In its own session the daemon sees no terminal signal at all: the bridge process alone gets the `KeyboardInterrupt`, exits the bridge against a live daemon (motion eases to neutral, tracking and wobbling are switched off daemon-side, media and the client close), and only then does teardown terminate the daemon, which uses its grace to put the robot to sleep. The order in which things stop is the bridge's to decide, and only the bridge stops the daemon.

### Teardown stops what the bridge started

`DaemonHandle.stop()` terminates an **owned** daemon (`SIGTERM`), gives it 10 seconds to exit, then kills it, and stops the reader of its output; a real daemon uses that grace to put the robot to sleep (measured at about 8 seconds on a Lite). On a **borrowed** daemon it is a no-op — the daemon is left running — and a second `stop()` is a no-op too. `managed_daemon` calls it on every exit path, including when the body raises.

**An orphaned daemon is the accepted trade-off of the detached session.** Because the daemon no longer shares the terminal's session, it outlives a bridge process that never reaches teardown — one that is `SIGKILL`ed, crashes without unwinding, or loses its terminal (the `SIGHUP` of a closed window goes to the terminal's session, which the daemon has left). Such a daemon keeps serving on its port, with the robot awake. On the next run, `daemon.spawn: "auto"` borrows it (`owned=False`, so the bridge never stops it) and `"always"` raises `DaemonError` naming the port already in use; `pkill -f reachy-mini-daemon` stops it by hand. A parent-death watchdog is deferred (open question 1).

### One implementation, two users

`ReachyMiniBridge.start()` calls `start_daemon` before building the robot when `daemon.spawn != "never"`, passing the config's `backend` (`sim` or `real`) to select the recipe, and its `stop()` ends with the handle's `stop()` ([bridge.md](../core/bridge.md) "Lifecycle"). The shipped testing harness's private `testing/_daemon.py` is a thin wrapper over the same functions — `is_daemon_ready` for the borrow decision, `managed_daemon(spawn="auto", backend=...)` to spawn a `sim` daemon or, on a loopback address, a `real` one — translating `DaemonError` into `pytest.skip` so the live tier still skips, never fails, when the environment cannot provide a daemon ([testing_support.md](../testing/testing_support.md)).

### Testable without a daemon

The process-spawning and readiness-probing steps are injectable seams (module-private callables `managed_daemon` resolves at call time), so the deterministic `tests/` tier drives the own-or-borrow decisions, the readiness loop, the exit-code and timeout errors, and the teardown against a scripted stand-in process — no `reachy-mini-daemon`, no `mujoco`. `launch_command` and the environment scrub are pure functions, tested directly. The real `_spawn` seam has its own tests on a trivial long-lived child (POSIX only): the child's session and process group differ from the spawner's, and a `SIGINT` sent to the spawner's whole process group leaves the child alive. The live tier exercises the real spawn through the harness.

## Relationship to the other specs

- **[config.md](../core/config.md):** `DaemonConfig` (the `daemon` block) is this module's input; the loopback-host rule and the `sim`/`real`-only rule are enforced there.
- **[sim_daemon.md](sim_daemon.md):** every `sim` recipe runs the bridge's sim daemon launcher (or the test scene's, built on it).
- **[real_daemon.md](real_daemon.md):** the `real` recipe runs the bridge's real daemon launcher; the readiness probe is side-effect-free so that it never re-draws the camera that launcher checks.
- **[bridge.md](../core/bridge.md):** the bridge's `start()` calls `start_daemon` first, then builds and enters the robot, then starts the media session; its `stop()` ends with the handle's `stop()`.
- **[robot.md](../core/robot.md):** the connection options a managed daemon needs (`network`, `local` media) are filled into the `robot` block by config; the readiness probe itself uses no client.
- **[testing_support.md](../testing/testing_support.md):** `testing/_daemon.py` wraps this module.

## Open questions

1. **Parent-death watchdog.** Stopping an orphaned daemon automatically ("Teardown" above) is deferred: Linux offers `prctl(PR_SET_PDEATHSIG, SIGTERM)` in a `preexec_fn`, macOS has no equivalent, and a cross-platform answer is a wrapper process polling `os.getppid()` — extra moving parts for a benign case (`auto` borrows the orphan; `pkill` stops it). Revisit if orphans prove to be a nuisance in practice.
