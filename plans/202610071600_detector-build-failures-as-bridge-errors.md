# A detector factory's failure is a `BridgeError` whatever it raises

**Status:** Done

Implements the settled behaviour of [user_perception.md](../specs/vision/user_perception.md) "Building the detector": a factory that raises fails the start with a `BridgeError` chaining the cause, and only a build that *returns* something the loop cannot run — or a detector the loop cannot name — is the caller's `ValueError`. Closes the one implementation finding left in the [consistency review of 2026-10-07](../analysis/20261007_project-consistency-and-documentation.md): a factory raising `ValueError` of its own reached the caller raw, because the bridge told the two apart by exception type alone. No spec changes status.

## Scope

- `src/reachy_mini_bridge/face_detection.py` — `_acquire` wraps whatever the factory raises in a `BridgeError` naming the detector and chaining the cause, at the one place that knows the factory from the validation; `start()`'s docstring states it.
- `src/reachy_mini_bridge/bridge.py` — `_start_detection` deleted: `start()` and `_sync_detection` call `detection.start()` directly, the loop's own errors already being the contract's.
- `tests/test_bridge.py` — `test_a_factory_raising_value_error_is_a_bridge_error_with_that_cause`.
- `tests/test_face_detection.py` — `test_a_factory_that_raises_fails_the_start_and_starts_nothing` asserts the `BridgeError` and its cause.
- This plan and [the plans index](_index.md).

## Steps

1. **Wrap at the source.** The bridge re-raised every `ValueError` from `FaceDetection.start()` as the caller's and wrapped the rest, so a factory's own `ValueError` passed for a validation failure. `_acquire` is where the factory runs and where its result is checked, so it is where the two are told apart: the factory's exception becomes `BridgeError("the face detector 'custom' could not be built: ValueError: …")` with the cause chained, the validation's `ValueError` stays as it is. The bridge's wrapper goes.
2. **Tests.** On the public verb: a registered factory raising `ValueError` makes `set_face_detection(True)` raise `BridgeError` with that `ValueError` as its cause and the switch off, and the session goes on. On the loop: the existing factory-raises test reads the `BridgeError` and its `OSError` cause. The three cases the review asked to distinguish are then pinned: a factory raising `ValueError`, a factory raising something else, a factory returning an object without `detect` (`test_a_factory_result_without_detect_fails_the_start_and_is_released`).

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest` — 750 passed on 2026-10-07; the new bridge test fails on the previous `bridge.py` / `face_detection.py` with the raw `ValueError`.
