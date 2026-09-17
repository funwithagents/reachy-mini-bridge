# Webcam relay on any camera, and the daemon's output in the bridge's log

**Status:** Done

Implements the settled behavior in `specs/sim_daemon.md` ("Camera sources", "Correction 2") and `specs/daemon.md` ("The child's output reaches the bridge's log"): the `webcam` relay takes whatever resolution a camera offers and centre-crops it into the sim's 1280×720 stream, the tracker's field of view follows the crop, and the spawned daemon's own log lines reach the person running the bridge instead of `/dev/null`.

## Why

The relay asked the camera for 1280×720 (`{source} ! video/x-raw,width=1280,height=720 ! …`). A camera that does not offer that exact mode cannot negotiate, so the pipeline never reaches `PLAYING` and the camera is never opened. Measured on a Mac whose camera offers only 3840×2592, 3840×2160, 3264×2448 and 1920×1080: `Internal data stream error.`, an `ERROR` every 5 s, no frames ever. `specs/sim_daemon.md` open question 3 deferred this until such a camera was in use; it now is.

The failure was invisible because the child's stdout/stderr go to `DEVNULL` — the relay's `ERROR` line, which names the cause and the macOS permission hint, reached nobody. That is `specs/daemon.md` open question 1, deferred "until the discard-plus-`DaemonError` path proves insufficient in practice". It has.

## Measurements behind the design

All on the 3840×2592 camera, pipeline ending in the real `rtpvrawpay ! udpsink`:

| Pipeline | Result |
|---|---|
| `{source} ! video/x-raw,width=1280,height=720 ! …` (today) | `Internal data stream error.`, 0 frames |
| `videoscale add-borders=true`, output size pinned | 131 frames, but **PAR 5/6** — it stretches and flags non-square pixels rather than padding; every consumer here ignores PAR, so the picture is stretched and the geometry silently wrong |
| same, `pixel-aspect-ratio=1/1` also pinned | no border columns in the frame — still stretched |
| `videobox` (manual borders, and `autocrop=true`) | `Internal data stream error.` both ways |
| `aspectratiocrop aspect-ratio=16/9 ! videoscale` | **works**; a `tee` comparing the same frame against both references: corr 0.968 vs a centre-crop-then-scale, 0.671 vs a full-frame stretch — it genuinely crops |
| `videoconvert ! aspectratiocrop ! videoscale` | 0.50 cores |
| `aspectratiocrop ! videoscale ! videoconvert` | **0.34 cores** — convert last, at the smallest size |

Borders are therefore not available without dynamic pipeline surgery (computing `videobox` offsets from negotiated caps at runtime, on an element that would not negotiate at all here), so the frame is a centre crop. Steering the camera to a cheaper capture mode is also unavailable: an ordered caps preference list and a bounded range were both ignored (still 3840×2592), and probing would mean opening the camera twice.

## Scope

- `src/reachy_mini_bridge/sim_daemon.py` — the relay pipeline crops and scales instead of demanding a size; `cropped_hfov_deg`; the relay reports its negotiated source size; webcam intrinsics follow it
- `src/reachy_mini_bridge/daemon.py` — the child's merged output is read by a thread and re-emitted through the bridge's logger; its last lines ride along on a startup `DaemonError`
- `tests/test_sim_daemon.py` — the pipeline shape, the cropped field of view, the size report, intrinsics end to end
- `tests/test_daemon.py` — output forwarding levels, and a failed launch carrying the daemon's own words
- `specs/sim_daemon.md` — "Camera sources" format/field-of-view, "Correction 2", open question 3 retired (`Implemented` → `Updated` → `Implemented`)
- `specs/daemon.md` — new "The child's output reaches the bridge's log", open question 1 retired (`Implemented` → `Updated` → `Implemented`)

## Steps

1. **The pipeline.** `relay_pipeline_description` emits `{source} name=camera ! <source caps> ! aspectratiocrop aspect-ratio=W/H ! videoscale ! videoconvert ! videorate ! video/x-raw,format=RGB,width=1280,height=720,framerate=25/1 ! …` with the ratio reduced from `STREAM_SIZE`; the name is what lets the relay read back which camera and mode it got. `relay_pipeline_candidates` gives two of them, tried in order by the relay: the stream's size asked of the camera outright (a 720p-capable camera then behaves exactly as before this plan — crop and scale are no-ops at that size), then a bare `video/x-raw` that constrains only the memory the frames live in (a macOS camera offers GPU memory first, which `aspectratiocrop` cannot take — with an explicit `device-index` the pipeline does not even link). Falling back is ordinary and logs nothing.

   Two pipelines rather than one ordered caps filter because negotiation ignores preference: an ordered caps list and a bounded range both left a 3840×2592 camera on 3840×2592. And a single unconstrained pipeline is *not* improvement-only — measured, the MacBook camera then negotiates 1080×1920 portrait, which the 16:9 crop cuts to 607 of its 1920 rows, where asking for 1280×720 gets it natively.

2. **`cropped_hfov_deg(hfov_deg, source_size, frame_size=STREAM_SIZE)`.** The frame's horizontal field of view once a source is centre-cropped to the frame's aspect: the crop keeps the fraction `r = min(1, frame_aspect / source_aspect)` of the source's width, so `2·atan(tan(hfov/2)·r)`. `r` is 1 for every source at or narrower than the frame's aspect — 16:9, 4:3, 3:2, the 3840×2592 camera — so only a wider-than-16:9 camera changes anything.

3. **The relay reports its source size.** `_Pipeline` gains `source_size()`, `_GstPipeline` implementing it from the named source pad's negotiated caps. `WebcamRelay` takes `on_source_size`, called once per pipeline run when the first frame arrives and the size is readable.

4. **Webcam intrinsics follow it.** `corrected_backend`'s webcam `run()` builds the relay with an `on_source_size` that sets `_TrackerCamera.hfov_deg = cropped_hfov_deg(camera.hfov_deg, size)` and logs the source size, the crop and the resulting field of view at `INFO`. Until the first frame — and if the caps cannot be read — the configured `hfov` stands, as it does today.

5. **The child's output.** `_spawn` gives the child `stdout=PIPE, stderr=STDOUT` (stdin stays `DEVNULL`); `_Process` gains `stdout`. `_ChildOutput` is a reader thread that re-emits each line through `reachy_mini_bridge.daemon.child`: a line carrying `- WARNING -` / `- ERROR -` / `- CRITICAL -` (upstream's daemon log format) at that level, everything else at `DEBUG`; it keeps the last lines in a ring buffer. `managed_daemon` starts it after the spawn and stops it after `_stop`, and `_wait_until_ready`'s `DaemonError`s append the buffered lines. A `None` stdout (the tests' scripted process) forwards nothing.

## Verification

- `tests/test_sim_daemon.py`: the description crops and scales rather than constraining the source; `cropped_hfov_deg` on 16:9 / 4:3 / 3840×2592 (unchanged) and on an ultrawide source (narrowed, to the value the formula gives); the relay calls back with the size its pipeline negotiated; through `corrected_backend` with a fake relay, the tracker's matrix after the report is the pinhole of the cropped field of view — the whole path, not the formula twice.
- `tests/test_daemon.py`: an upstream `ERROR` line is re-emitted at `ERROR` and an `INFO` line at `DEBUG`; a daemon that exits during startup raises a `DaemonError` quoting its own last lines.
- Live: `python -m reachy_mini_bridge.sim_daemon --headless --camera webcam` on the 3840×2592 camera — frames flow, and `get_camera_frame()` returns a 1280×720 frame with image content.
- `uv run ruff check .`, `uv run ruff format .`, `uv run pyright`, `uv run pytest`.

Mark this plan `Done` (here and in [_index.md](_index.md)) only once all pass, and flip both specs back to `Implemented`.
