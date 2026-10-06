# Pace the camera feed's empty reads

**Status:** Done

Implements the feed's reading rule in [camera.md](../specs/vision/camera.md) ("The feed"). Fixes R3 of the [repository consistency review of 2026-10-06](../analysis/20261006_repository-consistency-review.md): a reader that returns `None` at once no longer spins the feed's thread. Normal throughput and a prompt `stop()` are kept; the SDK's behaviour on an absent camera is upstream's.

## Scope

- `src/reachy_mini_bridge/camera.py` — the `_run` loop times every read; an empty read that came back sooner than `CAMERA_EMPTY_S` waits out the difference on the stop event before the next pass.
- `specs/vision/camera.md` — "The feed": the empty-read rule as paced, and the sentence claiming the backend always waits inside the call replaced. Status `Implemented` → `Updated` while this plan is open.
- `tests/test_camera.py` — a reader returning `None` at once is called a bounded number of times per second; a reader that blocks is not slowed; `stop()` returns promptly during the pace wait.
- This plan and [the plans index](_index.md).

## Steps

1. **Mark this plan `In progress`** here and in the index; set `camera.md` to `Updated` in the same change as the code.

2. **Pace the empty read.** Add `CAMERA_EMPTY_S = 0.02` beside `CAMERA_RETRY_S` (a module constant tests may shorten): the floor an empty pass costs, one real frame period of the 20 ms the SDK's own read waits — so a blocking reader that returns `None` after its own wait pays nothing extra. In `_run`, note `time.monotonic()` before the read; on `got is None`, compute `elapsed`, and when it is under `CAMERA_EMPTY_S` call `self._stop.wait(CAMERA_EMPTY_S - elapsed)` before continuing. No log: an absent camera is upstream's warning, and the feed's `latest()` reading `None` is the signal the harness probes.

3. **Spec.** In "The feed", replace the parenthesis "(the real backend already waits up to 20 ms inside the call, so an empty feed costs a few wake-ups a second and no CPU)" with the rule: an empty read is paced to `CAMERA_EMPTY_S = 0.02` s — a read that waited at least that long inside the call (upstream's GStreamer reader waits up to 20 ms for a sample) runs again at once, a read that returned sooner (upstream's `get_frame()` returns `None` immediately, with a warning, when the client was built without a camera) waits out the difference on the stop event — so an empty feed costs at most fifty passes a second whatever the reader does.

4. **Tests** (`tests/test_camera.py`, on a `CameraFeed` bound to a plain callable; each observed failing first where it can):
   - a reader that returns `None` at once, run for 0.2 s, is called at most ~15 times (bounded by `0.2 / CAMERA_EMPTY_S` with slack), against the millions of the unpaced loop;
   - a reader that blocks 25 ms and returns `None` is called at its own rate (no added wait: ~8 calls in 0.2 s, not fewer);
   - `stop()` on a feed whose reader returns `None` at once returns within a fraction of a second (the pace wait is on the stop event).

5. **Flip the statuses** once the gate passes: this plan `Done`, `camera.md` back to `Implemented`, index rows in sync.

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`. The fake's paced frames (`FAKE_FRAME_HZ`) are unaffected: its read blocks a frame period, longer than the pace.
