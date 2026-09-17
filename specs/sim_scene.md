---
code:
  - src/reachy_mini_bridge/testing/sim_scene.py
  - src/reachy_mini_bridge/errors.py
  - src/reachy_mini_bridge/daemon.py
  - src/reachy_mini_bridge/testing/_daemon.py
  - src/reachy_mini_bridge/testing/fixtures.py
tests:
  - tests/test_sim_scene.py
  - tests/test_daemon.py
  - tests/test_testing_support.py
  - tests-e2e/test_faces.py
---

# Sim scene — faces in the MuJoCo sim (`sim_scene.py`)

**Status:** Implemented

## Purpose

The `sim` backend is the one place the bridge's *perception-driven* behaviours — face tracking, the attention hand-back ([api.md](api.md) "Attention"), breathing resuming when nobody is there ([motion.md](motion.md)) — can be exercised end to end without a person in front of a robot: the MuJoCo daemon renders its eye camera offscreen and feeds that image to the same daemon-side face detector and tracking aim a real robot uses. What upstream lacks is anything to *look at* and any way to move it: its scenes are loaded by name from inside its package, ship no faces, and expose no runtime control over the objects in them. This concept adds a **portrait plane** in front of the robot — a photo of a face, textured onto a thin body the bridge can **show, move and hide** while the daemon runs — so an e2e test on the viewer sim can watch the head react to a face appearing, hand itself back to breathing once it leaves, and react again when it returns, and so a person can watch the same in the viewer window. What it does *not* claim: that the head precisely centres on the target — see "Tracking convergence" below.

## Core concepts / Decided

### The real pipeline, not a fake detection

Nothing here fakes a face target. The portrait is a real object in the physics scene; the daemon's own render thread draws the eye camera's view at 25 Hz, streams it over UDP into the daemon's GStreamer media server, which tees it to the camera IPC socket that both the client's `get_frame()` and the daemon's `FaceTracker` (downscale to 320 px wide, YuNet on ONNX Runtime) read. A detection latches the daemon's tracking aim and the head IK follows, exactly as with a person ([../docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking"). The bridge only puts the face there and moves it.

This holds only under the **viewer** daemon: upstream starts the render thread solely when not headless (on every platform), so a headless sim has no camera at all. The face scene therefore targets `daemon.headless = false` ([daemon.md](daemon.md): an unlocked GUI session, `mjpython` on macOS). Headless, the scene still loads and its bodies still move — there is just nothing looking at them.

### Three pieces, one process boundary

The daemon is a separate process (its `MjData` lives there), so the concept splits along it:

| Piece | Where it runs | Role |
|---|---|---|
| **Scene file** — `write_test_scene(out_dir, faces=(FacePlane(),))` → `Path` | bridge (or a consumer's conftest) | Writes `<out_dir>/scene.xml`: upstream's `empty` scene (skybox, checker floor, light) plus one **`mocap` body per prop** (`FacePlane` today) carrying a thin box textured with the portrait, upright and facing the head, at the **visibility its dataclass says** — the default face starts hidden. The robot model and its meshes are included by **absolute path**, textures too, so the file loads from anywhere. |
| **Launcher** — `python -m reachy_mini_bridge.testing.sim_scene --scene-path S [--headless] [--no-preload-datasets] [upstream flags…]` | daemon process (under `mjpython` for the viewer) | Runs upstream's daemon on that file with the **director** installed as MuJoCo's control callback and the **sim-scene router** mounted on the daemon's own FastAPI app. |
| **Client** — `SimSceneClient(host, port)` | bridge / tests | Drives the router over the daemon's HTTP port: `bodies()`, `place(name, pos, quat=None, duration=0)`, `show(name)`, `hide(name)`, `wait_still(name, timeout)`. Every failure is a `SimSceneError` (a `BridgeError`), including "this daemon was not launched through the sim-scene launcher". |

`FacePlane(name="face", image=None, pos=(0.45, 0.0, 0.20), size=(0.20, 0.25))` — `image` is a PNG path, the bundled public-domain portrait (`assets/face.png`, see `assets/ATTRIBUTION.md`) when `None`; `pos` in world metres; `size` = (width, height) in metres. Names must be unique and non-empty; a missing image is a `FileNotFoundError` at write time, not a daemon crash at load time.

### Loading a file upstream only loads by name

Upstream's `MujocoBackend` builds the scene path as `<its mjcf dir>/scenes/<name>.xml` from `--scene <name>`. `upstream_scene_name(path)` returns the path **relative to that `scenes/` directory, minus `.xml`** — a name upstream resolves to any file on the machine, so the scene itself needs no change to upstream's loading code. It requires an existing `.xml` file (`FileNotFoundError` / `ValueError` otherwise). The scene file's own `<include>` and `meshdir` are absolute precisely because the file lives outside upstream's tree.

### The director: bodies driven from inside the physics loop

`SceneDirector` is installed with `mujoco.set_mjcb_control(director.step)` — the global control callback MuJoCo invokes on **every `mj_step`, on the daemon's physics thread**, with the model and data. It is installed **after the model is built, never before**: MuJoCo's compiler runs the callback on the half-built model while loading a scene, and the Python binding fails wrapping that model before any callback code runs, so a callback present during `MjModel.from_xml_path` makes every load fail (`engine error: Python exception raised`). The launcher therefore substitutes the backend class the daemon constructs (`reachy_mini.daemon.daemon.MujocoBackend`) with `directed_backend(...)`'s subclass, whose only addition is that install at the end of `__init__` — per instance, so a daemon restart re-installs it. On its first call the director **attaches**: discovers every named `mocap` body and its geoms and records their initial pose. On each call it writes each body's commanded pose into `data.mocap_pos` / `data.mocap_quat` and its visibility into the alpha of its geoms (`model.geom_rgba[…, 3]`; the renderer skips alpha 0, so a hidden face vanishes from the eye camera and from the viewer window alike — the body keeps its pose). Writes are skipped when nothing changed, so the callback costs nothing at 500 Hz while idle.

A **timed move** (`duration > 0`) interpolates linearly on the **wall clock** (a monotonic clock, injectable for tests) from where the body *currently is* — retargeting mid-move starts from the interpolated pose, no jump — to the commanded pose; `duration` 0 lands on the next step. Orientation interpolates by normalised lerp (small rotations; a face stays facing the robot). `moving` reports whether a move is still in flight. Commands come from the HTTP handler thread under a lock the physics thread holds only to copy a few floats; a director bug is logged and swallowed so the physics loop never dies on it.

Validation: an unknown body is a `KeyError`; a position that is not three numbers, a quaternion that is not four (it is normalised on the way in; all-zero is refused), or a negative duration is a `ValueError`.

### Head tracking is stepped by the bridge in the sim

Upstream's daemon-side tracking has two halves: a detector thread that queues observations, and `step_head_tracking()` — which drains them, latches the aim and marks IK — that the backend loop must call. **Only the real-robot loop calls it** (SDK 1.10); the MuJoCo loop never does, so in a stock sim the detector runs while `get_tracked_face()` stays undetected and the head never follows a face, however well it is seen. The backend subclass closes that gap: it overrides `update_head_kinematics_model` — the method the MuJoCo loop calls once per 50 Hz control tick, at the same point where the robot loop calls it — to step tracking right after it, in the robot loop's order (kinematics update, tracking step, IK). Nothing else about tracking changes: the same detector, the same aim, the same weights and lost-face recentre, and the same hand-back the bridge's attention loop performs on top ([api.md](api.md) "Attention"). A sim daemon launched *without* this launcher therefore cannot exercise face tracking at all — one more reason the `faces` capability is probed, not inferred from the viewer.

### Tracking convergence: a real reaction, not a precise fix

Wiring `step_head_tracking()` into the sim (above) makes the head *react* to a face reliably — but the resulting orientation does not reliably *converge on* the target, and that turns out to be a genuine control-loop characteristic of upstream's own tracking algorithm applied to the sim's timing, not a bug introduced by this launcher. Investigated live (viewer sim, `REACHY_MINI_E2E_SIM_SCENE=test`), varying one thing at a time:

- **The geometry is correct.** With tracking off, the head stays at true neutral and a face at camera height detects within a degree or two of image-centre — confirmed by capturing a raw frame and running the detector on it independently of the daemon's own tracker.
- **A cold start (`start_head_tracking` from stopped) reliably overshoots.** The head swings 30–45° within about a second of the first detection and then holds there, well past what the target's actual angle calls for — confirmed across many trials, with and without a corrected camera-intrinsics matrix (`MujocoCameraSpecs.K` assumes a ~53° field of view where the MJCF's `eye_camera` is actually 80° — a genuine upstream mismatch, independently confirmed, but *not* the cause here: correcting it made the overshoot larger and added oscillation, since it raises the effective gain on the same unstable loop, so the correction is not applied).
- **The attention hand-back's own restore (`watching` → `engaged`, api.py `_run_attention`) under-reacts.** Once re-engaged this way the head settles at a small, sometimes near-unchanged angle regardless of the target's actual position — reproduced with the face both near dead-centre and ~20° off-axis, both settling within a few degrees of wherever tracking last left the head.
- **`play_emotion`'s own pause/restore does not share that under-reaction** — the head visibly reacts to the still-visible face once the move ends, comparably to a cold start.

The plausible mechanism is a control-loop lag: `step_head_tracking()`'s per-tick easing (`_tracking_alpha = 0.15`, `abstract.py`) and the detector's own asynchronous, ~25 fps thread were tuned against the real robot's loop; the sim's 50 Hz control tick can drive the same algorithm outside the regime that tuning assumed. This is a fix for upstream, not something to chase further here — a draft report belongs alongside [../docs/upstream-head-tracking-after-face-loss.md](../docs/upstream-head-tracking-after-face-loss.md) once this is confirmed on the real robot too (open question 4).

**What the tests assert accordingly** (`tests-e2e/test_faces.py` — see its module docstring): a face appearing pulls the head a large, easily-thresholded distance from neutral (reaction, not precision); alone, the head settles back near neutral and breathes; the face returning re-engages `attention` (state, not a magnitude assertion — the `watching` restore's under-reaction above is exactly why); an emotion under tracking visibly moves the head through its own choreography and the head reacts again once tracking resumes.

### The router: the daemon's own port

`run_daemon` **wraps** upstream's `create_app` (it calls the original, then `include_router`s the sim-scene router at `/api/sim-scene`), so upstream's app is built exactly as usual and served on the daemon's existing port — no second port to configure or firewall. The launcher rewrites `sys.argv` to `--sim --scene <name>` (plus `--headless` when asked, always an explicit `--[no-]preload-datasets`, and any unrecognised flags forwarded verbatim) and calls upstream's `main()`.

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

[testing_support.md](testing_support.md) gains the knob its open question deferred, in the shape this concept needs:

- **`REACHY_MINI_E2E_SIM_SCENE`** — unset (upstream's default scene), an upstream scene name (`minimal`), a path to an `.xml` scene file, or **`test`**: the harness writes the bridge's test scene into a temporary directory that lives as long as the daemon it spawns and passes its path as `DaemonConfig.scene`. A daemon already ready at the address is borrowed as before, whatever it runs.
- **`faces` capability** — probed like the others: the sim-scene endpoint at the daemon's address answers and lists a body named `face`. Absent on a headless or plain viewer sim and on a real robot, so `requires_caps(live_api, "camera", "faces")` skips a face test everywhere it cannot run. (`faces` says the face exists; `camera` says something is looking at it — a face test needs both.)
- **`sim_scene` fixture** (module-scoped, next to `live_api` in the plugin module) — a `SimSceneClient` on the fixture-managed daemon.

The bridge's own tier ([testing.md](testing.md)) dogfoods it in `tests-e2e/test_faces.py`: the head reacts to a face appearing; hidden, the attention loop hands the head back (`watching`), it settles near neutral and the z-axis breathing resumes, then `attention` re-engages when the face returns; an emotion plays over tracking (visibly moving the head through its own choreography) and the head reacts to the still-visible face again once it ends. See "Tracking convergence" above for what these tests do and don't claim about precision.

## Open questions

1. **Headless camera on Linux.** Upstream never starts the render thread headless, but with an EGL/OSMesa GL context the launcher could start it itself and the face tests could run on CI without a window. Deferred until a Linux CI target exists; the pieces (scene, director, router) would not change.
2. **Scene files as a first-class upstream feature.** The `upstream_scene_name` relative-path trick rests on upstream formatting `scenes/{name}.xml`; a `--scene-path` upstream flag would make it unnecessary. Worth proposing upstream; pinned by `tests/test_sim_scene.py` meanwhile.
3. **More than faces.** The director drives *any* named `mocap` body, so a scene with several faces (`FacePlane` already takes a list) or other props needs only a scene file; a body-per-person "crowd" test and a mocap object for a future `look_at` verb are natural extensions.
4. **Confirm the tracking-convergence finding on real hardware.** "Tracking convergence" above is confirmed in the sim; whether the same cold-start overshoot and `watching`-restore under-reaction are visible on a real robot (a different, likely gentler detector/network cadence) is unconfirmed — worth a live check before filing the draft upstream report the section proposes.
