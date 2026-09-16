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

The bridge owns bringing up — and tearing down — a local `reachy-mini-daemon`: a MuJoCo one for the `sim` backend, and a hardware one for a robot plugged into this machine over USB (Reachy Mini Lite), so that a `ReachyMiniApi` whose config asks for it ([config.md](config.md) `daemon.spawn`) produces a working simulated robot in one call, and so that the shipped testing harness ([testing_support.md](testing_support.md)) — which brings up either kind — and any consumer's own tooling reuse one implementation of the launch recipes in [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md).

Upstream's `ReachyMini` is a *client*: it needs a separately running daemon (hardware, or the MuJoCo simulation) and connects to it in its constructor (see [../docs/reachy-mini-api.md](../docs/reachy-mini-api.md)). This module is the piece between "a config that says `sim`" and "a daemon that is ready to accept that client": it launches the right variant, waits for actual readiness, keeps the child's environment sane, and stops exactly what it started.

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

After spawning, `managed_daemon` polls `is_daemon_ready` once per second until `startup_timeout`. If the child exits first, it raises `DaemonError` with the exit code and the launch command; on timeout it stops the child and raises `DaemonError` (the message includes the viewer hint below when `headless` is `false`).

### The launch command

`launch_command(config, backend=...)` builds the argv. For `sim`, from the recipes in [../docs/running-the-sim-daemon.md](../docs/running-the-sim-daemon.md):

- **headless** (`config.headless`, the default): `reachy-mini-daemon --sim --headless [--no-preload-datasets] [--scene <scene>]` — real MuJoCo physics, no viewer, runs anywhere (CI included); on macOS the sim camera returns `None` here (no GL context).
- **viewer** (`headless: false`): `mjpython -m reachy_mini.daemon.app.main --sim [--no-preload-datasets] [--scene <scene>]` — opens the MuJoCo viewer, which supplies the camera's GL context and lets a person watch the sim. It needs an unlocked, interactive GUI session; a locked screen or a non-GUI process tree makes it hang or crash, and the `DaemonError` on that path says so.

For `real` — a robot attached to this machine (USB):

- **hardware**: `reachy-mini-daemon [--kinematics-engine Placo] [--no-preload-datasets]` — no `--sim`; the daemon finds the robot's serial port itself, wakes the robot on start and puts it to sleep on stop. `--kinematics-engine Placo` is passed when the `placo` package is importable (`reachy-mini[placo_kinematics]`): the daemon's default engine rejects gravity compensation, and rejecting it drops the client's connection ([api.md](api.md) "Motors"). `headless` and `scene` are sim knobs and play no part.

`--no-preload-datasets` is passed when `config.preload_datasets` is `false`; `--scene` (sim) when `config.scene` is set. Media stays **on** (no `--no-media`) so audio — and, under the viewer, the camera — are available; a consumer that wants a motion-only daemon runs its own.

The launcher (`reachy-mini-daemon`, or `mjpython` for the viewer) is resolved on `PATH`; a missing launcher is a `DaemonError` — naming the `sim` extra (`reachy-mini-bridge[sim]`) for `sim`, and `reachy-mini` (the base dependency that ships the launcher) for `real`. The `DaemonError`s on the startup path name the backend (`sim daemon` / `real daemon`).

### The child's environment is scrubbed

The spawned process runs with the GStreamer-bundle variables removed from its inherited environment: `GST_PLUGIN_PATH_1_0`, `GST_PLUGIN_SYSTEM_PATH_1_0`, `GST_REGISTRY_1_0`, `GST_PLUGIN_SCANNER_1_0`, `GI_TYPELIB_PATH`, `PYGI_DLL_DIRS`, `XDG_DATA_DIRS`, `XDG_CONFIG_DIRS`. `reachy_mini`'s `gstreamer_bundle.pth` *prepends* to these at every Python startup, so a parent that has imported `reachy_mini` (every bridge process — `robot.py` imports it at module load) would hand the child already-set values that its own `.pth` doubles into paths GStreamer cannot exec, and the daemon segfaults in the in-process plugin scan. Scrubbing lets the child set fresh, correct values — the same thing upstream's own app launcher does.

The child's stdout/stderr are discarded; a failed launch is reported through `DaemonError` (exit code + command), which the user can re-run by hand to see the daemon's own output.

### Teardown stops what the bridge started

On exit, an **owned** daemon is terminated (`SIGTERM`), given 10 seconds to exit, then killed. A real daemon uses that grace to put the robot to sleep (measured at about 8 seconds on a Lite). A **borrowed** daemon is left running. Teardown runs on every exit path, including when the body raises.

### One implementation, two users

`ReachyMiniApi.__aenter__` enters `managed_daemon` before building the robot when `daemon.spawn != "never"` ([api.md](api.md) "Lifecycle"). The shipped testing harness's private `testing/_daemon.py` is a thin wrapper over the same functions — `is_daemon_ready` for the borrow decision, `managed_daemon(spawn="auto", backend=...)` to spawn a `sim` daemon or, on a loopback address, a `real` one — translating `DaemonError` into `pytest.skip` so the live tier still skips, never fails, when the environment cannot provide a daemon ([testing_support.md](testing_support.md)).

### Testable without a daemon

The process-spawning and readiness-probing steps are injectable seams (module-private callables `managed_daemon` resolves at call time), so the deterministic `tests/` tier drives the own-or-borrow decisions, the readiness loop, the exit-code and timeout errors, and the teardown against a scripted stand-in process — no `reachy-mini-daemon`, no `mujoco`. `launch_command` and the environment scrub are pure functions, tested directly. The live tier exercises the real spawn through the harness.

## Relationship to the other specs

- **[config.md](config.md):** `DaemonConfig` (the `daemon` block) is this module's input; the loopback-host rule and the `sim`-only rule are enforced there.
- **[api.md](api.md):** the api's lifecycle enters `managed_daemon` first, then builds and enters the robot, then opens the media session.
- **[robot.md](robot.md):** the readiness probe is a plain `build_robot` network client; the connection options a managed daemon needs (`network`, `local` media) are filled into the `robot` block by config.
- **[testing_support.md](testing_support.md):** `testing/_daemon.py` wraps this module.

## Open questions

1. **Daemon output.** Whether to forward the child's stdout/stderr into the bridge's logger (at `DEBUG`) instead of discarding it, for diagnosing failed launches in-process, is deferred until the discard-plus-`DaemonError` path proves insufficient in practice.
2. **A `real` daemon from `ReachyMiniApi`.** The launch recipe and lifecycle for a USB robot exist (above) and the testing harness uses them; a `ReachyMiniApi` config still spawns only for `sim` ([config.md](config.md)). Opening `daemon.spawn` to `backend: "real"` is deferred until an application needs the api, rather than the test harness, to start the robot's daemon.
