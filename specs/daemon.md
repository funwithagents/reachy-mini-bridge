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

The bridge owns bringing up — and tearing down — a local `reachy-mini-daemon`: a MuJoCo one for the `sim` backend, and a hardware one for a robot plugged into this machine over USB (Reachy Mini Lite), so that a `ReachyMiniApi` whose config asks for it ([config.md](config.md) `daemon.spawn`) produces a working simulated or USB-attached robot in one call, and so that the shipped testing harness ([testing_support.md](testing_support.md)) — which brings up either kind — and any consumer's own tooling reuse one implementation of the launch recipes in [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md).

Upstream's `ReachyMini` is a *client*: it needs a separately running daemon (hardware, or the MuJoCo simulation) and connects to it in its constructor (see [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)). This module is the piece between "a config that says `sim` (or `real`, for a robot on this machine)" and "a daemon that is ready to accept that client": it launches the right variant, waits for actual readiness, keeps the child's environment sane, and stops exactly what it started.

## Core concepts / Decided

### Public surface

```python
class DaemonError(BridgeError): ...


@dataclass(frozen=True)
class DaemonHandle:
    host: str
    port: int
    # True when this context spawned the process and will stop it
    owned: bool
    # the spawned process, when owned
    pid: int | None


def is_daemon_ready(host: str, port: int) -> bool: ...


def launch_command(config: DaemonConfig, *, backend: str = "sim") -> list[str]: ...


@contextmanager
def managed_daemon(
    config: DaemonConfig,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    backend: str = "sim",
) -> Iterator[DaemonHandle]: ...
```

`backend` is `"sim"` (the default) or `"real"` and selects the launch recipe below; anything else is a `ValueError`. `managed_daemon` is a **synchronous** context manager (subprocess and socket work); [api.md](api.md) enters and exits it off the event loop with `asyncio.to_thread`. `DaemonConfig` is [config.md](config.md)'s block — `daemon.py` imports `config.py`, a one-way dependency.

### Own it or borrow it

`managed_daemon` follows `config.spawn`:

- **`auto`** — if a daemon is already ready at `host:port`, yield it **borrowed** (`owned=False`) and never stop it: a viewer sim the user started by hand, or a daemon another process owns. If the port is open but the daemon is not yet ready (still booting), wait for readiness up to `startup_timeout`, then borrow it. If the port is free, **spawn** and own it.
- **`always`** — spawn and own; the port already open is a `DaemonError` (the caller asked for a fresh daemon and something else holds the address).
- **`never`** — not a `managed_daemon` mode: the api simply connects, and this module is not involved.

`host` must be a loopback address — the bridge only ever spawns on the local machine (enforced at config time, [config.md](config.md)).

### Readiness means the backend is up

`is_daemon_ready(host, port)` connects a plain network `reachy_mini` client with `media_backend="no_media"` (cheap; never negotiates media) and reports `client.get_status().backend_status is not None`. This is stronger than an open port or an accepted WebSocket: the daemon answers `/ws/sdk` with `403 "Daemon not ready"` until it has woken, and accepts the socket even when its MuJoCo backend failed to start (no GL context, for instance) — only a non-`None` backend status means a client can drive the robot. Any exception during the probe reads as "not ready".

After spawning, `managed_daemon` polls `is_daemon_ready` once per second until `startup_timeout`. If the child exits first, it raises `DaemonError` with the exit code and the launch command; on timeout it stops the child and raises `DaemonError` (the message includes the viewer hint below for a `sim` daemon with `headless` `false`).

### The launch command

`launch_command(config, backend=...)` builds the argv. Every `sim` daemon runs through the bridge's **sim daemon launcher** ([sim_daemon.md](sim_daemon.md)) — upstream's daemon with the corrections that make face tracking converge in the sim, and the choice of camera source — from the recipes in [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md):

- **headless** (`config.headless`, the default): `<this interpreter> -m reachy_mini_bridge.sim_daemon --headless --[no-]preload-datasets [--scene <scene>] [camera flags]` — real MuJoCo physics, no viewer, runs anywhere (CI included); with the default `sim` camera there are no frames here (upstream renders the eye camera only under the viewer), with a `webcam` camera the host camera's frames flow headless too.
- **viewer** (`headless: false`): `mjpython -m reachy_mini_bridge.sim_daemon --[no-]preload-datasets [--scene <scene>] [camera flags]` — opens the MuJoCo viewer, which supplies the render's GL context and lets a person watch the sim. It needs an unlocked, interactive GUI session; a locked screen or a non-GUI process tree makes it hang or crash, and the `DaemonError` on that path says so.
- **camera flags** come from `config.camera` ([config.md](config.md)): nothing for the default `sim` source; `--camera webcam [--webcam-device <device>] --webcam-hfov <degrees>` for `webcam` (the device only when set).

For `real` — a robot attached to this machine (USB):

- **hardware**: `reachy-mini-daemon [--kinematics-engine Placo] --[no-]preload-datasets` — no `--sim`; the daemon finds the robot's serial port itself, wakes the robot on start and puts it to sleep on stop. `--kinematics-engine Placo` is passed when the `placo` package is importable (`reachy-mini[placo_kinematics]`): the daemon's default engine rejects gravity compensation, and rejecting it drops the client's connection ([api.md](api.md) "Motors"). `headless` and `scene` are sim knobs and play no part.

- **scene file** (`config.scene` ending in `.xml`, either launch mode): `mjpython -m reachy_mini_bridge.testing.sim_scene --scene-path <abs> --[no-]preload-datasets [camera flags]` for the viewer, `<this interpreter> -m reachy_mini_bridge.testing.sim_scene --scene-path <abs> --headless --[no-]preload-datasets [camera flags]` headless — the test scene's launcher (shipped in the testing package), which is the sim daemon launcher with the scene's extension installed: upstream's daemon on a scene *file* the bridge wrote (hidden-by-default props — a portrait plane today — with a director and an HTTP endpoint to show/place/hide them: [sim_scene.md](sim_scene.md)), with the same corrections and camera choice as every other sim. The path is made absolute at launch; the launcher checks the file exists. Any other `scene` value is an upstream scene *name*, passed as `--scene`.

`--preload-datasets` is passed when `config.preload_datasets` is `true` (the default) and `--no-preload-datasets` when it is `false` — always one of the two, because the daemon's own default is not to preload; the preload runs in the background and does not delay readiness. `--scene` (sim) when `config.scene` is set. Media stays **on** (no `--no-media`) so audio — and, under the viewer, the camera — are available; a consumer that wants a motion-only daemon runs its own.

The launcher (`reachy-mini-daemon` for `real`, `mjpython` for the viewer; every `sim` recipe also needs `reachy-mini-daemon` present, as the sign the sim extra is installed) is resolved on `PATH`; a missing launcher is a `DaemonError` — naming the `sim` extra (`reachy-mini-bridge[sim]`) for `sim`, and `reachy-mini` (the base dependency that ships the launcher) for `real`. The `DaemonError`s on the startup path name the backend (`sim daemon` / `real daemon`).

### The child's environment is scrubbed

The spawned process runs with the GStreamer-bundle variables removed from its inherited environment: `GST_PLUGIN_PATH_1_0`, `GST_PLUGIN_SYSTEM_PATH_1_0`, `GST_REGISTRY_1_0`, `GST_PLUGIN_SCANNER_1_0`, `GI_TYPELIB_PATH`, `PYGI_DLL_DIRS`, `XDG_DATA_DIRS`, `XDG_CONFIG_DIRS`. `reachy_mini`'s `gstreamer_bundle.pth` *prepends* to these at every Python startup, so a parent that has imported `reachy_mini` (every bridge process — `robot.py` imports it at module load) would hand the child already-set values that its own `.pth` doubles into paths GStreamer cannot exec, and the daemon segfaults in the in-process plugin scan. Scrubbing lets the child set fresh, correct values — the same thing upstream's own app launcher does.

The child's stdout/stderr are discarded; a failed launch is reported through `DaemonError` (exit code + command), which the user can re-run by hand to see the daemon's own output.

### The child runs in its own session

The spawned daemon is started with `start_new_session=True` (`setsid()` in the child — the bridge's hosts are macOS and Linux) and its stdin detached (`DEVNULL`, like its stdout/stderr), so it belongs to neither the terminal's session nor its foreground process group. A terminal's Ctrl+C delivers `SIGINT` to every process in the foreground group; a daemon sharing that group would shut down *at the same moment* as the bridge process — closing its WebSocket clients with `1012 service restart` — and the api's ordered teardown ([api.md](api.md) "Lifecycle") would then run against a daemon that is already gone: the motion loop's every tick failing (a warning per tick), upstream's wobbler printing tracebacks for the speech offsets still scheduled for a playing sound, the camera pipeline reporting end-of-stream, and the robot left wherever the dying daemon dropped it. In its own session the daemon sees no terminal signal at all: the bridge process alone gets the `KeyboardInterrupt`, exits the api against a live daemon (motion eases to neutral, tracking and wobbling are switched off daemon-side, media and the client close), and only then does teardown terminate the daemon, which uses its grace to put the robot to sleep. The order in which things stop is the bridge's to decide, and only the bridge stops the daemon.

### Teardown stops what the bridge started

On exit, an **owned** daemon is terminated (`SIGTERM`), given 10 seconds to exit, then killed. A real daemon uses that grace to put the robot to sleep (measured at about 8 seconds on a Lite). A **borrowed** daemon is left running. Teardown runs on every exit path, including when the body raises.

**An orphaned daemon is the accepted trade-off of the detached session.** Because the daemon no longer shares the terminal's session, it outlives a bridge process that never reaches teardown — one that is `SIGKILL`ed, crashes without unwinding, or loses its terminal (the `SIGHUP` of a closed window goes to the terminal's session, which the daemon has left). Such a daemon keeps serving on its port, with the robot awake. On the next run, `daemon.spawn: "auto"` borrows it (`owned=False`, so the bridge never stops it) and `"always"` raises `DaemonError` naming the port already in use; `pkill -f reachy-mini-daemon` stops it by hand. A parent-death watchdog is deferred (open question 2).

### One implementation, two users

`ReachyMiniApi.__aenter__` enters `managed_daemon` before building the robot when `daemon.spawn != "never"`, passing the config's `backend` (`sim` or `real`) to select the recipe ([api.md](api.md) "Lifecycle"). The shipped testing harness's private `testing/_daemon.py` is a thin wrapper over the same functions — `is_daemon_ready` for the borrow decision, `managed_daemon(spawn="auto", backend=...)` to spawn a `sim` daemon or, on a loopback address, a `real` one — translating `DaemonError` into `pytest.skip` so the live tier still skips, never fails, when the environment cannot provide a daemon ([testing_support.md](testing_support.md)).

### Testable without a daemon

The process-spawning and readiness-probing steps are injectable seams (module-private callables `managed_daemon` resolves at call time), so the deterministic `tests/` tier drives the own-or-borrow decisions, the readiness loop, the exit-code and timeout errors, and the teardown against a scripted stand-in process — no `reachy-mini-daemon`, no `mujoco`. `launch_command` and the environment scrub are pure functions, tested directly. The real `_spawn` seam has its own tests on a trivial long-lived child (POSIX only): the child's session and process group differ from the spawner's, and a `SIGINT` sent to the spawner's whole process group leaves the child alive. The live tier exercises the real spawn through the harness.

## Relationship to the other specs

- **[config.md](config.md):** `DaemonConfig` (the `daemon` block) is this module's input; the loopback-host rule and the `sim`/`real`-only rule are enforced there.
- **[sim_daemon.md](sim_daemon.md):** every `sim` recipe runs the bridge's sim daemon launcher (or the test scene's, built on it).
- **[api.md](api.md):** the api's lifecycle enters `managed_daemon` first, then builds and enters the robot, then opens the media session.
- **[robot.md](robot.md):** the readiness probe is a plain `build_robot` network client; the connection options a managed daemon needs (`network`, `local` media) are filled into the `robot` block by config.
- **[testing_support.md](testing_support.md):** `testing/_daemon.py` wraps this module.

## Open questions

1. **Daemon output.** Whether to forward the child's stdout/stderr into the bridge's logger (at `DEBUG`) instead of discarding it, for diagnosing failed launches in-process, is deferred until the discard-plus-`DaemonError` path proves insufficient in practice.
2. **Parent-death watchdog.** Stopping an orphaned daemon automatically ("Teardown" above) is deferred: Linux offers `prctl(PR_SET_PDEATHSIG, SIGTERM)` in a `preexec_fn`, macOS has no equivalent, and a cross-platform answer is a wrapper process polling `os.getppid()` — extra moving parts for a benign case (`auto` borrows the orphan; `pkill` stops it). Revisit if orphans prove to be a nuisance in practice.
