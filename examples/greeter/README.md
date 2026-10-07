# The greeter

A demo over `ReachyMiniBridge`: the robot breathes until a face shows up, greets it while the head follows it, and says goodbye when it leaves.

| The robot | What the greeter does |
|---|---|
| sees nobody | breathes (the idle move); the head tracker is on and waits |
| sees a face | the head turns onto it; a second later the robot plays a welcoming emotion and says "Hello there", then keeps following |
| loses the face | after the tracker's two-second loss timeout, the robot says "Good bye" with a farewell move; the head eases back into breathing |

It is one loop over `bridge.head_tracking.changes()` ([app.py](app.py)), acting on the engaged / not-engaged edge: a switch from one person to another does not re-greet, a detector's blink does not say goodbye.

## Run it

```
uv run python -m examples.greeter                                          # the sim, seeing through your webcam
uv run python -m examples.greeter --config examples/greeter/configs/lite-usb.json   # a Reachy Mini Lite on USB
```

Ctrl-C stops it; a daemon the bridge started is stopped with it (the robot goes to sleep).

## Configs

The two profiles under [configs/](configs/) are the project's [examples/configs/](../configs/) ones with a voice added — the local pocket model (`tts-pocket`, in a plain `uv sync`; the weights download into the Hugging Face cache on first use):

| Config | Needs |
|---|---|
| [sim-webcam.json](configs/sim-webcam.json) (default) | the `sim` extra, a webcam with camera permission for the terminal, an unlocked GUI session for the viewer |
| [lite-usb.json](configs/lite-usb.json) | the robot on USB; camera and microphone permission for the terminal (macOS) |

Any `ReachyMiniConfig` works through `--config` as long as it names a `face_detection.detector`, sets `motion.tracking` on and carries a `tts` block ([docs/reference/configuration.md](../../docs/reference/configuration.md)) — the greeter refuses a config without tracking, and runs without a voice (logging what it could not say) when the voice fails to build.
