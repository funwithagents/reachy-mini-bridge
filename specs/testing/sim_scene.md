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
  - tests-e2e/test_perception.py
  - tests-e2e/test_head_tracking.py
  - tests-e2e/test_scene_props.py
---

# Sim scene — props in the MuJoCo sim (`sim_scene.py`)

**Status:** Implemented

## Purpose

The `sim` backend is the one place the bridge's *perception-driven* behaviours — face detection and the `faces` report ([user_perception.md](../vision/user_perception.md)), head tracking and the hand-back to the idle move when nobody is there ([head_tracking.md](../motion/head_tracking.md), [motion.md](../motion/motion.md) "The gaze layer") — can be exercised end to end without a person in front of a robot: the MuJoCo daemon renders its eye camera offscreen and streams that image to the client, where the bridge's detector — the same model upstream's daemon runs on a robot — sees it. What upstream lacks is anything to *look at* and any way to move it: its scenes are loaded by name from inside its package, ship no faces, and expose no runtime control over the objects in them. This concept adds **portrait planes** in front of the robot — photos of faces, each textured onto a thin body the bridge can **spawn, move and despawn** while the daemon runs, as many at once as a test needs — so an e2e test on the viewer sim can watch the head turn to face one, stay with it while another appears, wait for it when it vanishes and turn to the biggest other one when it does not come back, hand itself back to breathing once everyone leaves, and so a person can watch the same in the viewer window. The daemon runs through the bridge's sim daemon launcher ([sim_daemon.md](../daemon/sim_daemon.md)), the bridge's detector finds the portrait in the rendered camera stream ([user_perception.md](../vision/user_perception.md)), and the bridge's own tracker aims the head at it — so the tests assert where the head ends up, not only that it moved.

A portrait is one kind of **prop**: an image on a plane. A consumer adds props of its own kinds — a photo of a hand for a gesture recogniser, an object — to the scene the harness spawns, and spawns, moves and despawns them the same way ([testing_support.md](testing_support.md) "Props and capabilities a consumer declares").

## Core concepts / Decided

### The real pipeline, not a fake detection

Nothing here fakes a face. The portrait is a real object in the physics scene; the daemon's own render thread draws the eye camera's view at 25 Hz, streams it over UDP into the daemon's GStreamer media server, which tees it to the camera IPC socket the client's `get_frame()` reads — the bridge's camera feed ([camera.md](../vision/camera.md)), sampled by its detection loop, whose shipped detector (downscale to 320 px wide, YuNet on ONNX Runtime — the model upstream's own daemon-side tracker runs) finds the portrait and whose tracker turns it into the head's aim, exactly as with a person ([user_perception.md](../vision/user_perception.md) "Detectors"). The bridge only puts the face there and moves it.

This needs a camera looking at the scene: the **viewer** daemon on every platform (upstream starts the render thread under the viewer; [daemon.md](../daemon/daemon.md): an unlocked GUI session, `mjpython` on macOS), or a **headless** daemon on Linux, where the bridge's launcher renders the eye camera offscreen through EGL ([sim_daemon.md](../daemon/sim_daemon.md) "The headless camera") — what CI runs ([ci.md](ci.md)). A macOS headless sim has no camera: the scene still loads and its bodies still move, there is just nothing looking at them.

### Three pieces, one process boundary

The daemon is a separate process (its `MjData` lives there), so the concept splits along it:

| Piece | Where it runs | Role |
|---|---|---|
| **Scene file** — `write_test_scene(out_dir, props=None)` → `Path` | bridge (or a consumer's conftest) | Writes `<out_dir>/scene.xml`: upstream's `empty` scene (skybox, checker floor, light) plus one **`mocap` body per prop** — an `ImagePlane`: a thin box textured with its image, upright and facing the head, at the **visibility its dataclass says** — by default the pool of hidden portraits (`face_pool()`, "A pool of portraits" below). The robot model and its meshes are included by **absolute path**, textures too, so the file loads from anywhere. |
| **Launcher** — `python -m reachy_mini_bridge.testing.sim_scene --scene-path S [--headless] [--no-preload-datasets] [camera flags] [upstream flags…]` | daemon process (under `mjpython` for the viewer) | Runs the bridge's sim daemon launcher ([sim_daemon.md](../daemon/sim_daemon.md)) on that file with the scene's `SimDaemonExtension`: the **director** installed as MuJoCo's control callback, the **inject router** mounted on the daemon's own FastAPI app. |
| **Client** — `SimSceneClient(host, port)` | bridge / tests | Drives the router over the daemon's HTTP port: `spawn(pos, quat=None, *, name=None, kind="face", duration=0)` → the name of the prop it showed, `despawn(name)`, `clear()`, and per body `bodies()`, `place(name, pos, quat=None, duration=0)`, `show(name)`, `hide(name)`, `wait_still(name, timeout)`. Every failure is a `SimSceneError` (a `BridgeError`), including "this daemon was not launched through the sim-scene launcher". |

### A prop is an image on a plane

`ImagePlane(name, image, pos=DEFAULT_FACE_POS, height=None, width=None, visible=False)` — `image` a PNG path; `pos` in world metres (by default 0.45 m in front of the robot, at eye-camera height); exactly one of `height` and `width` in metres, the other following from the image's aspect ratio, so a photo is never stretched — a stretched hand is another hand to a classifier. Both, neither, or a size that is not a positive finite number is a `ValueError` at write time. `plane.size(aspect)` gives the (width, height) the writer uses.

- **PNG only.** MuJoCo decodes PNG textures and nothing else: any other file fails the scene's compile inside the daemon, in its log, far from the caller. The writer reads every image's 8-byte PNG signature and its `IHDR` chunk (width, height) — no decoder, no dependency — and refuses a file that is not a PNG with a `ValueError` naming it and saying to convert it to PNG; a missing file is a `FileNotFoundError`. A consumer holding photos converts them itself.
- **Names** must be unique and non-empty, and neither `spawn` nor `clear` (the inject router's own paths under `bodies/`, below). A prop's **kind** is its name up to the last `_<n>` ("A pool of portraits" below), so `hand_1` … `hand_5` are five props of kind `hand`.
- **`image_pool(kind, images, *, count=None, height=None, width=None)`** builds the planes of a pool: `<kind>_1` … `<kind>_<count>`, hidden, parked at the default position, images assigned in order — one prop per image when `count` is `None`, round-robin when it is larger — each sized by the one dimension given. A kind is non-empty and does not itself end in `_<n>`.
- **Orientation** is the portraits' for every prop (`FACE_QUAT`): thin axis toward the robot, height axis up, the image upright and unmirrored as the eye camera sees it.

### A pool of portraits

MuJoCo compiles a scene when the daemon loads it: bodies cannot be added to a running model. "As many portraits as a test needs" is therefore a **pool** written into the scene file — hidden portraits a test draws from and returns to while the daemon runs:

- **One person per prop.** The bridge bundles four public-domain portraits (`FACE_IMAGES`, `assets/`, credited in `assets/ATTRIBUTION.md`), one person each, all cropped to the same framing (320×400, the face box about half the height), and the default pool shows each of them once, in this order:

  | Prop | Person | Pose | Constant |
  |---|---|---|---|
  | `face_1` | Armstrong | frontal | — |
  | `face_2` | Chawla | frontal | — |
  | `face_3` | Schmid | head turned ~30° toward the image's right | `TURNED_FACE` |
  | `face_4` | Malenchenko | head tilted down | `LOOKING_DOWN_FACE` |

  A prop is a user: it is the same face every time it is spawned, no two props look alike, and a test reads "`face_2` appears, moves, leaves" with nothing to know about the photo. `FACE_POOL_SIZE = 4` covers the head tracking scenarios — the followed face, a rival, and the two poses. The two frontal faces come first, so the tests that spawn without a name get frontal faces.
- **`face_pool(count=None, images=None)`** → `image_pool("face", images or FACE_IMAGES, count=count, height=FACE_HEIGHT_M)`: `face_1` … `face_<count>`, all hidden, parked at the default position, life size (`FACE_HEIGHT_M` = 0.36 m tall; the bundled 320×400 portraits are 0.288 m wide). `write_test_scene(out_dir)` writes `face_pool()`; a consumer writes another pool, or its own planes, by passing `props=` — the harness adds a consumer's declared props to the default pool ([testing_support.md](testing_support.md)).
- **One texture per image.** The scene file declares one texture and one material per distinct image and every portrait of that image references them, so a portrait costs a body and a box, not a texture: the pool's size costs nothing measurable in the render.
- **A prop's kind and image are discovered, not declared twice.** A body's **kind** is its name up to the last `_<n>` (`face_3` → `face`); its **image** is the stem of its texture's file (`armstrong.png` → `armstrong`), which the scene file carries in the material's name (`prop_<stem>`; a second file of the same stem gets `prop_<stem>__2`, `__3`…, a suffix the reader strips, and the writer refuses a file whose stem itself ends in `__<digits>` — so a stem always reads back whole: `person_1.png` is the image `person_1`). The director reads both when it attaches, and a body's state reports them.
- **Free is hidden.** A pooled prop is free while hidden. `spawn(pos, quat=None, *, name=None, kind="face", duration=0)` shows the prop `name` — a given person — or, with no name, the lowest-numbered free prop of that kind; it places it (a timed move from its parked pose when `duration > 0`, else at once) and shows it, **in one director command under its lock**, and returns its state; `despawn(name)` hides it (its pose stays, it is free again). `clear()` hides every prop. A named prop already shown is a `SimSceneError` (a person is in view once), an unknown name one too; no free prop of that kind is a `SimSceneError` naming the pool's size and `image_pool(count=...)`.
- **Telling people apart.** The tracker tells faces apart by position (`track_id`, [user_perception.md](../vision/user_perception.md) "Tracks"), never by appearance, so whom the head follows does not depend on who is pictured; the distinct faces are for the person watching the viewer, for `BodyState.image`, and for a recogniser's test. A consumer's pool repeating an image round-robin holds look-alikes, which the tracker handles the same way.
- **Apparent size is distance.** Every portrait of a pool has the same physical size; a test makes one face bigger than another by spawning it nearer (0.35 m against 0.60 m, say — both inside the detector's measured range, below), the way people at a desk differ in size.

### Loading a file upstream only loads by name

Upstream's `MujocoBackend` builds the scene path as `<its mjcf dir>/scenes/<name>.xml` from `--scene <name>`. `upstream_scene_name(path)` returns the path **relative to that `scenes/` directory, minus `.xml`** — a name upstream resolves to any file on the machine, so the scene itself needs no change to upstream's loading code. It requires an existing `.xml` file (`FileNotFoundError` / `ValueError` otherwise). On Windows a relative path cannot cross drives, so the file must sit on the drive upstream is installed on; another drive is a `ValueError` naming that drive. The harness writes its test scene under the system temp folder, so a Windows machine whose `%TEMP%` and environment are on different drives points `TEMP` at a folder on the environment's drive (CI points it at the runner's temp folder, on the checkout's drive). The scene file's own `<include>` and `meshdir` are absolute precisely because the file lives outside upstream's tree.

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
| `POST /api/sim/inject/bodies/spawn` | JSON object: `pos` (required), `quat`, `duration`, `kind` (default `"face"`), `name` (a prop, or `null` for the lowest free one of `kind`) | the spawned body's state; `404` unknown name, `409` that prop already shown or no free prop of that kind, `400` malformed value |
| `POST /api/sim/inject/bodies/clear` | — | `{"bodies": {name: state}}` with every prop hidden |

A state is `{"name", "kind", "image", "pos", "quat", "visible", "moving"}` (`image` `null` for a prop without a texture) (`BodyState` on both sides; `pos`/`quat` are the *commanded* pose, the destination of a move in flight).

### Geometry

Numbers that make the default plane reliably detected, measured with upstream's detector on rendered frames (not estimated):

| | Value |
|---|---|
| eye camera, neutral head | ~0.04 m forward, ~0.20 m up, looking along world +x; fovy 80°, 1280×720 |
| plane orientation `FACE_QUAT` | thin axis toward the robot (−x), height axis up, width axis right-to-left as the camera sees it — the portrait reads upright, not mirrored |
| default plane | 0.288 × 0.36 m at (0.45, 0, 0.20) — **life size**: the pool's framing puts the face box at 0.49 of the portrait's height, so YuNet's box spans 0.18 m, `FACE_BOX_HEIGHT_M` ([user_perception.md](../vision/user_perception.md) "The face report"), and a portrait's `distance_m` reads its true distance; ~340 px tall in the frame, a ~170 px face, ~85 px after the detector's stride to 640 px wide |
| detector floor | YuNet fires reliably from ~40 px of portrait height in the downscaled frame; below ~30 px it does not. Every pooled portrait — frontal, turned, tilted down — is detected at every lateral position tested, from 0.35 m to 0.80 m away (measured at width 320 on the 0.25 m portraits, head still; the life-size portraits at the default 640 are larger in the detected image on both counts) |
| pose limit | a near-profile face (~75° of yaw) is detected at 0.35 m and not reliably beyond — erratically with distance and from one face to another; no pooled portrait is a profile, and no test spans that edge |
| aim point | the tracker centres the nose: on a frontal portrait it sits within a tenth of the face box's width of its middle (under 1° at 0.45 m), on the turned and the tilted-down ones about a fifth (~2°), inside the convergence kit's yaw tolerance |
| roll | measured 2026-11-03 on the life-size portraits rolled ±20° at the default width 640, head still: the eye-line roll reads 0.35–0.6 of the truth at 0.28 m, about 0.4 at 0.35 m, about 0.15 at 0.45 m — less than the same photo rotated flat at the same pixel size ([user_perception.md](../vision/user_perception.md) "The shipped detector — `yunet.py`"), the rendered texture being softer. Upright, `face_2` reads within 0.5° of level; the Armstrong photo (`face_1`) reads −4 to −6°, a tilt of the photo's own. The mirroring live test spawns `face_2` rolled 25° at 0.28 m |
| lateral travel | ±0.15 m at 0.45 m is ~±20° of yaw — well inside the head's range, clearly measurable on `get_current_head_pose()` |
| material | `emission="1"`, no specular — the portrait's pixels reach the camera unshaded whatever the scene lighting |
| floor | `reflectance="0"` (upstream's own `empty`/`minimal` scenes use `0.2`) — a reflective floor mirrors the face plane, upside down; YuNet detects the reflection as readily as the real face, and the daemon-side tracker can lock onto it instead — confirmed by capturing a live frame with the reflection visible and reproducing it with `reflectance="0.2"` restored. Zeroed in the generated scene only; nothing upstream changes |

Every prop's geom has `contype`/`conaffinity` 0: it collides with nothing and upstream's start-up collision toggling ignores it.

### Configuration and the daemon lifecycle

`DaemonConfig.scene` ([config.md](../core/config.md)) keeps its type (a non-empty string or `null`) and gains a second meaning: a value **ending in `.xml` is a scene file**, anything else an upstream scene name. `launch_command` ([daemon.md](../daemon/daemon.md)) routes a scene file to this module's launcher — `mjpython -m reachy_mini_bridge.testing.sim_scene --scene-path <abs> …` for the viewer, `<this interpreter> -m … --headless …` headless — with the same `sim`-extra check and the same `DaemonError`s as the plain recipes, and the path made absolute at launch. A `real` daemon ignores `scene` as before. Nothing else in the lifecycle changes: readiness, borrowing, teardown and the environment scrub apply unchanged, and a `ReachyMiniBridge` whose config names a scene file brings the bridge's test scene up like any other sim.

### The testing harness

[testing_support.md](testing_support.md) runs every sim it spawns on this scene:

- **The test scene is the harness's sim scene**: the harness writes the default pool of portraits plus the props the run declares (`pytest_reachy_mini_scene_props`, [testing_support.md](testing_support.md)) into a temporary directory that lives as long as the daemon it spawns and passes its path as `DaemonConfig.scene`. Its props start hidden, so every other test runs on upstream's empty scene. A daemon already ready at the address is borrowed as before, whatever it runs.
- **`props:<kind>` capabilities** — one per kind the run's scene carries, probed like the others: the inject endpoint at the daemon's address answers and lists at least one body of that kind. `props:face` is present on every harness-spawned sim; absent on a borrowed daemon started without the scene and on a real robot, so `requires_caps(live_bridge, "camera", "props:face")` — `camera` absent on a macOS headless sim — runs a tracking test where something looks at the face: the viewer sim, or a Linux headless sim with its offscreen render. (`props:face` says the face exists; `camera` says something is looking at it — a face test needs both.) A consumer's kinds are capabilities the same way (`props:hand`).
- **`sim_scene` fixture** (module-scoped, next to `live_bridge` in the plugin module) — a `SimSceneClient` on the fixture-managed daemon. A test spawns the props it needs; the `face_scene` fixture beside it ([testing_support.md](testing_support.md) "Public surface") gates on `camera` + `props:face` and `clear()`s the scene before and after the test — after it, waiting for the running detection to report nobody before turning it off — so every face test starts with nobody in view — the bridge's own face tests all take it.

The bridge's own tier ([testing.md](testing.md)) dogfoods it in the attention / gaze tests of `tests-e2e/test_head_tracking.py` (the faces report in `tests-e2e/test_perception.py`), through the shipped convergence kit `reachy_mini_bridge.testing.gaze` ([testing_support.md](testing_support.md)), asserting how the head moves and where it settles ("Head tracking converges on the face" above): the head turns toward a face ahead and to either side, swings past it at most once and by a bounded amount (never back past it: no oscillation), and settles at the yaw the face's position implies — `atan2(y, x)` from the head's pivot, within the kit's yaw tolerance of 5° (the head keeps breathing around the aim; the precise check is the face at the image centre, [testing_support.md](testing_support.md)) — with the tracked face near the image centre and its pitch unchanged; hidden, the tracker withdraws its aim after the loss timeout (`attention` reads `watching`) and the head settles back into the idle move — averaging a few degrees off neutral rather than holding the face's yaw, and breathing on the z axis; the face returning elsewhere re-engages `attention` and the head converges on the new position; an emotion plays over tracking (visibly moving the head through its own choreography) and the head converges on the still-visible face again once it ends; and someone not looking at the robot — `TURNED_FACE`, then `LOOKING_DOWN_FACE` — is converged on to one side and followed to the other like a frontal face, centred each time.

With several portraits it also pins **whom the head follows** ([head_tracking.md](../motion/head_tracking.md) "Whom the head follows"), each through `bridge.head_tracking`'s `track_id` and the head's yaw:

- **The biggest first.** Two portraits spawned together, one near (0.35 m, to one side) and one far (0.60 m, to the other): the head converges on the near one, and the reported `track_id` is the near face's in `bridge.faces`.
- **Stickiness.** Following a far portrait alone, a nearer one spawned beside it: the head stays on the far one, the `track_id` unchanged, for longer than `TRACKING_SWITCH_S`.
- **The hold.** Following one of two, the followed one despawned and spawned back at the same place within half of `TRACKING_SWITCH_S`: the head holds its direction meanwhile (yaw within a couple of degrees), the same `track_id` is followed afterwards, and `bridge.head_tracking.changes()` publishes nothing.
- **The switch.** Following one of two, the followed one despawned for good: the head holds for about `TRACKING_SWITCH_S`, then one change names the other face's `track_id` and the head converges on it.
- **The loss.** The last portrait despawned: after `TRACKING_LOST_S`, `attention` reads `watching` and the head hands back to the idle move, as with one face.

## Open questions

1. **Scene files as a first-class upstream feature.** The `upstream_scene_name` relative-path trick rests on upstream formatting `scenes/{name}.xml`; a `--scene-path` upstream flag would make it unnecessary. Worth proposing upstream; pinned by `tests/test_sim_scene.py` meanwhile.
2. **Props of other shapes.** Every prop is an image on a plane; a solid object — for a future `look_at` verb, a person's body for a presence detector — would be another dataclass beside `ImagePlane`, written into the scene and spawned by its kind like any prop. Deferred until a spec needs one.
3. **Changing a portrait's image at runtime.** Swapping a texture on a running model means re-uploading it to the viewer's GL context, from the render thread; the pool assigns images at write time instead. Revisit if a test needs more distinct images than a pool can carry.
4. **A person turning their head.** Every pooled pose is a different person, so a frontal face swapped for a turned one at the same place is another user, not the same one turning. A "turns their head while followed" test needs one person photographed in two poses (a public-domain portrait session with a frontal and a three-quarter shot); deferred until such a pair is found.
