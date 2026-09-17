# Clean exit: a detached daemon, and a motion loop that pauses on a lost connection

**Status:** Done

Implements [specs/daemon.md](../specs/daemon.md) "The child runs in its own session" (and the orphan trade-off under "Teardown stops what the bridge started") and [specs/motion.md](../specs/motion.md) "Lifecycle" (the lost-connection bullet). Delivers a Ctrl+C that no longer floods the console and no longer leaves the robot wherever a killed daemon dropped it: the bridge-spawned daemon runs in its own session so a terminal `SIGINT` reaches the bridge process only, and the api's ordered teardown then runs against a live daemon (motion eases to neutral, tracking and wobbling are switched off, media and the client close, and only then is the daemon terminated and given its grace to put the robot to sleep). As defence in depth, a motion loop whose `set_target` hits a lost connection logs once and pauses instead of logging sixty lines a second until the app exits. Deliberately leaves out a parent-death watchdog for the orphaned-daemon case (documented, deferred), rate-limiting of *non*-connection tick faults (deferred), and any change to the downstream app.

Origin: the handoff note `specs/_clean_exit.md` (diagnosis from `reachy-mini-interaction-wica`), folded into the two specs above and deleted by this plan.

## Decisions taken here (the note's open questions)

1. **Orphan mitigation: document-only.** A detached daemon survives a `SIGKILL`ed or crashed bridge process and a closed terminal. `daemon.spawn: "auto"` borrows it on the next run (harmless; never stopped by the bridge), `"always"` raises `DaemonError` "port … already in use"; `pkill -f reachy-mini-daemon` stops it. A Linux-only `PR_SET_PDEATHSIG` or a `getppid()`-polling wrapper is recorded as a deferral in `daemon.md`'s open questions — neither works on macOS without a wrapper process, and the orphan is benign.
2. **Non-connection tick faults keep today's per-tick contract** (log, fail the primary, keep ticking). Rate-limiting them is recorded as a deferral in `motion.md`'s open questions; no such flood has been observed.
3. **Whether the daemon's `SIGINT` path already parked the robot** is not answerable offline; it is an on-robot check below. Either way the end state after this plan is the bridge's ease-to-neutral followed by the daemon's `SIGTERM` sleep.
4. **`websockets` becomes a declared dependency.** The loop catches `websockets.exceptions.ConnectionClosed` (upstream raises it on the send that races the close; it is *not* a `ConnectionError`), so the bridge imports `websockets` directly. It is already installed through `reachy-mini` (`websockets<16,>=12`); declaring it keeps the import honest.

## Scope

- `src/reachy_mini_bridge/daemon.py` — `_spawn` passes `start_new_session=True` (own session ⇒ own process group) and `stdin=DEVNULL` (a detached child must not hold the terminal).
- `tests/test_daemon.py` — two tests of the real `_spawn` seam: the child's session and process group differ from the test's; a `SIGINT` to the spawner's process group leaves the child alive (a subprocess harness in its own session). Both skip on Windows.
- `src/reachy_mini_bridge/motion.py` — a lost-connection state: `_LOST_CONNECTION_ERRORS`, `_on_lost_connection`; `_on_submit` / `_on_resume` honour it.
- `tests/test_motion.py` — lost-connection tests against the fake (both exception types).
- `pyproject.toml`, `uv.lock` — `websockets` declared.
- `specs/daemon.md` (`Implemented` → `Updated` → `Implemented`), `specs/motion.md` (stays `Stable`: it is not yet `Implemented`, gated by plan [202609162000](202609162000_motion-loop-presence-and-breathing.md)), `specs/_index.md`, `plans/_index.md`, this file.
- `specs/_clean_exit.md` — deleted once folded in.

## Steps

### Step 0 — Baseline

`uv run ruff check . && uv run ruff format src tests tests-e2e && uv run pyright && uv run pytest` — green (272 passed at the start of this plan).

### Step 1 — Specs first

Fold the note into the specs (done before any code, per AGENTS.md):

- `specs/daemon.md`: new subsection "The child runs in its own session" after "The child's environment is scrubbed"; the orphan trade-off and how to stop one under "Teardown stops what the bridge started"; the seam test under "Testable without a daemon"; open question 2 (parent-death watchdog, deferred). Status → `Updated`.
- `specs/motion.md`: "Lifecycle" — the last bullet distinguishes a lost connection from a bad tick; open question 4 (rate-limiting non-connection faults, deferred). Status stays `Stable`.
- `specs/_index.md` rows for both.

### Step 2 — Detach the daemon (`daemon.py`)

In `_spawn`:

```python
def _spawn(cmd: list[str], env: dict[str, str]) -> _Process:
    return subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
```

with a comment stating why (a terminal's Ctrl+C is a `SIGINT` to the whole foreground process group; the daemon must die *last*, from `_stop`, after the robot session closed against it).

**Tests** (`tests/test_daemon.py`, both `@pytest.mark.skipif(sys.platform == "win32")`):

- `test_spawn_puts_the_child_in_its_own_session`: `daemon._spawn([sys.executable, "-c", "import time; time.sleep(30)"], scrubbed_env())`; assert `os.getsid(proc.pid) != os.getsid(0)` and `os.getpgid(proc.pid) != os.getpgid(0)`; `proc.terminate(); proc.wait()`.
- `test_terminal_sigint_does_not_reach_the_child`: run a Python harness with `subprocess.run([...], start_new_session=True, capture_output=True)` so its `killpg` cannot hit pytest. The harness spawns a sleeper through `daemon._spawn`, ignores `SIGINT` itself, sends `os.killpg(os.getpgrp(), SIGINT)`, waits briefly, prints `child.poll()` and terminates the child. Assert the printed poll is `None` (the child survived). Without `start_new_session=True` in `_spawn`, the child is in the harness's group and dies of the `SIGINT` (a non-`None` poll).

### Step 3 — Pause on a lost connection (`motion.py`)

- Module level: `import websockets.exceptions` and

  ```python
  # What upstream raises when the daemon is gone (specs/motion.md "Lifecycle"): the
  # builtin ConnectionError from ws_client.send_command once its receive loop noticed
  # the close, and websockets' ConnectionClosed (not a ConnectionError) on the send
  # that races the close.
  _LOST_CONNECTION_ERRORS = (ConnectionError, websockets.exceptions.ConnectionClosed)
  ```

- `MotionSession.__init__`: `self._lost = False`.
- `_run`: catch `_LOST_CONNECTION_ERRORS` before the generic `except Exception`, calling `self._on_lost_connection(e)`.
- `_on_lost_connection(error)`: log **once** at `WARNING` (`"motion loop paused: lost connection to the daemon: %s"`), set `_lost`, pause (`_paused = True`, `_commanding = False`), drop `_playing`, fail the in-flight primary and every queued one with `BridgeError("the motion loop lost its connection to the daemon")` whose `__cause__` is the upstream error.
- `_on_submit`: when `_lost`, fail the new primary's future with the same `BridgeError` at once (never un-pause).
- `_on_resume`: when `_lost`, stay paused.
- `_on_close` needs no change: a paused loop sets `_stop` at once, no exit blend into a dead socket. `_on_reanchor` is already a no-op while paused.

**Tests** (`tests/test_motion.py`, parametrised over `ConnectionError("Lost connection with the server.")` and `websockets.exceptions.ConnectionClosedError(None, None)`), driving a `MotionSession` on a `FakeReachyMini` whose `set_target` is monkeypatched to raise after the primary's trajectory has started:

- the in-flight primary's future fails with `BridgeError` whose `__cause__` is the injected error;
- the target count does not grow afterwards (nothing more is sent), and `resume()` does not restart the stream;
- a primary submitted afterwards fails with `BridgeError` promptly rather than hanging;
- `caplog` holds exactly one `WARNING` from `reachy_mini_bridge.motion` mentioning the lost connection;
- leaving the `async with` returns promptly (no exit blend).

### Step 4 — Declare `websockets`

`pyproject.toml` dependencies: `"websockets>=12"` with a one-line comment (the loop catches its `ConnectionClosed`). `uv lock` / `uv sync --dev` to refresh `uv.lock` (no new package: it is already resolved through `reachy-mini`).

### Step 5 — Live tier, statuses, cleanup

- `uv run pytest tests-e2e -rs` on the headless sim: the harness now spawns the daemon through the detached `_spawn`, and the module teardown must still stop it (`pgrep -f reachy-mini-daemon` finds nothing after the run).
- Delete `specs/_clean_exit.md`. Flip this plan to `Done` (here and in `_index.md`) and `specs/daemon.md` back to `Implemented` (file and index).

## Verification

- `uv run ruff check . && uv run ruff format src tests tests-e2e && uv run pyright && uv run pytest` — all green.
- `uv run pytest tests-e2e -rs` on the headless sim — no failures; no daemon left behind.

### Outcome (2026-09-17, Apple Silicon Mac, macOS 26.5.2)

- Fast tier: 276 passed (272 before this plan + 2 daemon seam tests + the lost-connection test × 2 exception types). Lint, format and pyright clean.
- Headless sim: 11 passed, 3 skipped (`camera`, `gravity_compensation`, "a simulation ignores motor modes"); the harness spawned the daemon through the detached seam and `pgrep -f reachy-mini-daemon` found nothing afterwards.
- Both new tests were checked against the *old* code and fail there: with a plain `Popen` the group `SIGINT` kills the spawned child (exit by signal 2), and with the lost-connection catch removed the motion test sees one warning per tick and a raw `ConnectionError` on the primary.
- One deviation from step 2 as first written: the SIGINT harness must install a Python-level `SIGINT` handler, not `SIG_IGN` — an ignored disposition is inherited across `exec`, which shielded the child whatever the seam did and made the test pass on the old code. Recorded in the test's comment.
- The on-robot checks above remain to be walked on the Lite; the mechanism (a separate session; a paused loop) is pinned by the fast tier.
- **On the robot** (not runnable here; walk it when the Lite is plugged in):
  - [ ] In `reachy-mini-interaction-wica` with `daemon.spawn: "auto"` and no daemon running: start the app, say something so the voice plays (wobbler active), press Ctrl+C mid-sentence. Expect no `motion tick failed`, no `head_wobbler` traceback, no GStreamer EOS; the head eases to neutral, then the robot goes to sleep; the process exits; `pgrep -f reachy-mini-daemon` finds nothing.
  - [ ] Orphan: start the app, `kill -9` it, confirm the daemon is still running (the documented trade-off), start the app again and confirm `auto` borrows it; `pkill -f reachy-mini-daemon` afterwards.
  - [ ] Lost connection: with the app running, stop the daemon from another terminal (or unplug USB); expect a single `motion loop paused: lost connection …` warning, not a flood, and the app to exit normally on Ctrl+C.
