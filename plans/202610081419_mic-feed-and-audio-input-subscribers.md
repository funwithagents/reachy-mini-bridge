# Mic feed and `audio_input()` subscribers

**Status:** Done

Implements the settled behavior in `specs/audio/microphone.md` (the whole spec) and the edits it brought to `specs/audio/audio.md` ("One media session", "Mic in"), `specs/core/bridge.md` ("Audio in", "Front door", "Lifecycle"), `specs/core/robot.md` (the fake's paced capture, `get_audio_sample` consumed by the feed alone), `specs/testing/testing_support.md` and `specs/testing/testing.md` (the `audio` probe on the feed). Delivers one reader thread over `get_audio_sample()` with a 2 s ring, `audio_input()` as one subscriber of many with its own cursor, `preroll_s`, the lapped-subscriber warning, `bridge.mic` with `latest()` / `published_count`, and the fake's capture paced at 10 ms. Leaves out the spec's open questions: a strict-continuity mode surfacing gaps in the stream, the ring length as config, and measuring the wireless chunk size.

## Scope

- `src/reachy_mini_bridge/microphone.py` — **new**: `MicChunk`, `MicFeed` (reader thread, ring, `latest()`, `published_count`, `subscribe()`), the constants `MIC_RING_CHUNKS`, `MIC_EMPTY_S`, `MIC_RETRY_S`, `MIC_DOWN_S`
- `src/reachy_mini_bridge/audio.py` — `MediaSession` takes a `mic: MicFeed | None`, binds / starts it after `start_recording()` and pushes its `stop()` on the exit stack; `audio_input(mono, preroll_s)` delegates to `mic.subscribe`; `_tap` and `_MIC_POLL_INTERVAL_S` removed
- `src/reachy_mini_bridge/bridge.py` — `self._mic = MicFeed()` at construction, passed to each `MediaSession`; `mic` property; `audio_input(mono, preroll_s)`
- `src/reachy_mini_bridge/__init__.py` — export `MicChunk`
- `src/reachy_mini_bridge/fake_reachy_mini.py` — `get_audio_sample()` paced at one chunk per 10 ms (first immediate)
- `src/reachy_mini_bridge/testing/fixtures.py` — `_probe_audio(mic: MicFeed)` reads `mic.latest()`; `_probe_capabilities` / `probed_capabilities` take the feed; the call site passes `bridge.mic`
- `tests/test_microphone.py` — **new**: the feed's and subscribers' functional tests on the fake
- `tests/test_audio.py` — the mic tap tests moved onto the new path (`test_mic_tap_waits_out_missing_samples`, `test_mic_tap_does_not_busy_poll` rewritten against the feed's empty-read pacing)
- `tests/test_bridge.py` — `bridge.mic` across the lifecycle; two `audio_input()` subscribers through the bridge
- `tests-e2e/test_audio.py` — the format test reads `bridge.mic.latest().samples`, not `get_audio_sample()`; a new test with two concurrent subscribers
- `specs/audio/microphone.md` — frontmatter gains `src/reachy_mini_bridge/microphone.py` and `tests/test_microphone.py`; Status `Stable` → `Implemented`
- `specs/audio/audio.md`, `specs/core/bridge.md`, `specs/core/robot.md`, `specs/testing/testing_support.md`, `specs/testing/testing.md` — Status `Updated` → `Implemented`; `bridge.md` / `audio.md` frontmatter `code:` gains `microphone.py` where they govern it
- `specs/_index.md` — the six status cells
- `AGENTS.md` — a Project map row for `microphone.py`
- `docs/reference/api.md` — the Mic in row and paragraph (subscribers, `preroll_s`, the lapped warning), `bridge.mic` / `MicChunk` in the values and lifecycle sections
- `docs/guides/audio.md` — several consumers at once, pre-roll for a wake word
- `docs/internals/upstream-sdk-notes.md` — `get_audio_sample()` is a one-shot pull (200-buffer appsink, `drop=True`, 20 ms wait, `None` when empty): two readers split the capture
- `README.md` — the Audio input row says any number of listeners

## Steps

1. **The fake's paced capture.** In `_FakeMedia`, keep `_last_chunk_at: float | None`; `get_audio_sample()` returns at once on the first call, and later sleeps until `_last_chunk_at + _CHUNK_FRAMES / _SAMPLE_RATE` before returning the zero chunk — the same shape as the fake's `get_frame` pacing. Reset the clock in `start_recording()`.

2. **`microphone.py`.** Module docstring in the voice of `camera.py`'s. Then:
   - `MicChunk` — frozen dataclass `seq: int`, `ts: float`, `samples: npt.NDArray[np.float32] = field(compare=False)`.
   - `type SampleReader = Callable[[], npt.NDArray[np.float32] | None]`.
   - `MicFeed.__init__`: `_lock = threading.Lock()`, `_ring: list[MicChunk | None] = [None] * MIC_RING_CHUNKS`, `_head = 0` (persists across sessions), `_session = 0` (incremented at each `start()`), `_running = False`, `_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]`, `_reader`, `_channels`, `_thread`, `_stop = threading.Event()`.
   - `bind(read_sample, channels)` — stores both; `BridgeError` while running.
   - `start()` — `BridgeError` when unbound or running; clears the ring slots, `_session += 1`, `_running = True`, clears `_stop`, spawns the daemon thread `reachy-mini-bridge-mic`.
   - The thread's loop: `t0 = time.monotonic()`; read; on raise, the camera's down / retry / recover logging with the `MIC_*` constants; on `None`, `self._stop.wait(max(0.0, MIC_EMPTY_S - (time.monotonic() - t0)))`; on a chunk, `_publish(np.asarray(sample, dtype=np.float32))`.
   - `_publish(samples)` — under the lock: `chunk = MicChunk(self._head, time.monotonic(), samples)`, `self._ring[self._head % N] = chunk`, `self._head += 1`, snapshot `_waiters`; outside it, `loop.call_soon_threadsafe(event.set)` for each, swallowing `RuntimeError` from a closed loop.
   - `stop()` — no-op when not running; sets `_stop`, `await asyncio.to_thread(thread.join)`, then under the lock `_running = False` and the ring cleared; wakes every waiter (so subscribers see the session ended).
   - `latest()` — under the lock, `None` when not running or `_head` is 0 in this session, else `_ring[(_head - 1) % N]` (check its `seq == _head - 1`).
   - `published_count` — `_head`.
   - `subscribe(*, mono, preroll_s)` — `ValueError` on `preroll_s < 0`, `BridgeError` when not running; computes the start cursor **at the call** (under the lock): `head`, or for `preroll_s > 0` the smallest `seq` in `[max(head - N, session_start_seq), head)` whose chunk `ts >= now - preroll_s` (keep `_session_start_seq` set at `start()` so pre-roll never reaches the previous session); returns `self._iterate(cursor, session, mono)`.
   - `_iterate(cursor, session, mono)` — an async generator: creates its `asyncio.Event`, registers `(loop, event)`; in `try`, loops: `event.clear()`; under the lock read `running`, `_session`, `head` and, when `cursor < head`, the slot; return when not running or the session changed; when `cursor < head - N`, log the gap warning (format in the spec) and set `cursor = head - N`, re-read; when `cursor == head`, `await event.wait()` and continue; otherwise check `slot.seq == cursor` (a mismatch is a lap: treat as above), `cursor += 1`, convert outside the lock (`downmix_to_mono` when `mono and channels > 1`, then `float32_to_int16(...).tobytes()`) and yield. `finally:` unregister.
   - The conversions stay in `audio.py` (their public import path), and `microphone.py` imports them from there. `audio.py` imports `MicFeed` under `TYPE_CHECKING` only and constructs its default inside `MediaSession.__init__` with a local import, which breaks the cycle.

3. **`MediaSession`.** `__init__(robot, *, audio_config=None, mic=None)` stores `mic or MicFeed()`. In `start()`, right after `start_recording` and its `stop_recording` callback: `self._mic.bind(media.get_audio_sample, media.get_input_channels())`, `await self._mic.start()`, `stack.push_async_callback(self._mic.stop)`. `audio_input(*, mono=True, preroll_s=0.0)` keeps `_require_open`, then `return self._mic.subscribe(mono=mono, preroll_s=preroll_s)`. Remove `_tap` and `_MIC_POLL_INTERVAL_S`. Update the class docstring ("the mic subscribers").

4. **The bridge.** `self._mic = MicFeed()` beside `self._camera`; `MediaSession(robot, audio_config=..., mic=self._mic)`; a `mic` property with a docstring mirroring `camera`'s; `audio_input(self, *, mono=True, preroll_s=0.0)` passes both through. Export `MicChunk` from `__init__.py` (and `__all__`).

5. **The harness probe.** `_probe_audio(mic: MicFeed) -> bool` waits up to `_AUDIO_PROBE_TIMEOUT` for `mic.latest()` to be non-`None` (polling at 0.1 s); `_probe_capabilities(robot, address, camera, mic)` and `probed_capabilities(...)` gain the `mic` parameter (`None` ⇒ no `audio`); the `live_bridge` call site passes `bridge.mic`. Update the comment block above the camera probe to name both feeds.

6. **Fast tests** — `tests/test_microphone.py`, on the fake (through `MediaSession` or `ReachyMiniBridge("fake")`), driving the public surface:
   - two concurrent subscribers each take 30 chunks; both receive the same 30 consecutive chunks when started together with `preroll_s` covering the same instant (compare by injecting a `get_audio_sample` that returns chunks carrying their index as sample values, then decode the int16);
   - a mono and a raw subscriber side by side: same chunk count, the raw one `channels`× longer, the mono one the downmix of it;
   - a lapped subscriber: monkeypatch `microphone.MIC_RING_CHUNKS` to a small value (e.g. 8), let a subscriber sleep past it while another drains; the slow one logs exactly one gap warning and resumes on the oldest buffered index, the fast one has no gap;
   - `preroll_s=0.05` yields chunks whose index predates the call; `preroll_s` beyond the ring starts at the oldest chunk; `preroll_s=-1` raises `ValueError` at the call;
   - cancelling one subscriber's task mid-wait leaves the other streaming (it keeps receiving consecutive indexes after the cancel) and `audio_input()` still works;
   - every subscriber ends when the session closes; a subscriber made in session 1 does not yield in session 2 of the same bridge;
   - `bridge.mic.latest()` `None` before entry and after exit, a `MicChunk` during; `published_count` grows about 100 a second (lower bound with slack) and keeps counting across sessions;
   - a reader that raises then recovers: chunks resume, `latest()` advances;
   - an empty reader (`None` at once, forever) is read at most ~`1 / MIC_EMPTY_S` times a second (upper bound only).
   Move or rewrite the three mic-tap tests in `tests/test_audio.py` that relied on `_tap`'s polling; keep `test_mic_tap_ends_when_the_session_closes` (it still holds) and the `audio_input` raised-at-call tests. In `tests/test_bridge.py`, add `bridge.mic` to the outside-a-session reads and one two-subscriber test through the bridge.

7. **Live tests** — `tests-e2e/test_audio.py`: the format test reads `bridge.mic.latest()` (polling up to 5 s) and inspects `.samples` — never `get_audio_sample()`. Add `test_two_mic_subscribers_each_receive_the_whole_capture`: two `audio_input()` iterators drained concurrently for 1 s through `live_bridge.run`; each count ≥ 90 % of `bridge.mic.published_count`'s delta over the same window.

8. **Docs and statuses.** The doc edits listed in Scope, the `microphone.py` row in `AGENTS.md`, the frontmatter additions, then the six statuses to `Implemented` (here, the plan to `Done`, both index files).

## Verification

- `uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`.
- `uv run pytest` — the new `tests/test_microphone.py`, the updated `tests/test_audio.py` / `tests/test_bridge.py`, `tests/test_project_map.py` (the new module in the map and in a spec's `code:`) and `tests/test_docs_consistency.py` (statuses, links). Rerun `tests/test_microphone.py` with `-n 0` once to check the timing bounds hold serially too.
- `uv run pytest tests-e2e -rs` on the headless sim: `test_audio.py` passes with `audio` probed through the feed; the control panel's mic meter still reads a level (`uv run python -m examples.control_panel --config examples/configs/sim-rendered-camera.json`, speak near the mic).
- Mark this plan `Done` here and in [_index.md](_index.md) only once all pass.
