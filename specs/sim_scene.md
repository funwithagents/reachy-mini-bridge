---
code:
  - src/reachy_mini_bridge/testing/sim_scene.py
  - src/reachy_mini_bridge/sim_daemon.py
  - src/reachy_mini_bridge/errors.py
  - src/reachy_mini_bridge/daemon.py
  - src/reachy_mini_bridge/testing/_daemon.py
  - src/reachy_mini_bridge/testing/fixtures.py
tests:
  - tests/test_sim_scene.py
  - tests/test_daemon.py
  - tests/test_testing_support.py
  - tests-e2e/test_api.py
---

# Sim scene — faces in the MuJoCo sim (`sim_scene.py`)

**Status:** Implemented

## Purpose

The `sim` backend is the one place the bridge's *perception-driven* behaviours — face tracking, the attention hand-back ([api.md](api.md) "Attention"), breathing resuming when nobody is there ([motion.md](motion.md)) — can be exercised end to end without a person in front of a robot: the MuJoCo daemon renders its eye camera offscreen and feeds that image to the same daemon-side face detector and tracking aim a real robot uses. What upstream lacks is anything to *look at* and any way to move it: its scenes are loaded by name from inside its package, ship no faces, and expose no runtime control over the objects in them. This concept adds a **portrait plane** in front of the robot — a photo of a face, textured onto a thin body the bridge can **show, move and hide** while the daemon runs — so an e2e test on the viewer sim can watch the head turn to face it, hand itself back to breathing once it leaves, and turn to it again when it returns, and so a person can watch the same in the viewer window. The daemon runs through the bridge's sim daemon launcher ([sim_daemon.md](sim_daemon.md)), whose corrections make the head converge on the face as it does on a robot — so the tests assert where the head ends up, not only that it moved.

## Core concepts / Decided

### The real pipeline, not a fake detection

Nothing here fakes a face target. The portrait is a real object in the physics scene; the daemon's own render thread draws the eye camera's view at 25 Hz, streams it over UDP into the daemon's GStreamer media server, which tees it to the camera IPC socket that both the client's `get_frame()` and the daemon's `FaceTracker` (downscale to 320 px wide, YuNet on ONNX Runtime) read. A detection latches the daemon's tracking aim and the head IK follows, exactly as with a person ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking"). The bridge only puts the face there and moves it.

This holds only under the **viewer** daemon: upstream starts the render thread solely when not headless (on every platform), so a headless sim has no camera at all. The face scene therefore targets `daemon.headless = false` ([daemon.md](daemon.md): an unlocked GUI session, `mjpython` on macOS). Headless, the scene still loads and its bodies still move — there is just nothing looking at them.

### Three pieces, one process boundary

The daemon is a separate process (its `MjData` lives there), so the concept splits along it:

| Piece | Where it runs | Role |
|---|---|---|
| **Scene file** — `write_test_scene(out_dir, faces=(FacePlane(),))` → `Path` | bridge (or a consumer's conftest) | Writes `<out_dir>/scene.xml`: upstream's `empty` scene (skybox, checker floor, light) plus one **`mocap` body per prop** (`FacePlane` today) carrying a thin box textured with the portrait, upright and facing the head, at the **visibility its dataclass says** — the default face starts hidden. The robot model and its meshes are included by **absolute path**, textures too, so the file loads from anywhere. |
| **Launcher** — `python -m reachy_mini_bridge.testing.sim_scene --scene-path S [--headless] [--no-preload-datasets] [camera flags] [upstream flags…]` | daemon process (under `mjpython` for the viewer) | Runs the bridge's sim daemon launcher ([sim_daemon.md](sim_daemon.md)) on that file with the scene's `SimDaemonExtension`: the **director** installed as MuJoCo's control callback, the **sim-scene router** mounted on the daemon's own FastAPI app. |
| **Client** — `SimSceneClient(host, port)` | bridge / tests | Drives the router over the daemon's HTTP port: `bodies()`, `place(name, pos, quat=None, duration=0)`, `show(name)`, `hide(name)`, `wait_still(name, timeout)`. Every failure is a `SimSceneError` (a `BridgeError`), including "this daemon was not launched through the sim-scene launcher". |

`FacePlane(name="face", image=None, pos=(0.45, 0.0, 0.20), size=(0.20, 0.25))` — `image` is a PNG path, the bundled public-domain portrait (`assets/face.png`, see `assets/ATTRIBUTION.md`) when `None`; `pos` in world metres; `size` = (width, height) in metres. Names must be unique and non-empty; a missing image is a `FileNotFoundError` at write time, not a daemon crash at load time.

### Loading a file upstream only loads by name

Upstream's `MujocoBackend` builds the scene path as `<its mjcf dir>/scenes/<name>.xml` from `--scene <name>`. `upstream_scene_name(path)` returns the path **relative to that `scenes/` directory, minus `.xml`** — a name upstream resolves to any file on the machine, so the scene itself needs no change to upstream's loading code. It requires an existing `.xml` file (`FileNotFoundError` / `ValueError` otherwise). The scene file's own `<include>` and `meshdir` are absolute precisely because the file lives outside upstream's tree.

### The director: bodies driven from inside the physics loop

`SceneDirector` is installed with `mujoco.set_mjcb_control(director.step)` — the global control callback MuJoCo invokes on **every `mj_step`, on the daemon's physics thread**, with the model and data. It is installed **after the model is built, never before**: MuJoCo's compiler runs the callback on the half-built model while loading a scene, and the Python binding fails wrapping that model before any callback code runs, so a callback present during `MjModel.from_xml_path` makes every load fail (`engine error: Python exception raised`). The scene extension's `on_backend` hook does the install — the sim daemon launcher calls it at the end of the backend's `__init__`, per instance, so a daemon restart re-installs it. On its first call the director **attaches**: discovers every named `mocap` body and its geoms and records their initial pose. On each call it writes each body's commanded pose into `data.mocap_pos` / `data.mocap_quat` and its visibility into the alpha of its geoms (`model.geom_rgba[…, 3]`; the renderer skips alpha 0, so a hidden face vanishes from the eye camera and from the viewer window alike — the body keeps its pose). Writes are skipped when nothing changed, so the callback costs nothing at 500 Hz while idle.

A **timed move** (`duration > 0`) interpolates linearly on the **wall clock** (a monotonic clock, injectable for tests) from where the body *currently is* — retargeting mid-move starts from the interpolated pose, no jump — to the commanded pose; `duration` 0 lands on the next step. Orientation interpolates by normalised lerp (small rotations; a face stays facing the robot). `moving` reports whether a move is still in flight. Commands come from the HTTP handler thread under a lock the physics thread holds only to copy a few floats; a director bug is logged and swallowed so the physics loop never dies on it.

Validation: an unknown body is a `KeyError`; a position that is not three numbers, a quaternion that is not four (it is normalised on the way in; all-zero is refused), or a negative duration is a `ValueError`.

### Head tracking converges on the face

The scene brings the face; the sim daemon launcher ([sim_daemon.md](sim_daemon.md)) makes the daemon track it the way a robot does — it steps daemon-side tracking on every control tick (upstream's MuJoCo loop never does) and gives the tracker the eye camera's true intrinsics (upstream's are mis-scaled for the sim camera, which puts the head ~45° off the face). Nothing about tracking is scene-specific: the same detector, aim, weights, lost-face recentre, and the bridge's attention hand-back on top ([api.md](api.md) "Attention"). On the test scene, with the default plane 0.45 m away:

| Face | Head, settled |
|---|---|
| ahead, `(0.45, 0, 0.20)` | yaw ~0°, pitch ~6° (the tracker aims at the nose, below the plane's centre) |
| `(0.45, ±0.15, 0.20)` | yaw ±17–19° — `atan2(0.15, 0.45)` = 18.4°: the eye camera sits on the head's forward axis, so it looks at the face when the head's heading from its pivot (the origin) does |

and the tracked face sits at the image centre (`get_tracked_face()` normalised |x|, |y| well under 0.1). On the way the head swings once past the face and creeps back onto it — 3–8° following a face that moves, 7–9.5° onto one that appears 18° away — because upstream's aim adds a detection a few frames old to the present head pose; it never swings back past the face. A face that returns after the attention hand-back is converged on the same way. The camera looking is the head-mounted eye camera, so the scene's props need the default `sim` camera source: with a `webcam` source they still move in the viewer, but nothing detects them.

### The router: the daemon's own port

The scene extension's `on_app` hook `include_router`s the sim-scene router at `/api/sim-scene` on the app upstream's `create_app` built, so the app is upstream's own, served on the daemon's existing port — no second port to configure or firewall. `run_daemon` parses `--scene-path`, resolves it to the upstream scene name (below), and hands `--scene <name>` with every other flag — `--headless`, `--[no-]preload-datasets`, the camera flags, anything unrecognised — to `run_sim_daemon` with the extension.

| Route | Body | Result |
|---|---|---|
| `GET /api/sim-scene/bodies` | — | `{"attached": bool, "bodies": {name: state}}` — `attached` is false until the first physics step |
| `POST /api/sim-scene/bodies/{name}` | JSON object with any of `pos` `[x,y,z]`, `quat` `[w,x,y,z]`, `duration` (s), `visible` (bool) | the body's new state; `404` unknown body, `400` unknown field or malformed value |

A state is `{"name", "pos", "quat", "visible", "moving"}` (`BodyState` on both sides; `pos`/`quat` are the *commanded* pose, the destination of a move in flight).

### Geometry

Numbers that make the default plane reliably detected, measured with upstream's detector on rendered frames (not estimated):

| | Value |
|---|---|
| eye camera, neutral head | ~0.04 m forward, ~0.20 m up, looking along world +x; fovy 80°, 1280×720 |
| plane orientation `FACE_QUAT` | thin axis toward the robot (−x), height axis up, width axis right-to-left as the camera sees it — the portrait reads upright, not mirrored |
| default plane | 0.20 × 0.25 m at (0.45, 0, 0.20): ~250 px tall in the frame, ~60 px after the tracker's downscale to 320 px wide |
| detector floor | YuNet fires reliably from ~40 px of portrait height in the downscaled frame; below ~30 px it does not. Detected at every lateral position tested, from 0.30 m to 0.70 m away |
| lateral travel | ±0.15 m at 0.45 m is ~±20° of yaw — well inside the head's range, clearly measurable on `get_current_head_pose()` |
| material | `emission="1"`, no specular — the portrait's pixels reach the camera unshaded whatever the scene lighting |
| floor | `reflectance="0"` (upstream's own `empty`/`minimal` scenes use `0.2`) — a reflective floor mirrors the face plane, upside down; YuNet detects the reflection as readily as the real face, and the daemon-side tracker can lock onto it instead — confirmed by capturing a live frame with the reflection visible and reproducing it with `reflectance="0.2"` restored. Zeroed in the generated scene only; nothing upstream changes |

The plane's geom has `contype`/`conaffinity` 0: it collides with nothing and upstream's start-up collision toggling ignores it.

### Configuration and the daemon lifecycle

`DaemonConfig.scene` ([config.md](config.md)) keeps its type (a non-empty string or `null`) and gains a second meaning: a value **ending in `.xml` is a scene file**, anything else an upstream scene name. `launch_command` ([daemon.md](daemon.md)) routes a scene file to this module's launcher — `mjpython -m reachy_mini_bridge.testing.sim_scene --scene-path <abs> …` for the viewer, `<this interpreter> -m … --headless …` headless — with the same `sim`-extra check and the same `DaemonError`s as the plain recipes, and the path made absolute at launch. A `real` daemon ignores `scene` as before. Nothing else in the lifecycle changes: readiness, borrowing, teardown and the environment scrub apply unchanged, and a `ReachyMiniApi` whose config names a scene file brings the bridge's test scene up like any other sim.

### The testing harness

[testing_support.md](testing_support.md) runs every sim it spawns on this scene:

- **The test scene is the harness's sim scene** — no knob: the harness writes it into a temporary directory that lives as long as the daemon it spawns and passes its path as `DaemonConfig.scene`. Its props start hidden, so every other test runs on upstream's empty scene. A daemon already ready at the address is borrowed as before, whatever it runs.
- **`faces` capability** — probed like the others: the sim-scene endpoint at the daemon's address answers and lists a body named `face`. Present on every harness-spawned sim; absent on a borrowed daemon started without the scene and on a real robot, so `requires_caps(live_api, "camera", "faces")` — with `camera` absent headless — runs a tracking test on the viewer sim only. (`faces` says the face exists; `camera` says something is looking at it — a face test needs both.)
- **`sim_scene` fixture** (module-scoped, next to `live_api` in the plugin module) — a `SimSceneClient` on the fixture-managed daemon.

The bridge's own tier ([testing.md](testing.md)) dogfoods it in the attention / gaze tests of `tests-e2e/test_api.py`, asserting how the head moves and where it settles ("Head tracking converges on the face" above): the head turns toward a face ahead and to either side, swings past it at most once and by a bounded amount (never back past it: no oscillation), and settles at the yaw the face's position implies — `atan2(y, x)` from the head's pivot, within 3° — with the tracked face at the image centre and its pitch unchanged; hidden, the attention loop hands the head back (`watching`), it settles near neutral and the z-axis breathing resumes; the face returning elsewhere re-engages `attention` and the head converges on the new position; an emotion plays over tracking (visibly moving the head through its own choreography) and the head converges on the still-visible face again once it ends.

## Open questions

1. **Headless camera on Linux.** Upstream never starts the render thread headless, but with an EGL/OSMesa GL context the launcher could start it itself and the face tests could run on CI without a window. Deferred until a Linux CI target exists; the pieces (scene, director, router) would not change.
2. **Scene files as a first-class upstream feature.** The `upstream_scene_name` relative-path trick rests on upstream formatting `scenes/{name}.xml`; a `--scene-path` upstream flag would make it unnecessary. Worth proposing upstream; pinned by `tests/test_sim_scene.py` meanwhile.
3. **More than faces.** The director drives *any* named `mocap` body, so a scene with several faces (`FacePlane` already takes a list) or other props needs only a scene file; a body-per-person "crowd" test and a mocap object for a future `look_at` verb are natural extensions.
