# Fix runtime consistency R1–R5

**Status:** Done

Restores the existing contracts in [bridge.md](../specs/core/bridge.md) ("Cancellation", "Core concepts / Decided" — lifecycle and wobbling), [audio.md](../specs/audio/audio.md) ("TTS out" and "Sound files"), [motion.md](../specs/motion/motion.md) ("Emotions through the loop", "Motors", "Lifecycle"), and [user_perception.md](../specs/vision/user_perception.md) ("The face report", "The detection loop", "Lifecycle"). Fixes R1–R5 from the [repository consistency review](../analysis/20261005_repository-consistency-review.md) without changing the public API or weakening those contracts; R6, harness/CI findings, and documentation reorganization are separate work.

## Scope

- `src/reachy_mini_bridge/audio.py` — serialize adapter sink ownership through producer cleanup; retain thread-safe queue delivery and existing speech interruption semantics.
- `src/reachy_mini_bridge/motion.py` — stop the playing primary's sound and acknowledge every dropped primary when motor pause interrupts motion.
- `src/reachy_mini_bridge/bridge.py` — bind emotion sound callbacks to their media session; retain ownership of wobbling commands until they finish and drain them during teardown; allocate face track IDs for the bridge's lifetime.
- `src/reachy_mini_bridge/face_detection.py` — acquire and validate detectors safely under cancellation at startup and replacement; accept a bridge-owned track-ID allocator, retaining a local allocator for standalone use.
- `tests/test_audio.py` — deterministic interruption/cancellation regressions through the real `TTSEngineSynthesizer` and `TTSEngine`, with only the provider stubbed.
- `tests/test_motion.py` — motor-pause sound ownership and drop acknowledgment regressions.
- `tests/test_bridge.py` — public-API regressions for emotion pause/shutdown, cancelled detector construction, wobbling completion/teardown, and track IDs across full sessions.
- `tests/test_face_detection.py` — detector acquisition/disposal during cancellation and replacement; allocator behavior across restarts.
- This plan and [the plans index](_index.md) — progress and final verification record.

Use existing source and test modules. No dependency changes, new root directories, or new concept modules are needed. The specs already define the intended behavior, so this is an implementation repair. Preserve current spec maturity statuses; in particular, this plan does not complete the motion spec's outstanding hardware validation. If explanatory comments/docstrings change, make them describe the restored behavior without introducing a new contract.

## Steps

1. **Begin the repair and pin the failing transitions.**

   Mark this plan `In progress` here and in the index when implementation starts. Add each regression with its corresponding fix below, observing its failure on the current implementation first. Drive `ReachyMiniBridge` on `fake` for integration cases, using the lower-level session tests only where they isolate ownership and ordering.

   Use `asyncio.Event` or `threading.Event` to stop at the relevant boundary: provider cleanup, SDK mode changes, or detector construction. Put finite timeouts around waits and release test gates in `finally`, so a failed assertion cannot strand a thread. Poll real motion/camera observations only where their existing real-time loops require it; do not use a fixed sleep as proof that a factory/SDK call has begun.

   Assertions must measure output, owned resources, completion order, and continued usability. The fake's command trace is a record of effects delivered to the SDK seam, not a substituted mock-call assertion. For TTS, assert delivered frame counts and PCM contents with a recording wrapper around the fake's speaker method.

2. **R1 — Hold the adapter's sink binding until its producer has terminated.**

   Add an adapter-owned async lease/lock. Acquire it before binding `_QueueSink` and retain it for the entire stream, including the engine task's cancellation cleanup. A stream cancelled while waiting for the lease must never bind the sink or start its producer. The engine's existing speech lock is insufficient because adapter binding currently precedes it.

   Within that lease, create the invocation's queue and `say_task`. Preserve normal completion: drain queued chunks, then await the engine task so provider failures propagate. On early exit, cancel and await the producer before unbinding/releasing the lease; retrieve its exception while preserving the consumer's original cancellation/failure. Ensure an interrupted cleanup cannot release the binding while the producer is still able to feed or drain it. Keep queue scheduling bound to captured loop/queue references for each callback, so already-scheduled deliveries do not follow a later binding.

   This uses the installed `tts-engine` module contract: a provider's `stream()` returns/raises only after it has stopped invoking its callback, including on cancellation. Cancellation requests cooperative termination and waits for cleanup; it must not wait for the original utterance to synthesize naturally. Keep model construction outside the per-utterance path. Leave `MediaSession` responsible for newest-wins behavior, speaker flushing, and `SpeechInterruptedError`.

   Add a deterministic provider stub by patching the installed engine's module-loading seam, then construct the real adapter normally. Give successive utterances distinct PCM values. Hold the first provider inside cancellation cleanup while the replacement is requested. Verify:

   - The first bridge call raises `SpeechInterruptedError`; its queued output is flushed.
   - The replacement delivers all expected frames with the replacement's sample values. Neither an old PCM chunk nor an old drain sentinel ends or contaminates it.
   - Cancelling an active utterance stops its output, and a subsequent utterance through the same adapter works.
   - A third request that supersedes a replacement waiting for the lease does not bind or produce the abandoned replacement; the latest request plays.
   - A provider failure propagates and leaves the same adapter usable for the next utterance.

   Retain the existing matched-rate/resampling/channel-conversion and playback-completion tests. Use short, controlled cleanup to verify interruption remains prompt without timing it against the full utterance duration.

3. **R2 — Stop emotion sound on motor pause and preserve callbacks through shutdown.**

   In `MotionSession._on_pause()`, capture the playing primary, drop it through `_drop_playing()` before resolving its failure, clear queued primaries, and set every affected primary's `dropped` event after its effect is gone. Fail unfinished futures with the existing motor-pause `BridgeError`. Queued primaries must never stop a sound they did not start. Preserve paused target behavior and present-pose reanchoring on resume.

   In `ReachyMiniBridge.start()`, capture the newly opened `MediaSession` in the motion session's callbacks. Bind `start_sound` directly to that instance and use a small closure for stopping a valid `SoundToken` on it. Remove the bridge-state lookup in `_stop_emotion_sound()` when it is no longer needed. Keep the media session alive until after motion shutdown, as the exit-stack order already does. Public bridge fields can still be cleared at the beginning of teardown.

   For emotion wobbling restoration, skip restoration after the session has begun closing; R4 will supply the captured session state and ordered SDK command path. Do not attempt to re-enable wobbling through a cleared `bridge.robot` during teardown.

   Add regressions that:

   - Pause a sounding primary, with another queued, and assert sound cleanup has completed when the primary failure/drop acknowledgment is observed. Both primaries finish instead of waiting for the drop timeout; resume accepts another primary.
   - Through the public bridge, switch motors to `disabled` and `gravity_compensation` during a sounding emotion. Its task fails with `BridgeError`, its file is stopped, no further trajectory targets are sent while paused, and another emotion works after motors are enabled again.
   - Replace the emotion's sound with a caller's `play_sound`, then pause motion. The caller's sound continues and completes normally. Preserve concurrent speech as well; an emotion's stop must not flush a `say` it does not own.
   - Stop the bridge while an emotion is sounding. Its file stop precedes drop acknowledgment and media shutdown, teardown finishes without the missing-media/missing-robot warnings, and the same bridge can start another session.

   Retain the existing tests for cancellation before the sound starts, cancellation while queued, soundless emotions, token replacement, and natural completion. Natural completion still does not forcibly stop the sidecar sound.

4. **R3 — Give detector factory results an owner even if awaiting construction is cancelled.**

   Extract the synchronous part of detector disposal into a private helper: call optional `close()` under `_detector_lock`, with the same debug-and-ignore failure policy. Keep normal `_release()` running that helper off the event loop. The same disposal function must serve invalid objects, cancelled acquisitions, retired detectors, and normal shutdown, without closing an object twice.

   Add one acquisition helper in `FaceDetection` following the shielding and eventual-disposal discipline of `cancel_safe_step()` in `audio.py`. Retain ownership of each factory task independently of its awaiting caller. Validate the completed object with `_check_face_detector()` before installing it; dispose of invalid results before propagating `ValueError`. Transfer ownership to `_detector` only after successful acquisition/validation. Use this helper in both `start()` and `_poll()`'s replacement path.

   If an acquisition's caller is cancelled, mark that acquisition abandoned and retain a cleanup task that awaits its eventual result, closes it, and observes any failure. Runtime detection enable must propagate cancellation promptly rather than wait for a model factory to finish; the cleanup task owns the eventual result and cannot install it or publish an active report. Make `FaceDetection.stop()` drain outstanding acquisitions/disposals, as well as stop its polling task, before teardown releases its session resources. This also closes a replacement whose factory finishes after its polling task was cancelled.

   Preserve the documented startup exception: the bridge's exit-stack unwind may wait for a factory already running in a thread before cancellation finishes. Keep the existing second-cancellation escape during startup unwind; this plan does not strengthen that separate exception. Avoid an untracked fire-and-forget cleanup: disposal tasks stay owned until complete and their exceptions are retrieved. Disposal must continue to wait for any `detect()` already holding the lock. No detector is built merely by registration.

   Add tests for:

   - Cancel `bridge.start()` while its factory is blocked, release the factory, and assert the resulting detector is closed once, the bridge and reports are inactive, and a subsequent start detects faces normally.
   - Cancel runtime detection enable while construction is blocked; the discarded detector is closed and the already-running bridge remains usable with its previous mode requests intact.
   - Begin a custom-detector replacement, stop the loop/bridge while the replacement factory is blocked, then release it. Both the retired detector and the uninstalled replacement are closed exactly once.
   - Replacement returning an invalid object closes it rather than installing it; optional or raising `close()` retains existing behavior.

   Preserve factory-error handling, once-per-start construction, detection/close mutual exclusion, and the existing shutdown-during-detection test. Also exercise a cancelled runtime enable followed by a new enable before the old factory finishes: the abandoned result must be disposed of rather than overwrite the new detector. All event gates must be released on failure, because teardown correctly waits for owned workers before unwinding.

5. **R4 — Retain and order wobbling commands independently of their awaiting callers.**

   Introduce private session-owned wobbling state in `bridge.py`, created after robot connection and captured by its exit-stack callback. It holds the robot, the requested mode, ownership of commands already accepted, and whether cleanup has begun. Keep the public `wobbling` property as the bridge's record of its own requested mode, distinct from the temporary SDK disable during an emotion.

   Represent accepted SDK changes as owned tasks, serialized in acceptance order. Await them through `asyncio.shield()` in public setters: cancelling the caller returns promptly while an already-accepted instantaneous mode command still completes. Update the session's record when the SDK operation succeeds even if that caller has stopped awaiting it. Observe failures from orphaned callers, and do not let an older command's completion overwrite a later completed request.

   Register cleanup before the startup enable. On teardown, close command admission, drain all accepted commands, and disable any wobbling this session enabled or may have armed during a failed enable. Keep cleanup off the event loop. Completion of an old session's task must not update a new session's records or re-enable its robot after cleanup. Only reset/detach the session's state after this cleanup has run.

   Route the emotion's temporary disable and restoration through the same ordering mechanism, without changing its requested mode. Preserve one wobbling lease across consecutive emotions, caller changes made during an emotion, and restoration after an emotion's cancellation. Restoration after closing starts is skipped. A cancelled `set_wobbling(True)` is not itself a request to undo the enable: completion bookkeeping and session exit must remain correct.

   Add event-controlled tests that:

   - Cancel a runtime enable from an initially off mode while the SDK call is blocked. Cancellation returns before the worker is released; once released, the public record reflects completion, and exiting disables the mode.
   - Exit while that cancelled enable is still blocked. Shutdown waits for it, then disables in order, and no later completion re-enables the closed session.
   - Cancel startup during the configured enable. Release the worker and assert enable is followed by disable during unwind; another session starts cleanly.
   - Cancel a runtime disable and verify its eventual completion is reflected; exercise an enable followed by disable so the final state follows command order.
   - Preserve caller-requested off during an emotion, restoration after emotion cancellation, and one pause across queued emotions. Ensure a previous session has no surviving command that can affect the next one.

   Measure cancellation promptly with a held gate; measure cleanup completion only after releasing it. Assertions on command ordering must be accompanied by final state and successful later session/verb behavior.

6. **R5 — Allocate face track IDs at bridge lifetime.**

   Initialize a private next-ID counter once in `ReachyMiniBridge.__init__()` and provide a small allocation method returning increasing positive IDs. Pass that callable into each new `FaceDetection` created by the bridge. Do not reset it during `stop()`, failed startup, detection enable/disable, or custom factory replacement.

   Add an optional allocator argument to the internal `FaceDetection` constructor. For standalone use, default to its existing local monotonic allocator. Make every `_FaceTracks` instance created by startup/replacement use the selected allocator; only association state is reset. Keep track order, short-miss continuity, and client-created `Face(track_id=0)` behavior unchanged.

   Test one bridge across at least two complete sessions, presenting a face in each, and assert the second session's ID is greater than the first. Include detection off/on and a factory replacement to verify IDs continue increasing within a session. Verify a separately constructed bridge starts its own allocation at 1. Retain the existing standalone-loop restart test and camera frame-ID continuity; no camera lifetime changes are needed.

7. **Complete the gate and record the repaired contracts.**

   Run targeted tests while each fix is developed. Resolve all failures before moving to final verification. Confirm the final changes leave only the intended runtime/test files and plan progress in the diff, and inspect the source/spec mappings if scope changes unexpectedly. Run the complete gate below once the integrated repairs are ready; repeat it only if subsequent fixes warrant another run.

   Mark this plan `Done` here and in the index only after all steps and verification pass. Record actual results and any remaining limits. Leave unrelated plans and spec statuses alone; passing these fake regressions does not claim hardware validation or resolve the review's R6/V/D findings.

## Verification

The implementation must satisfy these behavioral checks, rather than merely complete without an exception:

| Finding | Required result |
|---|---|
| R1 | Replacement PCM reaches the speaker intact through the shipped adapter; old cleanup cannot terminate the new stream; cancellation/failure leave the adapter usable. |
| R2 | Pausing or closing a sounding emotion stops its owned file before the caller observes its drop; replacement files and concurrent speech survive; paused motion sends no targets and resumes correctly. |
| R3 | Every completed, discarded factory result is closed once when it has `close()`; construction cancellation and replacement shutdown leave no installed detector or active report, and the bridge can detect again. |
| R4 | A cancelled caller cannot lose an accepted SDK mode command; completion records are accurate, teardown ends with the session's wobbling disabled, and old commands cannot affect another session. |
| R5 | Track IDs increase across sessions/restarts/replacements of one bridge, while a different bridge owns an independent sequence. |

Targeted regressions:

```sh
uv run pytest tests/test_audio.py tests/test_motion.py tests/test_face_detection.py tests/test_bridge.py
```

If a transition assertion is timing-sensitive, run its selected test serially (`uv run pytest -n 0 <file>::<test>`) and fix its synchronization rather than loosening a timeout without evidence.

Final gate:

```sh
uv run ruff check .
uv run ruff format --check src tests tests-e2e examples
uv run pyright
uv run pytest
git diff --check
```

Format only the code directories when necessary (`uv run ruff format src tests tests-e2e examples`), preserving Markdown examples and plan text. These repairs can be established with deterministic tests and the installed engine under a stub provider; no live service, model download, daemon, or physical robot is required. The existing live suite continues to run in CI, with skips reported as skips rather than validation.

## Completion record — 2026-10-06

Implemented R1–R5 in the scoped modules. Regression tests reproduced the affected transitions before their repairs and now cover intact replacement PCM through the real TTS adapter, including repeated cancellation during cleanup; emotion sound ownership on motor pause and shutdown, including concurrent speech; eventual disposal of cancelled detector acquisitions; completion and teardown of cancelled wobbling commands; and increasing face IDs across sessions, detection restarts, and replacement. Existing public API and spec statuses are preserved.

Verification used the installed development environment (`UV_CACHE_DIR=/tmp/reachy-audit-uv-cache uv run --no-sync …`):

- Ruff lint: passed.
- Ruff format check: all 57 Python files in the code directories already formatted.
- Pyright: 0 errors, 0 warnings.
- Complete fast suite: **690 passed in 40.95 seconds**, on eight workers. No skips.
- `git diff --check`: passed.

The full fast suite includes all four targeted test modules. Its local HTTP-server tests required running outside the filesystem sandbox. The live/e2e tier and hardware validation were not run; the motion spec remains `Stable`. R6, harness/CI findings, and documentation reorganization remain separate work.

Follow-up verification, 2026-10-06, viewer sim (`REACHY_MINI_E2E_SIM_VIEWER=1 uv run pytest tests-e2e -rs`): **30 passed, 2 skipped in 3 min 24 s** — the two skips are the by-design ones (`gravity_compensation` needs hardware; a simulation ignores motor modes). The camera, perception and head-tracking files ran.
