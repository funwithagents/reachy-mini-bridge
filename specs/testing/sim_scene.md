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
  - tests-e2e/test_bridge.py
---

# Sim scene — faces in the MuJoCo sim (`sim_scene.py`)

**Status:** Implemented

## Purpose

The `sim` backend is the one place the bridge's *perception-driven* behaviours — face detection and the `faces` report ([user_perception.md](../vision/user_perception.md)), head tracking and the hand-back to the idle move when nobody is there ([head_tracking.md](../motion/head_tracking.md), [motion.md](../motion/motion.md) "The gaze layer") — can be exercised end to end without a person in front of a robot: the MuJoCo daemon renders its eye camera offscreen and streams that image to the client, where the bridge's detector — the same model upstream's daemon runs on a robot — sees it. What upstream lacks is anything to *look at* and any way to move it: its scenes are loaded by name from inside its package, ship no faces, and expose no runtime control over the objects in them. This concept adds **portrait planes** in front of the robot — photos of faces, each textured onto a thin body the bridge can **spawn, move and despawn** while the daemon runs, as many at once as a test needs — so an e2e test on the viewer sim can watch the head turn to face one, stay with it while another appears, wait for it when it vanishes and turn to the biggest other one when it does not come back, hand itself back to breathing once everyone leaves, and so a person can watch the same in the viewer window. The daemon runs through the bridge's sim daemon launcher ([sim_daemon.md](../daemon/sim_daemon.md)), the bridge's detector finds the portrait in the rendered camera stream ([user_perception.md](../vision/user_perception.md)), and the bridge's own tracker aims the head at it — so the tests assert where the head ends up, not only that it moved.

## Core concepts / Decided

### The real pipeline, not a fake detection

Nothing here fakes a face. The portrait is a real object in the physics scene; the daemon's own render thread draws the eye camera's view at 25 Hz, streams it over UDP into the daemon's GStreamer media server, which tees it to the camera IPC socket the client's `get_frame()` reads — the bridge's camera feed ([camera.md](../vision/camera.md)), sampled by its detection loop, whose shipped detector (downscale to 320 px wide, YuNet on ONNX Runtime — the model upstream's own daemon-side tracker runs) finds the portrait and whose tracker turns it into the head's aim, exactly as with a person ([user_perception.md](../vision/user_perception.md) "Detectors"). The bridge only puts the face there and moves it.

This holds only under the **viewer** daemon: upstream starts the render thread solely when not headless (on every platform), so a headless sim has no camera at all. The face scene therefore targets `daemon.headless = false` ([daemon.md](../daemon/daemon.md): an unlocked GUI session, `mjpython` on macOS). Headless, the scene still loads and its bodies still move — there is just nothing looking at them.

### Three pieces, one process boundary

The daemon is a separate process (its `MjData` lives there), so the concept splits along it:

| Piece | Where it runs | Role |
|---|---|---|
| **Scene file** — `write_test_scene(out_dir, faces=face_pool())` → `Path` | bridge (or a consumer's conftest) | Writes `<out_dir>/scene.xml`: upstream's `empty` scene (skybox, checker floor, light) plus one **`mocap` body per prop** (`FacePlane` today) carrying a thin box textured with its portrait, upright and facing the head, at the **visibility its dataclass says** — by default a pool of hidden portraits ("A pool of portraits" below). The robot model and its meshes are included by **absolute path**, textures too, so the file loads from anywhere. |
| **Launcher** — `python -m reachy_mini_bridge.testing.sim_scene --scene-path S [--headless] [--no-preload-datasets] [camera flags] [upstream flags…]` | daemon process (under `mjpython` for the viewer) | Runs the bridge's sim daemon launcher ([sim_daemon.md](../daemon/sim_daemon.md)) on that file with the scene's `SimDaemonExtension`: the **director** installed as MuJoCo's control callback, the **inject router** mounted on the daemon's own FastAPI app. |
| **Client** — `SimSceneClient(host, port)` | bridge / tests | Drives the router over the daemon's HTTP port: `spawn(pos, quat=None, *, kind="face", image=None, duration=0)` → the name of the prop it showed, `despawn(name)`, `clear()`, and per body `bodies()`, `place(name, pos, quat=None, duration=0)`, `show(name)`, `hide(name)`, `wait_still(name, timeout)`. Every failure is a `SimSceneError` (a `BridgeError`), including "this daemon was not launched through the sim-scene launcher". |

`FacePlane(name="face_1", image=None, pos=(0.45, 0.0, 0.20), size=(0.20, 0.25), visible=False)` — `image` is a PNG path, the bundled public-domain portrait (`assets/face.png`, see `assets/ATTRIBUTION.md`) when `None`; `pos` in world metres; `size` = (width, height) in metres. Names must be unique and non-empty, and neither `spawn` nor `clear` (the inject router's own paths under `bodies/`, below); a missing image is a `FileNotFoundError` at write time, not a daemon crash at load time.

### A pool of portraits

MuJoCo compiles a scene when the daemon loads it: bodies cannot be added to a running model. "As many portraits as a test needs" is therefore a **pool** written into the scene file — hidden portraits a test draws from and returns to while the daemon runs:

- **`face_pool(count=FACE_POOL_SIZE, images=None)`** → the `FacePlane`s of a pool: `face_1` … `face_<count>`, all hidden, parked at the default position. `FACE_POOL_SIZE = 3` — the followed face, a rival and one more cover the head tracking scenarios; a consumer that needs more writes a bigger pool. `images` is a list of PNG paths assigned round-robin across the pool (the bundled portrait alone when `None`), so a pool of four over two images holds two of each. `write_test_scene(out_dir)` writes `face_pool()`; a consumer writes a bigger pool, or its own `FacePlane`s, by passing `faces=`.
- **One texture per image.** The scene file declares one texture and one material per distinct image and every portrait of that image references them, so a portrait costs a body and a box, not a texture: the pool's size costs nothing measurable in the render.
- **A prop's kind and image are discovered, not declared twice.** A body's **kind** is its name up to the last `_<n>` (`face_3` → `face`); its **image** is the stem of its texture's file (`face.png` → `face`), which the scene file carries in the material's name (`portrait_<stem>`). The director reads both when it attaches, and a body's state reports them.
- **Free is hidden.** A pooled prop is free while hidden. `spawn(pos, quat=None, *, kind="face", image=None, duration=0)` takes the lowest-numbered free prop of that kind — of that image when one is named — places it (a timed move from its parked pose when `duration > 0`, else at once) and shows it, **in one director command under its lock**, and returns its state; `despawn(name)` hides it (its pose stays, it is free again). `clear()` hides every prop. No free prop of that kind (or image) is a `SimSceneError` naming the pool's size and `face_pool(count=...)`; an image no prop carries is a `SimSceneError` listing the images the scene has.
- **Same image, several faces.** Portraits sharing an image are identical to a detector and a tracker — the bridge's `track_id`s tell them apart by position ([user_perception.md](../vision/user_perception.md) "Tracks"), which is what the head tracking tests need. A test that needs to tell *people* apart (a recogniser) needs different images: more portraits land in `assets/` with their attribution and in `face_pool(images=...)`, nothing else changes.
- **Apparent size is distance.** Every portrait of a pool has the same physical size; a test makes one face bigger than another by spawning it nearer (0.35 m against 0.60 m, say — both inside the detector's measured range, below), the way people at a desk differ in size.

### Loading a file upstream only loads by name

Upstream's `MujocoBackend` builds the scene path as `<its mjcf dir>/scenes/<name>.xml` from `--scene <name>`. `upstream_scene_name(path)` returns the path **relative to that `scenes/` directory, minus `.xml`** — a name upstream resolves to any file on the machine, so the scene itself needs no change to upstream's loading code. It requires an existing `.xml` file (`FileNotFoundError` / `ValueError` otherwise). The scene file's own `<include>` and `meshdir` are absolute precisely because the file lives outside upstream's tree.

### The director: bodies driven from inside the physics loop

`SceneDirector` is installed with `mujoco.set_mjcb_control(director.step)` — the global control callback MuJoCo invokes on **every `mj_step`, on the daemon's physics thread**, with the model and data. It is installed **after the model is built, never before**: MuJoCo's compiler runs the callback on the half-built model while loading a scene, and the Python binding fails wrapping that model before any callback code runs, so a callback present during `MjModel.from_xml_path` makes every load fail (`engine error: Python exception raised`). The scene extension's `on_backend` hook does the install — the sim daemon launcher calls it at the end of the backend's `__init__`, per instance, so a daemon restart re-installs it. On its first call the director **attaches**: discovers every named `mocap` body, its geoms, its kind and its image, and records their initial pose. On each call it writes each body's commanded pose into `data.mocap_pos` / `data.mocap_quat` and its visibility into the alpha of its geoms (`model.geom_rgba[…, 3]`; the renderer skips alpha 0, so a hidden face vanishes from the eye camera and from the viewer window alike — the body keeps its pose). Writes are skipped when nothing changed, so the callback costs nothing at 500 Hz while idle.

A **timed move** (`duration > 0`) interpolates linearly on the **wall clock** (a monotonic clock, injectable for tests) from where the body *currently is* — retargeting mid-move starts from the interpolated pose, no jump — to the commanded pose; `duration` 0 lands on the next step. Orientation interpolates by normalised lerp (small rotations; a face stays facing the robot). `moving` reports whether a move is still in flight. Commands come from the HTTP handler thread under a lock the physics thread holds only to copy a few floats; a director bug is logged and swallowed so the physics loop never dies on it.

Validation: an unknown body is a `KeyError`; a position that is not three numbers, a quaternion that is not four (it is normalised on the way in; all-zero is refused), or a negative duration is a `ValueError`.

### Head tracking converges on the face

The scene brings the face; the sim daemon launcher ([sim_daemon.md](../daemon/sim_daemon.md)) streams the rendered eye camera to the client, the bridge's detector ([user_perception.md](../vision/user_perception.md)) finds the portrait in it as it would a person on a robot, and the bridge's tracker ([head_tracking.md](../motion/head_tracking.md)) aims the head with the eye camera's true intrinsics (upstream's own daemon-side aim is ~45° off in the sim, and is not used). Nothing about tracking is scene-specific: the same detector, the same tracker, the same loss timeout and hand-back to the idle move as with a person. On the test scene, with the default plane 0.45 m away:

| Face | Head, settled |
|---|---|
| ahead, `(0.45, 0, 0.20)` | yaw ~0°, pitch ~6° (the tracker aims at the nose, below the plane's centre) |
| `(0.45, ±0.15, 0.20)` | yaw ±17–19° — `atan2(0.15, 0.45)` = 18.4°: the eye camera sits on the head's forward axis, so it looks at the face when the head's heading from its pivot (the origin) does |

and the tracked face sits at the image centre (`bridge.faces.value` normalised |x|, |y| under 0.05 — about 3° on the eye camera; measured under 0.025 at settle), which is what tracking guarantees; the head's yaw is a coarser check (within 5°), since the head breathes around the aim — its roaming toned down to a quarter is still about ±2° of yaw on a slow random walk that a 2 s mean does not cancel. On the way the head may swing once past the face and creep back onto it, by a bounded amount, never back past the face: a detection is a few frames old when it is aimed, which the tracker compensates by aiming against the head pose at the frame's time ([head_tracking.md](../motion/head_tracking.md)); the overshoot measured with upstream's daemon-side aim was 3–9.5°; the bridge tracker, aiming against the reported head pose at the frame's time with its delay estimated online, overshoots 0–3° on the viewer sim. A face that returns after the hand-back is converged on the same way. The camera looking is the head-mounted eye camera, so the scene's props need the default `sim` camera source: with a `webcam` source they still move in the viewer, but nothing detects them.

### The router: the daemon's own port

The scene extension's `on_app` hook `include_router`s the inject router at **`/api/sim/inject`** on the app upstream's `create_app` built, so the app is upstream's own, served on the daemon's existing port — no second port to configure or firewall. The path says what the routes do: they **inject** props into the scene the eye camera renders. The sim displays' routes, which draw for the person watching and never for the camera, sit beside them under `/api/sim/displays` ([sim_displays.md](../daemon/sim_displays.md)). Every route acts on bodies, so every route is under `bodies`; the two fixed paths, `bodies/spawn` and `bodies/clear`, are registered before `bodies/{name}`, and a scene refuses props with those names ("Three pieces" above). `run_daemon` parses `--scene-path`, resolves it to the upstream scene name (below), and hands `--scene <name>` with every other flag — `--headless`, `--[no-]preload-datasets`, the camera flags, anything unrecognised — to `run_sim_daemon` with the extension.

| Route | Body | Result |
|---|---|---|
| `GET /api/sim/inject/bodies` | — | `{"attached": bool, "bodies": {name: state}}` — `attached` is false until the first physics step |
| `POST /api/sim/inject/bodies/{name}` | JSON object with any of `pos` `[x,y,z]`, `quat` `[w,x,y,z]`, `duration` (s), `visible` (bool) | the body's new state; `404` unknown body, `400` unknown field or malformed value |
| `POST /api/sim/inject/bodies/spawn` | JSON object: `pos` (required), `quat`, `duration`, `kind` (default `"face"`), `image` (a stem, or `null` for any) | the spawned body's state; `409` no free prop of that kind / image, `400` malformed value or an image the scene does not carry |
| `POST /api/sim/inject/bodies/clear` | — | `{"bodies": {name: state}}` with every prop hidden |

A state is `{"name", "kind", "image", "pos", "quat", "visible", "moving"}` (`image` `null` for a prop without a texture) (`BodyState` on both sides; `pos`/`quat` are the *commanded* pose, the destination of a move in flight).

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

`DaemonConfig.scene` ([config.md](../core/config.md)) keeps its type (a non-empty string or `null`) and gains a second meaning: a value **ending in `.xml` is a scene file**, anything else an upstream scene name. `launch_command` ([daemon.md](../daemon/daemon.md)) routes a scene file to this module's launcher — `mjpython -m reachy_mini_bridge.testing.sim_scene --scene-path <abs> …` for the viewer, `<this interpreter> -m … --headless …` headless — with the same `sim`-extra check and the same `DaemonError`s as the plain recipes, and the path made absolute at launch. A `real` daemon ignores `scene` as before. Nothing else in the lifecycle changes: readiness, borrowing, teardown and the environment scrub apply unchanged, and a `ReachyMiniBridge` whose config names a scene file brings the bridge's test scene up like any other sim.

### The testing harness

[testing_support.md](testing_support.md) runs every sim it spawns on this scene:

- **The test scene is the harness's sim scene** — no knob: the harness writes it into a temporary directory that lives as long as the daemon it spawns and passes its path as `DaemonConfig.scene`. Its props start hidden, so every other test runs on upstream's empty scene. A daemon already ready at the address is borrowed as before, whatever it runs.
- **`faces` capability** — probed like the others: the inject endpoint at the daemon's address answers and lists at least one body of kind `face`. Present on every harness-spawned sim; absent on a borrowed daemon started without the scene and on a real robot, so `requires_caps(live_bridge, "camera", "faces")` — with `camera` absent headless — runs a tracking test on the viewer sim only. (`faces` says the face exists; `camera` says something is looking at it — a face test needs both.)
- **`sim_scene` fixture** (module-scoped, next to `live_bridge` in the plugin module) — a `SimSceneClient` on the fixture-managed daemon. A test spawns the portraits it needs and the bridge's own tests `clear()` the scene around each test, so every test starts with nobody in view.

The bridge's own tier ([testing.md](testing.md)) dogfoods it in the attention / gaze tests of `tests-e2e/test_bridge.py`, asserting how the head moves and where it settles ("Head tracking converges on the face" above): the head turns toward a face ahead and to either side, swings past it at most once and by a bounded amount (never back past it: no oscillation), and settles at the yaw the face's position implies — `atan2(y, x)` from the head's pivot, within 3° — with the tracked face near the image centre and its pitch unchanged; hidden, the tracker withdraws its aim after the loss timeout (`attention` reads `watching`) and the head settles back into the idle move — averaging a few degrees off neutral rather than holding the face's yaw, and breathing on the z axis; the face returning elsewhere re-engages `attention` and the head converges on the new position; an emotion plays over tracking (visibly moving the head through its own choreography) and the head converges on the still-visible face again once it ends.

With several portraits it also pins **whom the head follows** ([head_tracking.md](../motion/head_tracking.md) "Whom the head follows"), each through `bridge.head_tracking`'s `track_id` and the head's yaw:

- **The biggest first.** Two portraits spawned together, one near (0.35 m, to one side) and one far (0.60 m, to the other): the head converges on the near one, and the reported `track_id` is the near face's in `bridge.faces`.
- **Stickiness.** Following a far portrait alone, a nearer one spawned beside it: the head stays on the far one, the `track_id` unchanged, for longer than `TRACKING_SWITCH_S`.
- **The hold.** Following one of two, the followed one despawned and spawned back at the same place within half of `TRACKING_SWITCH_S`: the head holds its direction meanwhile (yaw within a couple of degrees), the same `track_id` is followed afterwards, and `bridge.head_tracking.changes()` publishes nothing.
- **The switch.** Following one of two, the followed one despawned for good: the head holds for about `TRACKING_SWITCH_S`, then one change names the other face's `track_id` and the head converges on it.
- **The loss.** The last portrait despawned: after `TRACKING_LOST_S`, `attention` reads `watching` and the head hands back to the idle move, as with one face.

## Open questions

1. **Headless camera on Linux.** Upstream never starts the render thread headless, but with an EGL/OSMesa GL context the launcher could start it itself and the face tests could run on CI without a window. Deferred until a Linux CI target exists; the pieces (scene, director, router) would not change.
2. **Scene files as a first-class upstream feature.** The `upstream_scene_name` relative-path trick rests on upstream formatting `scenes/{name}.xml`; a `--scene-path` upstream flag would make it unnecessary. Worth proposing upstream; pinned by `tests/test_sim_scene.py` meanwhile.
3. **More than faces.** The director drives *any* named `mocap` body, so another kind of prop — a mocap object for a future `look_at` verb, a person's body for a presence detector — is a new dataclass beside `FacePlane`, written into the scene and spawned by its kind; deferred until a spec needs one.
4. **Changing a portrait's image at runtime.** Swapping a texture on a running model means re-uploading it to the viewer's GL context, from the render thread; the pool assigns images at write time instead. Revisit if a test needs more distinct images than a pool can carry.
