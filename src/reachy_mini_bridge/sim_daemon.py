"""The sim daemon launcher: every MuJoCo daemon the bridge starts (specs/sim_daemon.md).

``python -m reachy_mini_bridge.sim_daemon [--scene NAME] [--headless]
[--[no-]preload-datasets] [--camera sim|webcam] [--webcam-device D] [--webcam-hfov DEG]
[upstream flags...]`` runs upstream's daemon with three corrections that make daemon-side
face tracking work in the sim, and a choice of camera source:

1. **Tracking is stepped** on every control tick — upstream's MuJoCo loop never calls
   ``step_head_tracking()`` (only the real-robot loop does), so the head never follows.
2. **The tracker's intrinsics** are an ideal pinhole of the active camera. Upstream's
   ``intrinsics_for_size`` rescales ``MujocoCameraSpecs.K`` (a 1280x720 matrix) as if it
   were calibrated on the robot's 3840x2592 sensor, which puts the principal point near
   the frame's top-left corner and the head ~45° off the face.
3. **A webcam is a fixed camera**: with ``--camera webcam`` the aim is computed from the
   neutral head pose, not the present one, so a camera that does not turn with the head
   does not feed the head's own motion back into the aim.

``--camera webcam`` relays a host camera into the stream the MuJoCo daemon's media server
reads (RTP raw video on UDP 5005) instead of the eye-camera render, so the tracker and
every client see the person in front of the computer.

Importing this module pulls in neither ``mujoco`` nor GStreamer; the daemon-side pieces
import them when they run.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import math
import platform
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .config import CAMERA_SOURCES, DEFAULT_WEBCAM_HFOV_DEG

__all__ = [
    "CAMERA_SOURCES",
    "DEFAULT_WEBCAM_HFOV_DEG",
    "SimDaemonExtension",
    "WebcamRelay",
    "corrected_backend",
    "install_tracker_intrinsics",
    "pinhole_intrinsics",
    "run_sim_daemon",
    "sim_hfov_deg",
    "webcam_source",
]

_logger = logging.getLogger(__name__)

# The MJCF camera the MuJoCo daemon renders (upstream's CAMERA_REACHY) and the stream it
# renders into: upstream's render thread sends 1280x720 RGB as RTP raw video to this port,
# and the media server's sim source reads it (GStreamerUDPCamera / _build_sim_source).
EYE_CAMERA = "eye_camera"
STREAM_SIZE = (1280, 720)
STREAM_PORT = 5005
_STREAM_FPS = 25

# The webcam relay's watchdog: a source that delivers no frame for this long is a failure,
# and a failed source is retried this often.
_FRAME_TIMEOUT_S = 5.0
_RETRY_S = 5.0


# --- intrinsics (correction 2) ----------------------------------------------------------


def pinhole_intrinsics(hfov_deg: float, size: tuple[int, int]) -> np.ndarray:
    """The camera matrix of an ideal pinhole with horizontal field of view ``hfov_deg``
    at ``size`` = (width, height): square pixels, principal point at the frame centre."""
    width, height = size
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0.0, width / 2.0], [0.0, f, height / 2.0], [0.0, 0.0, 1.0]])


def sim_hfov_deg(fovy_deg: float, width: int, height: int) -> float:
    """The horizontal field of view of a MuJoCo camera (``fovy`` is vertical) rendered at
    ``width`` x ``height`` with square pixels."""
    half = math.atan(math.tan(math.radians(fovy_deg) / 2.0) * width / height)
    return math.degrees(2.0 * half)


class _TrackerCamera:
    """The active camera's horizontal field of view, as the tracker's intrinsics read it.

    Set by the launcher (``webcam``) or by the backend once its model exists (``sim``);
    read when the tracker thread computes its matrix — after both.
    """

    hfov_deg: float | None = None


def _tracker_intrinsics(
    K: np.ndarray, crop_scale: float, target_size: tuple[int, int]
) -> np.ndarray:
    hfov = _TrackerCamera.hfov_deg
    if hfov is None:  # not launched through this module: leave upstream's behaviour
        from reachy_mini.media import camera_utils

        return camera_utils.intrinsics_for_size(K, crop_scale, target_size)
    return pinhole_intrinsics(hfov, (int(target_size[0]), int(target_size[1])))


def install_tracker_intrinsics() -> None:
    """Replace the ``intrinsics_for_size`` the face tracker resolves (and only that one:
    ``reachy_mini.media.camera_utils`` and every other caller keep upstream's)."""
    from reachy_mini.vision import face_tracking

    face_tracking.intrinsics_for_size = _tracker_intrinsics  # type: ignore[assignment]


# --- the webcam relay (camera source "webcam") ------------------------------------------


def webcam_source(device: str | int | None, system: str | None = None) -> str:
    """The GStreamer source element for a host camera: the platform default when
    ``device`` is ``None``; a device index on macOS (``avfvideosrc``); a device path — or
    an index, as ``/dev/videoN`` — on Linux (``v4l2src``). ``ValueError`` otherwise."""
    system = platform.system() if system is None else system
    if device is None:
        return "autovideosrc"
    if system == "Darwin":
        if isinstance(device, int):
            return f"avfvideosrc device-index={device}"
        raise ValueError(
            f"a macOS webcam device is an index (0, 1, ...), got {device!r}"
        )
    if system == "Linux":
        path = f"/dev/video{device}" if isinstance(device, int) else device
        return f"v4l2src device={path}"
    raise ValueError(f"choosing a webcam device is not supported on {system}")


def relay_pipeline_description(source: str) -> str:
    """The relay: the camera at the sim stream's size, converted to what upstream's render
    thread sends (RGB, RTP raw video, payload 96) and sent where the media server reads."""
    width, height = STREAM_SIZE
    return (
        f"{source} ! video/x-raw,width={width},height={height} ! videoconvert ! "
        f"videorate ! video/x-raw,format=RGB,width={width},height={height},"
        f"framerate={_STREAM_FPS}/1 ! queue leaky=downstream max-size-buffers=2 ! "
        "rtpvrawpay mtu=1400 name=pay ! application/x-rtp,payload=96 ! "
        f"udpsink host=127.0.0.1 port={STREAM_PORT} sync=false"
    )


class _Pipeline(Protocol):
    """What the relay needs from a running capture pipeline (tests fake it)."""

    def start(self) -> bool: ...
    def error(self) -> str | None: ...
    def stop(self) -> None: ...


class _GstPipeline:
    """A ``Gst.parse_launch`` pipeline reporting each buffer that reaches the payloader."""

    def __init__(self, description: str, on_frame: Callable[[], None]) -> None:
        # gi ships inside the gstreamer wheel's own site-packages, invisible to pyright.
        import gi  # pyright: ignore[reportMissingImports]

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # pyright: ignore[reportMissingImports]

        Gst.init(None)
        self._gst: Any = Gst
        self._pipeline: Any = Gst.parse_launch(description)
        pay = self._pipeline.get_by_name("pay")

        def probe(_pad: Any, _info: Any) -> Any:
            on_frame()
            return Gst.PadProbeReturn.OK

        pay.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)
        self._bus: Any = self._pipeline.get_bus()

    def start(self) -> bool:
        result = self._pipeline.set_state(self._gst.State.PLAYING)
        return result != self._gst.StateChangeReturn.FAILURE

    def error(self) -> str | None:
        Gst = self._gst
        message = self._bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if message is None:
            return None
        if message.type == Gst.MessageType.EOS:
            return "the camera stream ended"
        err, _debug = message.parse_error()
        return str(err.message)

    def stop(self) -> None:
        self._pipeline.set_state(self._gst.State.NULL)


def _open_pipeline(description: str, on_frame: Callable[[], None]) -> _Pipeline:
    return _GstPipeline(description, on_frame)


class WebcamRelay:
    """Relays a host camera into the sim daemon's camera stream, in its own thread.

    A source that cannot start, reports an error, or delivers no frame for
    ``frame_timeout`` seconds is logged once (at ``ERROR``, with the macOS camera
    permission hint) and retried every ``retry`` seconds; a recovery is logged at
    ``INFO``. The daemon never stops on a camera problem."""

    def __init__(
        self,
        device: str | int | None = None,
        *,
        frame_timeout: float = _FRAME_TIMEOUT_S,
        retry: float = _RETRY_S,
        clock: Callable[[], float] = time.monotonic,
        open_pipeline: Callable[[str, Callable[[], None]], _Pipeline] | None = None,
        system: str | None = None,
    ) -> None:
        self._device = device
        self._system = platform.system() if system is None else system
        self._description = relay_pipeline_description(
            webcam_source(device, self._system)
        )
        self._frame_timeout = frame_timeout
        self._retry = retry
        self._clock = clock
        self._open = open_pipeline if open_pipeline is not None else _open_pipeline
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_frame = 0.0
        self._failing = False

    @property
    def description(self) -> str:
        return self._description

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="webcam-relay", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _on_frame(self) -> None:
        self._last_frame = self._clock()

    def _run(self) -> None:
        while not self._stop.is_set():
            problem = self._run_once()
            if self._stop.is_set():
                return
            self._report(problem)
            self._stop.wait(self._retry)

    def _run_once(self) -> str:
        """Run one pipeline until it fails (returns why) or the relay stops."""
        try:
            pipeline = self._open(self._description, self._on_frame)
        except Exception as e:  # noqa: BLE001 - a missing plugin, a bad device string
            return f"cannot build the capture pipeline: {e}"
        try:
            self._last_frame = self._clock()
            if not pipeline.start():
                return "the camera source did not start"
            seen_frame = False
            started = self._last_frame
            while not self._stop.wait(0.2):
                error = pipeline.error()
                if error is not None:
                    return error
                if self._last_frame > started and not seen_frame:
                    seen_frame = True
                    if self._failing:
                        _logger.info("webcam relay: frames flowing again")
                        self._failing = False
                if self._clock() - self._last_frame > self._frame_timeout:
                    return f"no frame for {self._frame_timeout:g} s"
            return ""
        finally:
            pipeline.stop()

    def _report(self, problem: str) -> None:
        if self._failing:
            return
        self._failing = True
        device = "the default camera" if self._device is None else repr(self._device)
        hint = (
            " — on macOS, the app that launched the daemon (your terminal or editor) "
            "needs Camera access in System Settings > Privacy & Security"
            if self._system == "Darwin"
            else ""
        )
        _logger.error(
            "webcam relay (%s): %s; retrying every %g s%s",
            device,
            problem,
            self._retry,
            hint,
        )


# --- the backend subclass (corrections 1 and 3, camera source wiring) --------------------


@dataclass(frozen=True)
class SimDaemonExtension:
    """Adds to the launched daemon: ``on_backend(backend)`` runs at the end of the
    backend's ``__init__`` (the model exists); ``on_app(app)`` on the FastAPI app
    upstream's ``create_app`` built."""

    on_backend: Callable[[Any], None] | None = None
    on_app: Callable[[Any], None] | None = None


@dataclass(frozen=True)
class _Camera:
    source: str = "sim"
    device: str | int | None = None
    hfov_deg: float = DEFAULT_WEBCAM_HFOV_DEG


def corrected_backend(
    backend_class: type,
    *,
    camera: _Camera | None = None,
    extensions: Sequence[SimDaemonExtension] = (),
    relay_factory: Callable[[str | int | None], Any] = WebcamRelay,
) -> type:
    """A subclass of upstream's ``MujocoBackend`` carrying the corrections.

    - ``update_head_kinematics_model`` — which the MuJoCo loop calls once per control
      tick, where the robot loop calls it — steps tracking right after it (correction 1).
    - ``__init__`` records the camera's field of view for the tracker's intrinsics
      (correction 2): the scene's eye camera for ``sim``, the configured one for
      ``webcam``; then runs each extension's ``on_backend``.
    - With a ``webcam`` camera: ``set_tracking_face`` computes the aim from the neutral
      head pose (correction 3) — every other reader of the head pose sees the real one;
      the eye-camera render thread does nothing; the webcam relay runs with the loop.
    """
    camera = _Camera() if camera is None else camera
    webcam = camera.source == "webcam"

    class CorrectedMujocoBackend(backend_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._aim_from_rest = threading.local()
            if webcam:
                _TrackerCamera.hfov_deg = camera.hfov_deg
            else:
                mujoco: Any = importlib.import_module("mujoco")
                cam_id = mujoco.mj_name2id(
                    self.model, mujoco.mjtObj.mjOBJ_CAMERA, EYE_CAMERA
                )
                _TrackerCamera.hfov_deg = sim_hfov_deg(
                    float(self.model.cam_fovy[cam_id]), *STREAM_SIZE
                )
            for extension in extensions:
                if extension.on_backend is not None:
                    extension.on_backend(self)

        def update_head_kinematics_model(self, *args: Any, **kwargs: Any) -> None:
            super().update_head_kinematics_model(*args, **kwargs)
            self.step_head_tracking()

        if webcam:

            def set_tracking_face(self, *args: Any, **kwargs: Any) -> None:
                self._aim_from_rest.active = True
                try:
                    super().set_tracking_face(*args, **kwargs)
                finally:
                    self._aim_from_rest.active = False

            def get_current_head_pose(self) -> Any:
                if getattr(self._aim_from_rest, "active", False):
                    return np.array(self.INIT_HEAD_POSE, dtype=np.float64)
                return super().get_current_head_pose()

            def rendering_loop(self, *args: Any, **kwargs: Any) -> None:
                return None  # the webcam relay feeds the camera stream

            def run(self) -> None:
                relay = relay_factory(camera.device)
                relay.start()
                try:
                    super().run()
                finally:
                    relay.stop()

    CorrectedMujocoBackend.__name__ = backend_class.__name__
    CorrectedMujocoBackend.__qualname__ = backend_class.__qualname__
    return CorrectedMujocoBackend


# --- the launcher -----------------------------------------------------------------------


def _device(value: str) -> str | int:
    return int(value) if value.isdigit() else value


def _hfov(value: str) -> float:
    try:
        hfov = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {value!r}") from None
    if not 1.0 < hfov < 179.0:
        raise argparse.ArgumentTypeError("must be strictly between 1 and 179 degrees")
    return hfov


def _parser(prog: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run the Reachy Mini MuJoCo daemon with the bridge's face-tracking "
        "corrections and camera source (specs/sim_daemon.md). Unrecognised flags go to "
        "upstream's daemon.",
    )
    parser.add_argument("--scene", help="an upstream scene name (empty, minimal)")
    parser.add_argument("--headless", action="store_true", help="no viewer window")
    parser.add_argument(
        "--preload-datasets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="pre-download the recorded-move datasets in the background",
    )
    parser.add_argument(
        "--camera",
        choices=CAMERA_SOURCES,
        default="sim",
        help="sim: the rendered eye camera (viewer only); webcam: a host camera",
    )
    parser.add_argument(
        "--webcam-device",
        type=_device,
        default=None,
        help="the capture device: a macOS index, a Linux path (default camera if unset)",
    )
    parser.add_argument(
        "--webcam-hfov",
        type=_hfov,
        default=None,
        help=f"the webcam's horizontal field of view in degrees "
        f"(default {DEFAULT_WEBCAM_HFOV_DEG:g})",
    )
    return parser


def run_sim_daemon(
    argv: Sequence[str] | None = None,
    *,
    extensions: Sequence[SimDaemonExtension] = (),
    prog: str = "python -m reachy_mini_bridge.sim_daemon",
) -> None:
    """Run upstream's MuJoCo daemon with the corrections and ``extensions`` installed.

    Upstream's ``main()`` parses ``sys.argv``; this rewrites it to ``--sim [--scene S]
    [--headless] --[no-]preload-datasets`` plus anything unrecognised, substitutes the
    backend class the daemon constructs, wraps ``create_app`` for the extensions'
    ``on_app``, installs the tracker's intrinsics, and calls ``main()``.
    """
    parser = _parser(prog)
    args, passthrough = parser.parse_known_args(argv)
    if args.camera != "webcam" and (
        args.webcam_device is not None or args.webcam_hfov is not None
    ):
        parser.error("--webcam-device / --webcam-hfov need --camera webcam")
    camera = _Camera(
        source=args.camera,
        device=args.webcam_device,
        hfov_deg=(
            args.webcam_hfov
            if args.webcam_hfov is not None
            else DEFAULT_WEBCAM_HFOV_DEG
        ),
    )
    if camera.source == "webcam":
        try:
            webcam_source(camera.device)
        except ValueError as e:
            parser.error(str(e))

    from reachy_mini.daemon import daemon as upstream_daemon
    from reachy_mini.daemon.app import main as upstream_main

    upstream_daemon.MujocoBackend = corrected_backend(
        upstream_daemon.MujocoBackend, camera=camera, extensions=extensions
    )
    original_create_app = upstream_main.create_app

    def create_app(*a: Any, **kw: Any) -> Any:
        app = original_create_app(*a, **kw)
        for extension in extensions:
            if extension.on_app is not None:
                extension.on_app(app)
        return app

    upstream_main.create_app = create_app
    install_tracker_intrinsics()
    sys.argv = [
        "reachy-mini-daemon",
        "--sim",
        *(["--scene", args.scene] if args.scene else []),
        *(["--headless"] if args.headless else []),
        "--preload-datasets" if args.preload_datasets else "--no-preload-datasets",
        *passthrough,
    ]
    _logger.info("sim daemon: camera %s", camera)
    upstream_main.main()


if __name__ == "__main__":  # pragma: no cover - the daemon-side entry point
    run_sim_daemon()
