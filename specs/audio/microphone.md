---
code:
  - src/reachy_mini_bridge/microphone.py
  - src/reachy_mini_bridge/audio.py
  - src/reachy_mini_bridge/bridge.py
  - src/reachy_mini_bridge/fake_reachy_mini.py
  - src/reachy_mini_bridge/testing/fixtures.py
tests:
  - tests/test_microphone.py
  - tests/test_audio.py
  - tests/test_bridge.py
  - tests-e2e/test_audio.py
---

# Mic feed — the one reader of the robot's microphone (`microphone.py`)

**Status:** Implemented

## Purpose

The bridge's audio input feed: **one reader** of the robot's echo-cancelled microphone, running for the whole media session, keeping the last couple of seconds of capture in a ring, and serving **any number of subscribers** — each one an `audio_input()` iterator receiving every chunk, in order, at its own pace: a speech recognizer, a wake-word detector, a direction-of-arrival estimator on the raw channels, the control panel's level meter, a recorder. `bridge.mic` is that feed. It is the audio counterpart of the camera feed ([camera.md](../vision/camera.md)), with one difference the medium imposes: a video consumer wants the *newest* frame and loses nothing by skipping one, an audio consumer wants *every* chunk, so the feed keeps a history and each subscriber reads through it with a cursor of its own.

Upstream hands capture out one chunk at a time: the client's audio pipeline ends in a GStreamer `appsink` holding up to 200 buffers (`drop=True`, the oldest dropped when full), and `media.get_audio_sample()` pulls one — each 10 ms chunk returned once, then the next, then `None` after a wait of up to 20 ms when nothing is queued ([../docs/internals/upstream-sdk-notes.md](../../docs/internals/upstream-sdk-notes.md) "Perception"). Two readers in one process therefore split the capture between them, silently: each gets about every other chunk, and both streams are full of 10 ms holes — inaudible in a log, fatal to a recognizer. The feed owns the pull, so nothing else in the process calls `get_audio_sample()`.

## Core concepts / Decided

### The chunk

```python
@dataclass(frozen=True)
class MicChunk:
    seq: int                              # 0, 1, 2, … per feed — the subscribers' cursor counts these
    ts: float                             # time.monotonic() when the reader received the chunk
    samples: npt.NDArray[np.float32]      # (frames, mic_channels), interleaved float32 — the array upstream returned
```

- **`seq`** increases by one per published chunk, from 0, and keeps counting across sessions of the same bridge object (the feed is per bridge, like `camera`). `MicFeed.published_count` is the number of chunks published, ever — the next `seq`.
- **`ts`** is the chunk's arrival time on the monotonic clock. The capture's own timestamps are not read (the daemon's media pipeline runs on the audio device's clock, [camera.md](../vision/camera.md) "The frame's time and the head pose"); arrival time is what pre-roll needs.
- **`samples`** is the array upstream returned — no copy, no conversion. Chunks are shared by reference between every subscriber, so **a consumer never writes into `samples`**, as for a camera frame's `image`.

### The feed

```
MicFeed()
  bind(read_sample, channels, rate)  # the media session's: its get_audio_sample and capture format
  async .start() / async .stop()     # the reader thread; the media session's to call (below)
  .latest() -> MicChunk | None       # the newest chunk, or None before the first / after the session
  .published_count -> int            # chunks published, ever, on this feed
  .subscribe(*, mono, preroll_s)     # the iterator behind audio_input() (below)
```

- **One thread, one reader.** `start()` spawns the reader thread, which loops `read_sample()` — `robot.media.get_audio_sample`, the only call site of it in the bridge — and, for every chunk it gets back, stamps it (`seq`, `ts`) and publishes it into the ring. A `None` is one more pass, paced to `MIC_EMPTY_S = 0.01` s (one chunk): a read that waited at least that long inside the call (upstream waits up to 20 ms) runs again at once, a read that returned sooner waits out the difference on the stop event, so an empty feed never spins. A read that raises is logged at `DEBUG`, retried after `MIC_RETRY_S = 0.1` s, and a read that keeps raising for `MIC_DOWN_S = 5` s logs one `WARNING` (and one `INFO` when chunks return) — the camera feed's rules. `stop()` sets the stop event, joins the thread off the event loop (`asyncio.to_thread`), ends every subscriber and resets `latest()` to `None`; a `stop()` on a feed that is not running is a no-op; the thread is a daemon thread.
- **The ring.** The feed keeps the last `MIC_RING_CHUNKS = 200` chunks — 2 s at upstream's 10 ms chunks, about 256 KB of stereo float32 — in a fixed array: chunk `seq` lives in slot `seq % MIC_RING_CHUNKS` until chunk `seq + MIC_RING_CHUNKS` overwrites it. Publishing writes the slot and advances `head` (the next `seq`) together under the feed's lock, then wakes the waiting subscribers; the lock is held for the write only, never during a read of the robot, and the reader never waits on a subscriber.
- **Bound per session, alive per bridge.** `bridge.mic` is available from construction, with `latest()` `None` and no subscriber served until a session opens. The media session ([audio.md](audio.md)) binds the feed to its robot (`get_audio_sample`, with `get_input_channels()` and `get_input_audio_samplerate()` read once — the rate sizes a reported gap in milliseconds) and starts it right after `start_recording()`, and stops it right before `stop_recording()` — the feed's stop is pushed on the session's exit stack, so a failed open and a close unwind it like any other step. The feed is the session's, not the caller's: a consumer subscribes to it and never starts or stops it.

### Subscribers — `audio_input()`

`audio_input(mono=True, preroll_s=0.0)` returns a new subscriber: an async iterator over the ring, holding one integer, its **cursor** — the `seq` of the next chunk it yields. Every call is an independent subscriber; any number run at once, each seeing the whole capture from its start.

- **Reading.** Each step compares the cursor with the feed's `head`:

  | Cursor | Meaning | The step |
  |---|---|---|
  | `cursor == head` | caught up | waits until the reader publishes the next chunk |
  | `head - MIC_RING_CHUNKS <= cursor < head` | behind, its chunk still in the ring | reads slot `cursor % MIC_RING_CHUNKS`, advances the cursor, yields the chunk converted |
  | `cursor < head - MIC_RING_CHUNKS` | lapped: its chunks were overwritten | logs the gap, moves the cursor to the oldest chunk in the ring, and reads on |

  The head and the slot are read together under the feed's lock, the same lock the reader writes them under, so a subscriber never yields a chunk other than the one it asked for. A subscriber behind by a few chunks catches up without waiting, chunk after chunk; a caught-up one is woken by the reader within a chunk's time.
- **Conversion is the subscriber's.** The ring holds the raw capture; each subscriber applies its own conversion on its own step: `mono=True` downmixes with `downmix_to_mono` (when `mic_channels > 1`) and every subscriber converts with `float32_to_int16` — the same int16-LE contract as ever ([audio.md](audio.md) "Mic in"). A mono recognizer and a raw-stereo direction estimator run side by side over the same chunks.
- **A slow subscriber costs only itself.** No subscriber can slow the reader or another subscriber: one that falls more than the ring behind loses the chunks it was lapped by. The gap is **reported, never silent**: one `WARNING` per lap, naming the chunks lost and their duration (`audio_input: a subscriber fell 37 chunks (370 ms) behind the mic; resuming from the oldest buffered chunk`), and the subscriber resumes from the oldest chunk the ring holds. A consumer that keeps up — every ASR client streaming over the network does, the conversions cost microseconds — never sees one.
- **Where a subscriber starts.** By default at `head` — the capture from the call on. `preroll_s > 0` starts it in the past: at the oldest chunk in the ring whose `ts` is at least `now - preroll_s`, or at the oldest chunk the ring holds when the request reaches further back than 2 s — so a recognizer started by a wake-word detector hears the sentence that woke it. A negative `preroll_s` raises `ValueError` at the call.
- **The session's checks, at the call.** `audio_input()` outside an open session raises `BridgeError` where it is called, not at the first `async for` ([audio.md](audio.md) "One media session"). A subscriber ends on its own when the session closes — the feed's `stop()` wakes it and its iterator returns — so a consumer task still draining the mic finishes cleanly at teardown. A subscriber never carries over into a later session: it remembers the session it was made in and ends when that session ends.
- **Cancellation.** `audio_input` is a spanning verb ([bridge.md](../core/bridge.md) "Cancellation"): `break`, a cancel of the consuming task, or the iterator's `aclose()` ends that subscriber — it unregisters from the feed's wake-ups in a `finally` — and every other subscriber, the reader and the session go on untouched.
- **Waking across the thread.** A waiting subscriber holds an `asyncio.Event` registered with the feed together with its event loop; the reader, after publishing, sets every registered event through `loop.call_soon_threadsafe`. A subscriber clears its event before reading `head`, so a chunk published between the read and the wait is never missed.

### `latest()`, for samplers

`bridge.mic.latest()` is the newest chunk, read in an instant from any thread — the shape of `bridge.camera.latest()`, for a consumer that samples rather than streams: a level meter, an agent tool asking whether anyone is speaking, the live harness's `audio` probe ([testing_support.md](../testing/testing_support.md)). It takes nothing from any subscriber.

### `fake` backend support

`FakeReachyMini.media.get_audio_sample()` ([robot.md](../core/robot.md)) is paced like the real backend's capture: the first call returns a chunk at once, and each later call returns the next `(160, 2)` zero chunk once 10 ms have elapsed since the previous one, blocking until then on the feed's thread — so the feed publishes 100 chunks a second, as on the robot, and a subscriber's timing in `tests/` is real. `tests/` observe on the fake: two subscribers both receiving every `seq`, in order, with no gap; a mono and a raw subscriber side by side over the same chunks; a subscriber stalled past the ring reporting one gap and resuming at the oldest chunk while a fast one beside it loses nothing; `preroll_s` yielding chunks published before the call; a cancelled subscriber leaving the others streaming; every subscriber ending when the session closes; `latest()` `None` before entry and after exit; a `get_audio_sample` that raises leaving the feed recovering when chunks return.

## Relationship to the other specs

- **[audio.md](audio.md):** the media session binds, starts and stops the feed; `audio_input()` is the feed's subscriber; the int16-LE contract and the conversion helpers.
- **[bridge.md](../core/bridge.md):** `bridge.mic`; `audio_input(mono, preroll_s)`; `MicChunk` exported from the front door.
- **[camera.md](../vision/camera.md):** the same one-reader rule over upstream's other one-shot pull; latest value there, a ring with cursors here.
- **[robot.md](../core/robot.md):** `media.get_audio_sample` is consumed by the feed alone; the fake's paced capture.
- **[testing_support.md](../testing/testing_support.md):** the `audio` probe reads the feed's `latest()`, never `get_audio_sample()` beside it.
- **[control_panel.md](../examples/control_panel.md):** the mic meter is one subscriber among any others.

## Open questions

1. **Strict continuity.** A consumer that must know about every lost chunk in its own code — a recorder writing a file — reads the gap only from the log today. An opt-in that surfaces it in the stream (`audio_input(on_gap="raise")`, raising a `BridgeError` subclass at the gap, or a gap marker) is cheap to add once such a consumer exists.
2. **Ring length as config.** 2 s covers a wake-word pre-roll and any consumer that keeps up; a longer pre-roll (a "what was just said" tool) would want `audio.mic_ring_s` in the config. Deferred until one does.
3. **Chunk size on other transports.** The ring counts chunks, sized for upstream's 10 ms blocks on the local IPC path; the wireless robot's WebRTC client delivers chunks whose size is to be measured, which changes the ring's span in seconds, not its rules.
