# Face detection loop and the observable face report (daemon source)

**Status:** Done

Implements [specs/core/observable.md](../specs/core/observable.md) in full and [specs/vision/user_perception.md](../specs/vision/user_perception.md) "The face report", "The report is an observable", "The detection loop", "Detection sources" (the `daemon` source), "Configuration" (the `faces` block) and "Lifecycle", with the consumed-slice change in [specs/core/robot.md](../specs/core/robot.md) (the REST read replaces `get_tracked_face`; the fake's `show_face` / `hide_face`). Delivers `Observable[T]`, the `Face` / `FaceReport` types, a detection loop polling the daemon's `GET /api/media/tracking/face` and publishing `api.faces` with the count-change debounce, the `faces` config block and the `set_face_detection` verb — and re-bases the existing attention loop on the detection loop's observations, so it stops polling the 1 Hz status stream. Deliberately leaves out the bridge's head tracker (the daemon still steers the head in this plan — [202609261041](202609261041_bridge-head-tracker-and-gaze-layer.md)) and the `custom` source ([202609291000](202609291000_camera-feed-and-custom-face-detectors.md)); a config naming `custom` is rejected at session entry until then.

First of three plans; the other two build on it in order.

## How to work this plan

- **Read first:** [AGENTS.md](../AGENTS.md); [specs/vision/user_perception.md](../specs/vision/user_perception.md) in full (the design — do not redesign it); [specs/core/api.md](../specs/core/api.md) "Faces (perception)", "Attention / gaze" and "Lifecycle"; [specs/core/config.md](../specs/core/config.md) "`faces` block"; [specs/core/robot.md](../specs/core/robot.md) "The consumed slice"; [docs/reachy-mini-api.md](../docs/reachy-mini-api.md) "Face tracking" for the daemon facts.
- **Do the steps in order.** Each ends with the same check; fix everything red before the next step:

  ```
  uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest
  ```

  (`ruff format .` also reflows the Python blocks in `plans/*.md` — format the code directories only.)
- **Tests are functional:** assert on `api.faces.value`, on what a `changes()` subscriber receives, on the fake's recorded commands (`start_head_tracking` weights) and on the `attention` property — never on internals. Keep fake-tier tests fast: monkeypatch `FACE_POLL_HZ`, `FACE_ABSENT_S` and `ATTENTION_GRACE_S` to tenths of a second; sleeps ≤ 1.5 s. Every stream added here gets a cancel-mid-flight test ([specs/core/api.md](../specs/core/api.md) "Cancellation").
- **Do not commit** unless asked. Do not touch `docs/upstream-*.md` beyond what a step says.

## Facts you must not violate (daemon, SDK 1.10)

1. The daemon runs its face detector **only while tracking is enabled at a requested weight above zero**; `enable_head_tracking(0.0)` pauses the detector and clears the face target. Any weight above zero also blends the daemon's own aim into the head by that weight. So in this plan the daemon's tracker is armed by **whoever needs it**: the attention loop's weight when tracking is on (unchanged behaviour), `DAEMON_DETECT_WEIGHT` (0.001) when only detection wants it. The two never fight: the detection loop arms ε only while `api.tracking` is off, and the attention loop's `stop_head_tracking` re-arms ε when detection is still wanted.
2. The daemon's status stream is published at **1 Hz**; the face target read through `GET /api/media/tracking/face` is the backend's current one. The endpoint answers `503` until the backend is ready. The payload is `{"status": "ok", "face_target": {"detected": bool, "x": float|null, "y": float|null, "roll": float|null, "ts": float|null}}`, `ts` from the daemon's `time.monotonic()`.
3. `x`, `y` are the face's nose in normalised image coordinates, `[-1, 1]`, x right, y down.
4. In the sim, the daemon updates its face target only through the launcher's stepping correction ([specs/daemon/sim_daemon.md](../specs/daemon/sim_daemon.md)); nothing in this plan changes the launcher.
5. `_fetch_json` is patched by existing tests (`tests/test_api.py`, the testing harness) — keep a patchable seam when you move it.

## Scope

- `src/reachy_mini_bridge/observable.py` — `Observable[T]` (replaces the placeholder).
- `tests/test_observable.py` — new.
- `src/reachy_mini_bridge/robot.py` — `fetch_daemon_json(robot, path)` (the `_fetch_json` seam, moved here from `api.py` so `face_detection.py` and `api.py` share it without a cycle).
- `src/reachy_mini_bridge/face_detection.py` — `Face`, `FaceReport`, the constants, `daemon_face_target(robot)`, `FaceDetection` (the loop, `daemon` source only).
- `tests/test_face_detection.py` — new.
- `src/reachy_mini_bridge/config.py` — `FaceSettings`, `FACE_DETECTORS`, `ReachyMiniConfig.faces`, validation.
- `tests/test_config.py` — the `faces` block.
- `config.example.json` — the `faces` block.
- `src/reachy_mini_bridge/fake_reachy_mini.py` — `client.face_target`, `show_face` / `hide_face`; `face_detected` removed; `get_tracked_face` reads `face_target`.
- `tests/test_fake_reachy_mini.py`, `tests/test_robot.py` — the fake's face target.
- `src/reachy_mini_bridge/api.py` — `faces`, `set_face_detection` / `face_detection`, lifecycle, the attention loop as a subscriber, `_fetch_json` import.
- `tests/test_api.py` — faces tests; attention tests re-based on `show_face` / `hide_face`.
- `src/reachy_mini_bridge/__init__.py` — export `Face`, `FaceReport`, `Observable`.
- `tests/test_project_map.py` — nothing; `AGENTS.md` — the `observable.py` row loses its placeholder note.
- `examples/control_panel/controller.py`, `examples/control_panel/app.py`, `tests/test_control_panel.py` — `faces` count and `face_detection` in the snapshot and the state panel, the Detection checkbox, as [specs/examples/control_panel.md](../specs/examples/control_panel.md) already describes (it reads `Updated` for this).
- `docs/reachy-mini-api.md` — the 1 Hz status cadence and the REST endpoint recorded under "Face tracking".
- `README.md` — the "Following a face" row and the Gaze row of the verb table.
- `tests-e2e/test_api.py` — an *appeared* / *left* test on `faces.changes()` over the sim scene's show / hide.
- `plans/_index.md`, this file — status.

## Steps

### Step 0 — Baseline

Run the check command. Everything must be green before you change anything.

### Step 1 — `Observable[T]`

**File:** `src/reachy_mini_bridge/observable.py` (replace the placeholder's body; keep a module docstring pointing at `specs/core/observable.md`).

```python
class Observable[T]:
    def __init__(self, initial: T) -> None: ...
    @property
    def value(self) -> T: ...
    def set(self, value: T) -> None: ...      # replace + publish to every subscriber
    def update(self, value: T) -> None: ...   # replace silently
    def changes(self) -> AsyncIterator[T]: ...
    async def wait_for(self, predicate: Callable[[T], bool]) -> T: ...
```

- One `asyncio.Queue(maxsize=1)` per live subscriber, created when `changes()` is first iterated (so a subscriber sees only values published after it subscribed), removed in the generator's `finally` (a cancelled `async for` detaches). `set` puts into every queue; on a full queue it drops the queued value and puts the new one (latest wins). `wait_for` returns `value` at once when the predicate holds, else subscribes and returns the first published value that does.
- `set` / `update` assert they run on a thread with a running loop (`asyncio.get_running_loop()`), so a misuse from a worker thread fails loudly; the marshalling helper for producers on other threads is the caller's `loop.call_soon_threadsafe(observable.set, value)`.

**Tests** (`tests/test_observable.py`): `value` reads the initial and the latest set / updated value; a subscriber is woken by `set` and not by `update`; two subscribers each receive every published value; a subscriber that does not consume sees only the latest of a burst; cancelling a task blocked in `async for` ends it promptly and later `set`s do not fail; `wait_for` returns at once on a matching current value and otherwise on the first matching publication; `set` from a plain thread raises.

### Step 2 — Move the daemon HTTP seam to the robot module

**File:** `src/reachy_mini_bridge/robot.py`. Add:

```python
DAEMON_HTTP_TIMEOUT_S = 2.0

def fetch_daemon_json(robot: ReachyMini, path: str) -> Any:
    """GET ``path`` (``/api/...``) from the robot's daemon over HTTP and decode the JSON body."""
```

built from `robot.client.host` / `robot.client.port` with `urllib.request.urlopen`. In `api.py`, `_daemon_kinematics_engine` calls it (`fetch_daemon_json(robot, "/api/kinematics/info")`); delete `_fetch_json` and `_DAEMON_HTTP_TIMEOUT_S` there. Find every patch of `api_module._fetch_json` (tests, `testing/`) and re-point it at `robot_module.fetch_daemon_json` — `grep -rn "_fetch_json" src tests tests-e2e`.

### Step 3 — The face types and the daemon face target

**File:** `src/reachy_mini_bridge/face_detection.py` (replace the placeholder's body).

- `Face`, `FaceReport` exactly as [specs/vision/user_perception.md](../specs/vision/user_perception.md) "The face report" (frozen dataclasses; `FaceReport.inactive(source)` classmethod for the `((), 0.0, source, False)` value).
- Constants: `FACE_POLL_HZ = 10.0`, `FACE_ABSENT_S = 0.3`, `FACE_SOURCE_DOWN_S = 5.0`, `DAEMON_DETECT_WEIGHT = 0.001`.
- `daemon_face_target(robot: AnyReachyMini) -> dict[str, Any]` — the REST payload's `face_target` dict: `fetch_daemon_json(robot, "/api/media/tracking/face")["face_target"]` on a `ReachyMini`, `robot.client.face_target` on the fake (the same `isinstance(robot, FakeReachyMini)` branch as the kinematics read). Blocking; callers run it under `asyncio.to_thread`.
- `report_from_daemon(target: dict, *, active: bool) -> FaceReport` — `detected` true → one `Face(x, y, roll, size=None)`; `ts` from the payload (`0.0` when null).

### Step 4 — The fake's face target

**File:** `src/reachy_mini_bridge/fake_reachy_mini.py`.

- `_FakeDaemonClient.face_target: dict[str, Any]` — starts as the undetected payload (`{"detected": False, "x": None, "y": None, "roll": None, "ts": None}`).
- `FakeReachyMini.show_face(x: float = 0.0, y: float = 0.0, roll: float | None = None) -> None` sets it detected with `ts=time.monotonic()`; `hide_face()` sets it undetected (with `ts`). Delete `face_detected`; `get_tracked_face` builds its `_FakeFaceTarget` from `client.face_target` (still consumed by the attention loop in this plan — it goes in the next).
- Tests: `tests/test_fake_reachy_mini.py` — `show_face` / `hide_face` drive `client.face_target` and `get_tracked_face().detected`. `tests/test_robot.py` parity list unchanged.

### Step 5 — The `faces` config block

**File:** `src/reachy_mini_bridge/config.py`. `FaceSettings` as in [specs/core/config.md](../specs/core/config.md) "`faces` block" (`FaceDetector` imported under `TYPE_CHECKING` from `.face_detection` — a forward reference until the third plan fills it in; define a placeholder `FaceDetector` `Protocol` in `face_detection.py` now, with the `detect` signature from the spec, so the import is real), `FACE_DETECTORS = ("daemon", "custom")`, `from_dict` / `from_json` / `from_json_file` in the style of `MotionSettings` (rejects `face_detector` in a dict — "set from code"), `ReachyMiniConfig.faces` wired in `from_dict` (`faces` added to the known top-level keys). `config.example.json`: add `"faces": {"detector": "daemon", "detection": true}` between `audio` and `motion`.

**Tests** (`tests/test_config.py`): defaults; a full block round-trips; unknown key, bad `detector`, non-bool `detection`, `face_detector` in JSON each raise `ConfigError` naming the field; the example file still loads (the existing test).

### Step 6 — The detection loop

**File:** `src/reachy_mini_bridge/face_detection.py`.

```python
class FaceDetection:
    def __init__(self, robot: AnyReachyMini, *, source: str, faces: Observable[FaceReport],
                 owns_daemon_arming: bool, on_observation: Callable[[FaceReport], None] | None = None) -> None: ...
    async def start(self) -> None: ...   # arms the daemon at DAEMON_DETECT_WEIGHT when it owns the arming; starts the task
    async def stop(self) -> None: ...    # cancels the task; disarms (stop_head_tracking) when it armed; publishes inactive
    def set_owns_daemon_arming(self, owns: bool) -> None: ...
```

`owns_daemon_arming` is a **transitional hand-off switch**, deleted by the next plan. In this plan the daemon still steers the head, so two parties send `start_head_tracking` to it: the attention loop with its own weights while tracking is on (the caller's weight, then `0` and the watching weight on a hand-back), and this loop, which only needs the daemon's detector running and would arm it at `DAEMON_DETECT_WEIGHT`. The last weight sent wins, so they must never both talk to the daemon — `ε` would undo the attention loop's full weight, and the attention loop's `0` would pause the detector this loop relies on. The switch says who owns that call: `True` — nobody is tracking, this loop arms the daemon at `ε` and disarms it when it stops; `False` — the attention loop owns the daemon's weight, and this loop only polls the face target that arming keeps alive. Put this explanation as the docstring of `set_owns_daemon_arming`, so whoever reads the code in between the two plans knows why it exists and that it goes.

- Only `source == "daemon"` is accepted here; `"custom"` raises `ValueError("the custom detection source is not available yet")` from `start()` (the api turns that into a bring-up failure).
- The task loop: every `1 / FACE_POLL_HZ` s, `target = await asyncio.to_thread(daemon_face_target, robot)`; build the report with `active=True`; call `on_observation(report)` (the attention loop's feed — every poll, undebounced); then the **debounce**: keep `published_count` and `lower_since`; if `len(report.faces) > published_count` → `faces.set(report)`, `published_count` updated, `lower_since = None`; if lower → note `lower_since` on the first such poll, `faces.update(report)` until `now − lower_since ≥ FACE_ABSENT_S`, then `faces.set(report)`; equal → `faces.update(report)`.
- Failures: a poll that raises is logged at `DEBUG` and skipped; after `FACE_SOURCE_DOWN_S` of consecutive failures publish the last report with `active=False` (`set`) and one `WARNING`; the next good poll publishes `active=True` again (`set`).
- `stop()` publishes `FaceReport.inactive(source)` through `set`, then detaches nothing — subscribers see the inactive value; the api ends their iteration at exit (Step 7).
- `start()` when `owns_daemon_arming` sends `robot.start_head_tracking(DAEMON_DETECT_WEIGHT)` under `asyncio.to_thread`; `stop()` sends `stop_head_tracking()` only if the loop armed it and `set_owns_daemon_arming(False)` has not since handed the daemon to the attention loop. `set_owns_daemon_arming(True)` while running arms ε at once (the attention loop has just stopped tracking).

**Tests** (`tests/test_face_detection.py`, on `FakeReachyMini`, `FACE_POLL_HZ` → 20, `FACE_ABSENT_S` → 0.15): the report flips to one face after `show_face` and a subscriber is woken once; `hide_face` for less than the absence window then `show_face` again wakes nobody (the `value` did read empty meanwhile); `hide_face` for longer wakes once with an empty report; moving the face (`show_face(0.5, 0.0)` after `show_face(0.0, 0.0)`) updates `value.faces[0].x` without waking; `start` records `start_head_tracking` at `DAEMON_DETECT_WEIGHT` and `stop` records `stop_head_tracking`, and with `owns_daemon_arming=False` neither; a `daemon_face_target` that raises (monkeypatched) for longer than the source-down window publishes `active=False` then `True` on recovery; `on_observation` sees every poll.

### Step 7 — The api: `faces`, the detection switch, lifecycle, attention as a subscriber

**File:** `src/reachy_mini_bridge/api.py`.

- Construction: `self._faces: Observable[FaceReport] = Observable(FaceReport.inactive(cfg.faces.detector))`, `self._face_detection_wanted = cfg.faces.detection`, `self._detection: FaceDetection | None = None`.
- Properties: `faces -> Observable[FaceReport]` (always readable), `face_detection -> bool`.
- `set_face_detection(enabled)`: records the wish; starts the loop if it is not running and anyone wants it (`enabled or self._tracking_wanted`), stops it when nobody does. Needs an entered session (`BridgeError`), as the other mode verbs.
- Lifecycle in `__aenter__`: after `set_wobbling`, before `MotionSession`: build `FaceDetection(robot, source=cfg.faces.detector, faces=self._faces, owns_daemon_arming=not self._tracking_wanted, on_observation=self._on_face_observation)`; `await start()` when `cfg.faces.detection or self._tracking_wanted`; push `self._stop_detection` on the stack (so it runs after the motion session's exit and before wobbling's). `__aexit__` resets `_face_detection_wanted` to the config and publishes the inactive report if the loop never started.
- **Attention loop as a subscriber.** `_run_attention` no longer polls `get_tracked_face`: `_on_face_observation(report)` records `self._face_seen_at = time.monotonic()` when `report.faces` (and stores the latest report). The attention task keeps its transitions on a period of `ATTENTION_POLL_S` but reads `_face_seen_at` instead of the robot; delete the `get_tracked_face` call. The transitional hand-off (Step 6): `_start_tracking_now` calls `self._detection.set_owns_daemon_arming(False)` before its own `start_head_tracking(weight)` (and ensures the loop is running); `stop_head_tracking` calls `set_owns_daemon_arming(True)` after its `stop_head_tracking` when detection is still wanted, else stops the loop. `_stop_tracking_if_on` at exit unchanged in effect.
- `__init__.py`: export `Face`, `FaceReport`, `Observable`.

**Tests** (`tests/test_api.py`): `api.faces.value.active` is `False` before entry, `True` inside a session with the defaults, `False` again after exit; `set_face_detection(False)` with `motion.tracking=False` stops the loop (`active` false, `stop_head_tracking` recorded), and with tracking on changes nothing; a `changes()` subscriber inside a session receives one report on `show_face` and one on `hide_face` past the window; cancelling a task blocked in `async for api.faces.changes()` returns promptly and `say`-free verbs still work; with `motion.tracking=False` and `faces.detection=True` entry records `start_head_tracking(0.001)`; with tracking on entry records the attention loop's `1.0` and no ε; the six attention tests re-based on `show_face` / `hide_face` keep their weight sequences (`[1.0, 0.0, 0.05]`, …); a config with `faces.detector="custom"` fails `__aenter__` with `ValueError` and leaves nothing entered.

### Step 8 — Control panel

`PanelState` gains `faces: int` (`len(api.faces.value.faces)`, `-1` when inactive → shown as `—`) and `face_detection: bool`; the state panel shows both; a Detection checkbox under Modes calls the controller's new `set_face_detection(on)`. Extend the panel's fake-backed tests with one snapshot assertion after `show_face`. This is what [specs/examples/control_panel.md](../specs/examples/control_panel.md) specifies; it goes back to `Implemented` in Step 10.

### Step 9 — Docs and live tier

- `docs/reachy-mini-api.md` "Face tracking": add the status-stream cadence (1 Hz, `Daemon._publish_status`) and the REST endpoint with its payload; note that `get_tracked_face()` is the 1 Hz read.
- `README.md`: the "Following a face" row mentions `api.faces` (read it, or `async for` its changes); the verb table gains a Faces row.
- `tests-e2e/test_api.py`: `test_faces_report_someone_appearing_and_leaving` gated on `camera` + `faces`: subscribe to `api.faces.changes()`, `show` the portrait → the next report has one face within 3 s; `hide` → an empty report within `FACE_ABSENT_S + 3` s. Run the headless sim tier (it skips) and, if a GUI session is available, the viewer tier.
- `AGENTS.md`: drop "(placeholder until its plans land)" from the `observable.py` row.

### Step 10 — Statuses

Mark this plan `Done` here and in [_index.md](_index.md). [specs/core/observable.md](../specs/core/observable.md) is built in full by this plan: `Stable` → `Implemented` (in the file and in `specs/_index.md`); [specs/examples/control_panel.md](../specs/examples/control_panel.md) matches its code again: `Updated` → `Implemented`. [specs/vision/user_perception.md](../specs/vision/user_perception.md) stays `Stable` until the third plan lands (its code still lags the tracker and the custom source).

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e examples && uv run pyright && uv run pytest` green.
- `uv run pytest tests-e2e -rs` on the headless sim: the faces test skips for lack of `camera`, everything else as before. With the viewer (`REACHY_MINI_E2E_SIM_VIEWER=1`): the new faces test and the three attention / gaze tests pass.
- Manual: the control panel on `config.example.json` shows the face count going 0 → 1 → 0 as you step in and out of the webcam's view, within about half a second.
