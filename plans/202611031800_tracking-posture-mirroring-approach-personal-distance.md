# Tracking posture: mirroring, approach, personal distance

**Status:** Done

Implements the settled behaviour in `specs/motion/head_tracking.md` ("The posture", "Configuration and verbs", "The head tracking report"), `specs/core/config.md` ("`motion.tracking` → `TrackingSettings`", the `face_detection.width` default), `specs/vision/user_perception.md` ("The face report": `distance_m`; "The shipped detector": width 640, `box_height_m`), `specs/core/bridge.md` ("Attention / gaze": `set_tracking`, `tracking`), `specs/daemon/sim_displays.md` (`FACE_BOX_HEIGHT_M = 0.18`), `specs/testing/sim_scene.md` (life-size portraits), `specs/testing/testing_support.md` (`face_scene` restores the config's tracking settings) and `specs/examples/control_panel.md` (the posture checkboxes and readout), then the retuned speeds of `specs/motion/motion.md` "The gaze layer" (`GAZE_ALPHA = 0.15`, the weight and focus fades over `GAZE_FADE_S = 0.35`) and of the posture's filters (`ROLL_SMOOTHING_S = 0.3`, `OFFSET_SMOOTHING_S = 0.6`), after a first test with a person found the head slow to follow and slow to mirror. Delivers the three posture behaviours carried in the tracker's aim, the settings object as config and runtime API, the face's distance, and a gaze that keeps up with a person; leaves out a face position in the robot's frame (user_perception.md open question 2) and the box-height calibration on people (open question 9).

## Scope

- `src/reachy_mini_bridge/config.py` — `MirroringSettings`, `ApproachSettings`, `PersonalDistanceSettings`, `TrackingSettings` (frozen, `from_dict`); `MotionSettings.tracking` the object, a boolean a `ConfigError`; the cross-block rule on `tracking.enabled`; `face_detection.width` default 640
- `src/reachy_mini_bridge/face_detection.py` — `FACE_BOX_HEIGHT_M = 0.18`, `DETECT_WIDTH = 640`; `Face.distance_m`; `FaceDetection(camera=...)` and `report_from_pixels(camera=..., box_height_m=...)`; the detector's optional `box_height_m`
- `src/reachy_mini_bridge/yunet.py` — `box_height_m`
- `src/reachy_mini_bridge/head_tracking.py` — `face_camera_point` (shared with the marker), the posture (world-frame mirrored roll with a soft deadband, gain and clamp; the offset; the two first-order filters; freeze / carry-over / reset), `HeadTracker.start(settings)` / `configure(settings)`, `HeadTrackingReport.roll_deg` / `offset_mm`
- `src/reachy_mini_bridge/bridge.py` — the camera model built once and handed to the detection loop and the tracker; `tracking` → `TrackingSettings`; `set_tracking`; `start_head_tracking(focus=None)` / `stop_head_tracking` over it
- `src/reachy_mini_bridge/sim_displays.py` — the marker's depth through `face_camera_point`, `FACE_BOX_HEIGHT_M` from perception
- `src/reachy_mini_bridge/testing/sim_scene.py` — `DEFAULT_FACE_SIZE = (0.29, 0.36)`
- `src/reachy_mini_bridge/testing/fixtures.py` — `face_scene` resets the tracking settings to the config's after a test
- `src/reachy_mini_bridge/__init__.py` — export the settings classes
- `tests/test_config.py`, `tests/test_head_tracking.py`, `tests/test_face_detection.py`, `tests/test_bridge.py`, `tests/test_sim_displays.py` — functional tests below
- `tests-e2e/test_head_tracking.py` (and others touching `motion.tracking`) — the object config; a distance test and a mirroring test on the viewer sim
- `config.example.json`, `examples/configs/*.json`, `examples/greeter/`, `examples/control_panel/` — the object form; the panel's `set_posture`, its three checkboxes, the posture in the state table and each face's distance in the faces line
- `README.md`, `docs/reference/configuration.md`, `docs/reference/api.md`, `docs/guides/perception-and-tracking.md` — the object, the verbs, the distance
- `src/reachy_mini_bridge/motion.py` — the speeds: `GAZE_ALPHA` 0.12 → 0.15; `GAZE_FADE_S`, the gaze weight's and focus's fade, in place of `BLEND_S`
- `tests/test_head_tracking.py`, `tests/test_bridge.py` — the speeds: the posture tests' bounds derived from the constants; the offline convergence tests held to the live kit's thresholds (`OVERSHOOT_MAX_DEG`, `YAW_TOLERANCE_DEG`); the pipeline test reading the direction of the first clear turn; the detection-switch test waiting for the published report (a race the ARM runner hit)

## Steps

1. Config: the four frozen dataclasses with `from_dict` and bounds, `MotionSettings.tracking: TrackingSettings`, the boolean refused with a hint, the cross-block rule reading `tracking.enabled`, width 640.
2. Perception: `FACE_BOX_HEIGHT_M`, `DETECT_WIDTH = 640`, `Face.distance_m`, the camera model and box height threaded into `report_from_pixels`; `YuNetDetector.box_height_m`.
3. Tracker: `face_camera_point`; the posture computed in `_aim` against `frame_head_pose`; the filters stepped per aimed observation, frozen while missing, reset on loss and stop; the report's two fields published with `update`; `start(settings)` / `configure(settings)`.
4. Bridge: one camera model per session for the loop, the tracker and the markers; `tracking`, `set_tracking`, `start_head_tracking(focus=None)`, `stop_head_tracking`; the package exports.
5. The marker on `face_camera_point`; the life-size portraits.
6. Examples, profiles, `config.example.json`, docs.
7. Tests: fast tier (config shapes and bounds; distance from size and box height; mirrored roll settling, clamped, deadband, no feedback from the head's own roll; lean / retreat along the look direction; frozen while missing, gone after loss; focus keeps the posture; switching a behaviour off eases it out; `set_tracking` while running; the report's fields). Live: `distance_m` against a portrait's true distance; a rolled portrait near the robot → a mirrored roll of the right sign; the head's x leaning and retreating.
8. The speeds, once the Lite had been tried: measure where the time goes (the commanded head against the actual one; the daemon's raw step response, 0.23 s to 90% on the Lite, 0.6–0.9 s on the sim's servos); sweep `GAZE_ALPHA` × the fade length offline (settle time without delay, overshoot and swing-back under a 0.3 s unreported delay), taking 0.2 × 0.35 s at first and 0.15 once CI's live tier caught 0.2 hunting on its runner (10.6° past a face, 7.4° back); shorten the posture filters and re-measure the mirror.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`; the live tier headless (`uv run pytest tests-e2e -rs`) and on the viewer sim (`REACHY_MINI_E2E_SIM_VIEWER=1`), the head tracking and motion files also on `REACHY_MINI_E2E_KINEMATICS=placo`. Then the specs back to `Implemented` and this plan `Done`, in the files and the indexes. Measured after the speeds (viewer sim, commanded head): a 20° acquisition to 90% in 0.39–0.43 s (0.58 s before); a 25° tilt mirrored to 63% in 1.12 s (1.85 s); a 14° tilt in 2.07 s (2.47 s); live overshoot at most 3.8°.
