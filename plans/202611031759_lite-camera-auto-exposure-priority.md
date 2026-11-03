# The Lite camera's auto-exposure priority on macOS

**Status:** Done

Implements the settled behavior in `specs/daemon/real_daemon.md` ("The camera exposure"). The real daemon launcher's wrapped media-server start sets the Lite camera's UVC auto-exposure priority to 1 over USB, after the index check, so the daemon's 1920×1080@60 stream is lit as the camera's own auto-exposure wants it instead of capped at a 1/60 s exposure. It leaves out a config field (an opt-out is deferred until asked for), the Linux and Windows paths (deferred: open question 3) and any change of the camera's mode.

## Scope

- `specs/daemon/real_daemon.md` — the purpose and the new "The camera exposure" section, "What it is not", the testable list, the relationships, open questions 3 and 4; `specs/_index.md` row
- `src/reachy_mini_bridge/real_daemon.py` — `LITE_CAMERA_USB_ID`, `CameraTerminal` / `camera_terminal` (the descriptor parse), `enable_ae_priority` (the decision over a `UvcControl`), the libusb glue (`_LibusbControl`, `_lite_camera`) and the exposure step run by `_checked_start` after the selection
- `tests/test_real_daemon.py` — the parse, the USB-id parity, the decision with a scripted control, the wiring in `_checked_start`
- `docs/internals/upstream-sdk-notes.md` — the measured facts of the Lite camera's 1080p60 mode and of the priority control
- `docs/guides/troubleshooting.md` — a "dark image" entry; `docs/guides/running-daemons.md` and `AGENTS.md` — the launcher's one-line descriptions naming both corrections

## Steps

1. **The parse.** `camera_terminal(descriptors: bytes) -> CameraTerminal | None` walks the class-specific descriptors (`bLength`, `bDescriptorType`) of a VideoControl interface and returns the camera input terminal's id and whether its `bmControls` has the auto-exposure priority bit (bit 2); `None` for no camera terminal or a zero-length block. `LITE_CAMERA_USB_ID = (0x38FB, 0x1002)`.
2. **The decision.** `enable_ae_priority(control, terminal) -> Literal["enabled", "already", "unsupported", "refused"]` over the `UvcControl` protocol (`get(selector, request, length) -> bytes`, `set(selector, data)`): the four outcomes and their log lines as the spec lists them; `OSError` (pyusb's `USBError` is one) is the refusal.
3. **The glue.** `_lite_camera()` finds the device by its ids through `usb.core.find` on `libusb_package.get_libusb1_backend()`, picks the VideoControl interface (class `0x0E`, subclass `0x01`), parses its `extra_descriptors`, and returns a `_LibusbControl` bound to the device, the interface number and the terminal id. The control sends the class requests through pyusb's backend directly (`ctrl_transfer` on the opened handle), never through `Device.ctrl_transfer`, which claims the interface macOS's driver holds; `close()` disposes the device's resources.
4. **The wiring.** `_checked_start` runs `_lite_ae_priority()` after `select_camera` on a macOS start whose camera path is an index: the finder's exceptions and a `None` are a warning or a `DEBUG` line, the control closed after the decision.
5. **Tests** as the spec's "Testable without a daemon" lists; the existing `_macos` helper gives `_checked_start` a finder returning `None`, so no test reaches the real USB bus.
6. **Docs**: the upstream note, the troubleshooting entry, the two one-liners.

## Verification

- `uv run pytest tests/test_real_daemon.py` and the full fast tier; `uv run ruff check .`, `uv run ruff format src tests tests-e2e examples`, `uv run pyright`.
- On the robot: the launcher started with `--no-wake-up-on-start --no-goto-sleep-on-stop` (no motion), its log showing the `INFO` line of the exposure step, and a frame read through the SDK client measurably brighter than upstream's daemon gives (a mean near 100 on 255 against near 25 in the same scene). **Run 2026-11-03** on the Lite over USB: the launcher ready in 2 s, its log carrying the `INFO` line; the SDK client's frames at a mean of 90 on 255 with the correction, 22 with the flag forced back to 0 from outside (upstream's state), 88 with it restored — the same stream, 10 fps throughout.

Fast tier, lint, format and type check pass (846 tests).
