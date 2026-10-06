# The wobbling lease over every request, bring-up cleanup past repeated cancels, IPv4 loopback only

**Status:** Todo

Restores three narrower contracts: [motion.md](../specs/motion/motion.md) "Emotions through the loop" (wobbling paused from the first emotion's start to the last's end, whatever is requested meanwhile), [bridge.md](../specs/core/bridge.md) "Lifecycle" (a cancelled bring-up leaks nothing), and [config.md](../specs/core/config.md) (the loopback hosts a managed daemon accepts). Fixes R4, R5 and R6 of the [repository consistency review of 2026-10-06](../analysis/20261006_repository-consistency-review.md). R6 is decided as *IPv4 only*: upstream's SDK client builds `ws://{host}:{port}/ws/sdk` from the raw host, so `::1` can never connect — the bridge rejects it rather than half-supporting it.

## Scope

- `src/reachy_mini_bridge/bridge.py` — `_WobblingSession._apply` sends the *effective* state: the request (recorded when `record`) and no lease held; a request during an emotion is recorded and the pause kept, the release restoring it.
- `src/reachy_mini_bridge/audio.py` — `cancel_safe_step`: once the first cancel is caught, the finish-and-undo runs as its own task that the helper awaits shielded, absorbing further cancels until it has completed; the docstring's "abandons the step" sentence goes.
- `src/reachy_mini_bridge/config.py` — `LOOPBACK_HOSTS` loses `::1`; the `ConfigError` names the two accepted hosts.
- `src/reachy_mini_bridge/daemon.py` — `status_url` drops the IPv6 bracketing (no caller can pass an IPv6 host any more); `_port_open` unchanged (IPv4).
- `specs/motion/motion.md` — "Emotions through the loop", the wobbling bullet: a `set_wobbling(True)` during the pause is recorded and takes effect when the last emotion ends; `False` is recorded and nothing is sent. Status stays `Stable` (the spec is not `Implemented`; this is its settled design, clarified).
- `specs/core/bridge.md` — "Lifecycle": the bring-up-cancel sentence states that a repeated cancel is absorbed until the step in flight has finished and been undone (the promise "nothing is leaked" is what holds); "Cancellation" point 1 gains the cross-reference to that bring-up exception so the two sections agree. Status `Implemented` → `Updated` while this plan is open (shared with the R1/R2 plan; whichever closes last flips it back).
- `specs/core/config.md` — the loopback sentence: `127.0.0.1` / `localhost` only, with the reason. Status `Implemented` → `Updated` while this plan is open.
- `tests/test_bridge.py` — `set_wobbling(True)` during an emotion sends no enable until the emotion ends (fake command trace), including an emotion entered with wobbling off; `set_wobbling(False)` during an emotion stays off after it.
- `tests/test_audio.py` — `cancel_safe_step` cancelled twice while `enter` is held still runs `undo` once `enter` returns, and raises `CancelledError` once.
- `tests/test_config.py`, `tests/test_daemon.py` — `::1` is a `ConfigError` on a managed daemon; the `status_url` test uses an IPv4 host.
- `README.md` — the loopback sentence under the `robot` block (`127.0.0.1` / `localhost`).
- This plan and [the plans index](_index.md).

## Steps

1. **Mark this plan `In progress`** here and in the index; set `bridge.md` and `config.md` to `Updated` with the spec edits below, in the same change as the code.

2. **R4 — the lease governs every request.** In `_WobblingSession._apply`, compute `requested = self.enabled if enabled is None else enabled`, record it first when `record` is set, and send `effective = requested and self.leases == 0` to the SDK (`enable_wobbling` when true, `disable_wobbling` otherwise). `_may_be_enabled` follows `effective`. The emotion's pause (`submit(False, record=False)`) and release (`submit(None, record=False)`) keep working unchanged: the release reads the record, which a mid-emotion `set_wobbling` updated. A `set_wobbling(True)` during the pause therefore sends a (redundant) disable and the release sends the enable; a `set_wobbling(False)` sends a disable and the release sends another. Spec bullet in `motion.md`: add "a `set_wobbling` during the pause changes the record and nothing on the robot until the last emotion ends: `True` takes effect at the release, `False` keeps the robot still after it".

3. **R5 — cleanup owned past repeated cancels.** In `cancel_safe_step`, on the first `CancelledError` create `cleanup = asyncio.create_task(_finish_and_undo(step, undo))` — the existing body: await the step, log and re-raise a failing step as the cancel's cause, otherwise `await asyncio.to_thread(undo, result)` — and `await asyncio.shield(cleanup)` in a loop that catches `CancelledError` and keeps awaiting until `cleanup.done()`; then re-raise the original cancel (chaining a failed step as today). Docstring: a repeated cancel is absorbed until the step has been undone. Spec `bridge.md` "Lifecycle": after "nothing is leaked, at the cost of the cancel taking as long as that step", add "— a second cancel in that window does not shorten it"; "Cancellation" point 1: "(bring-up excepted: see Lifecycle — the cancel waits for the step in flight)".

4. **R6 — IPv4 loopback only.** `LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})`; the error message reads "`robot.host` must be a loopback address for a managed daemon (`127.0.0.1` or `localhost`; the SDK's client does not form IPv6 URLs), got …". `status_url` builds `http://{host}:{port}/api/daemon/status` plainly. Spec `config.md`: the loopback sentence lists the two hosts and the reason. README: the same two hosts.

5. **Tests, each observed failing first** where the old code lets them:
   - `test_bridge.py`: with wobbling on, hold the fake's emotion (its real 0.3 s), call `set_wobbling(True)` mid-flight; the command trace between the emotion's `disable_wobbling` and its end contains no `enable_wobbling`, and one follows the end. The variant entered with wobbling off (`config.motion.wobbling=False`): `set_wobbling(True)` during the emotion sends nothing but a disable, the enable follows the end, `bridge.wobbling` reads true throughout. `set_wobbling(False)` during an emotion: no enable after the end.
   - `test_audio.py`: `enter` held on a `threading.Event`; cancel the awaiting task twice (the second after the first cancel is caught, observed through a flag in a patched `undo` or by awaiting a loop tick); release; the task ends with `CancelledError` and `undo` ran once with the step's result.
   - `test_config.py`: `::1` on a spawning config raises `ConfigError` naming the accepted hosts; `test_daemon.py`: the `status_url` assertion on `127.0.0.1`.

6. **Flip the statuses** once the gate passes: this plan `Done`, `config.md` back to `Implemented`, `bridge.md` back to `Implemented` if the R1/R2 plan is `Done` too, index rows in sync.

## Verification

`uv run ruff check .`, `uv run ruff format --check src tests tests-e2e examples`, `uv run pyright`, `uv run pytest`. The existing wobbling and bring-up cancellation tests (`test_a_cancel_caught_inside_the_wobbling_pause_still_restores_it`, `test_cancel_during_bring_up_exits_the_robot`, the `cancel_safe_step` pair) keep passing.
