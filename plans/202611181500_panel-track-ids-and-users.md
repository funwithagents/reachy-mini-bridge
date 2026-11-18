# Control panel: track ids on the camera, the users present, the tracking events

**Status:** Done

Implements `specs/examples/control_panel.md` ("The controller": `draw_faces`, `users_view()`, the tracking events; "The UI": the camera's labels, the Users section). Makes the track ids and whom the head follows visible in the panel — to debug the head losing or switching people during a fast emotion — and leaves the panel's controls unchanged (no pin control).

## Scope

- `examples/control_panel/controller.py` — `FaceView.track_ids`; `draw_faces(..., labels=, mirror=)` with a 3×5 bitmap font; `UsersView` + `users_view()`; the tracking-events task (`users` and `head_tracking` subscribed with `every=True`), `tracking_events()`, `describe_tracking_change()`
- `examples/control_panel/app.py` — the camera tick passes labels and mirrors through `draw_faces`; the faces line names each face's `#id`; the Users section (head line, users table, events log) on the camera timer, `refresh_users` endpoint
- `tests/test_control_panel.py` — labels drawn upright on a mirrored frame; the users view and events through a stub face's arrival, engagement, and `stopped` departure; `describe_tracking_change` per transition; the faces line's ids
- `specs/examples/control_panel.md` — the design above

## Steps

1. `FaceView` gains `track_ids` read off the same report as the positions.
2. `draw_faces`: boxes as today, then the flip when `mirror`, then each label at the (mirrored) box's top centre, a black plate under green glyphs, scale `max(1, height // 80)`, below the box when it would leave the top of the frame, clipped at the edges.
3. `UsersView` / `users_view()` from one read of `bridge.users.value` and `bridge.head_tracking.value`.
4. `_tracking_events` task started with the meters: two subscriptions run concurrently, each appending lines to a `deque(maxlen=TRACKING_EVENTS)`; `describe_tracking_change(previous, report)` turns two head-tracking reports into lines.
5. App: `refresh_camera` uses `draw_faces(..., labels=..., mirror=MIRROR_CAMERA)`; `faces_line` takes track ids; `users_markdown(view)` and `refresh_users`; the Users row.

## Verification

`uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` — then mark this plan `Done` and the spec `Implemented`.
