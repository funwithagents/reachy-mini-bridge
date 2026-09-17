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
every client see the person in front of the computer. The capture pipeline constrains
nothing about the source — a camera offers the modes it has — and centre-crops whatever it
negotiates into the stream's 1280x720; the tracker's field of view follows the crop.

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
    "cropped_hfov_deg",
    "install_tracker_intrinsics",
    "pinhole_intrinsics",
    "relay_pipeline_candidates",
    "run_sim_daemon",
    "sim_hfov_deg",
    "webcam_source",
]

_logger = logging.getLogger(__name__)

# The MJCF camera the MuJoCo daemon renders (upstream's CAMERA_REACHY) and the stream it
# renders into: upstream's render thread sends 1280x720 RGB as RTP raw video to this port,
# and the media server's sim source reads it (GStreamerUDPCamera / _build_sim_source).
EYE_CAMERA = "eye_camera"
# The capture pipeline's source element, named so the relay can read back which of its own
# modes the camera negotiated.
_SOURCE_NAME = "camera"
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


def cropped_hfov_deg(
    hfov_deg: float,
    source_size: tuple[int, int],
    frame_size: tuple[int, int] = STREAM_SIZE,
) -> float:
    """The horizontal field of view of the frame a camera of ``source_size`` fills once it
    is centre-cropped to ``frame_size``'s aspect.

    The crop keeps the fraction ``min(1, frame_aspect / source_aspect)`` of the source's
    width: all of it for every camera at or narrower than the frame (16:9, 3:2, 4:3, the
    3840x2592 sensors), so only a wider one sees its field of view narrowed.
    """
    source_width, source_height = source_size
    frame_width, frame_height = frame_size
    kept = min(1.0, (frame_width * source_height) / (frame_height * source_width))
    return math.degrees(2.0 * math.atan(math.tan(math.radians(hfov_deg) / 2.0) * kept))


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


def relay_pipeline_candidates(source: str) -> list[str]:
    """The capture pipelines to try, best first.

    A camera that offers the stream's own size is asked for exactly that, as before any of
    this: no crop, no scale, and its whole landscape view. Only a camera that cannot
    deliver it — the Reachy Mini's own 3840x2592 sensor, a 4:3 webcam — falls back to
    taking whatever mode it prefers, which costs it the crop. Preference cannot be
    expressed in caps (an ordered list and a bounded range are both ignored during
    negotiation), so it is two pipelines, tried in order.
    """
    width, height = STREAM_SIZE
    return [
        relay_pipeline_description(
            source, f"video/x-raw,width={width},height={height}"
        ),
        relay_pipeline_description(source),
    ]


def relay_pipeline_description(source: str, source_caps: str = "video/x-raw") -> str:
    """The relay: the camera on whichever of its own modes it negotiates, centre-cropped to
    the sim stream's aspect and scaled to its size, then converted to what upstream's render
    thread sends (RGB, RTP raw video, payload 96) and sent where the media server reads.

    ``source_caps`` is what the camera is asked for. The default asks only that the frames
    live in system memory: a bare ``video/x-raw`` rules out the GPU-memory caps a macOS
    camera offers first (which the crop cannot take — ``avfvideosrc`` then fails to link at
    all) and leaves size, format and rate to the camera, so any camera feeds the stream —
    one asked for a size it does not offer never negotiates, and never opens.
    ``relay_pipeline_candidates`` asks for the stream's size first on top of that.

    The crop and the scale are in both: they are no-ops on a camera already delivering the
    stream's size, and what fits any other camera's mode into it. The conversion to RGB
    comes last so it runs on the smallest frame (measured: 0.34 cores against 0.50 for
    converting first, on a 3840x2592 camera).
    """
    width, height = STREAM_SIZE
    divisor = math.gcd(width, height)
    return (
        f"{source} name={_SOURCE_NAME} ! {source_caps} ! "
        f"aspectratiocrop aspect-ratio={width // divisor}/{height // divisor} ! "
        f"videoscale ! videoconvert ! videorate ! "
        f"video/x-raw,format=RGB,width={width},height={height},"
        f"framerate={_STREAM_FPS}/1 ! queue leaky=downstream max-size-buffers=2 ! "
        "rtpvrawpay mtu=1400 name=pay ! application/x-rtp,payload=96 ! "
        f"udpsink host=127.0.0.1 port={STREAM_PORT} sync=false"
    )


class _Pipeline(Protocol):
    """What the relay needs from a running capture pipeline (tests fake it)."""

    def start(self) -> bool: ...
    def error(self) -> str | None: ...
    def source_size(self) -> tuple[int, int] | None: ...
    def device_name(self) -> str | None: ...
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

    def source_size(self) -> tuple[int, int] | None:
        """The resolution the camera negotiated, once its pad has caps."""
        element = self._pipeline.get_by_name(_SOURCE_NAME)
        pad = None if element is None else element.get_static_pad("src")
        caps = None if pad is None else pad.get_current_caps()
        if caps is None or caps.get_size() == 0:
            return None
        structure = caps.get_structure(0)
        has_width, width = structure.get_int("width")
        has_height, height = structure.get_int("height")
        if not (has_width and has_height) or width <= 0 or height <= 0:
            return None
        return int(width), int(height)

    def device_name(self) -> str | None:
        """What the camera calls itself, for the log: which camera the platform default
        turned out to be is not otherwise visible, and on a machine with a Reachy Mini
        plugged in the default is quite likely the robot's own."""
        element = self._pipeline.get_by_name(_SOURCE_NAME)
        for candidate in (*_bin_children(element), element):
            if candidate is None:
                continue
            try:
                name = candidate.get_property("device-name")
            except (TypeError, AttributeError):
                continue  # a source element without the property
            if name:
                return str(name)
        return None

    def stop(self) -> None:
        self._pipeline.set_state(self._gst.State.NULL)


def _bin_children(element: Any) -> list[Any]:
    """The elements inside ``element`` when it is a bin (``autovideosrc`` wraps the real
    camera source in one), empty otherwise."""
    iterator = getattr(element, "iterate_elements", None)
    if iterator is None:
        return []
    import gi  # pyright: ignore[reportMissingImports]

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst  # pyright: ignore[reportMissingImports]

    children: list[Any] = []
    walk = iterator()
    while True:
        result, child = walk.next()
        if result != Gst.IteratorResult.OK:
            return children
        children.append(child)


def _open_pipeline(description: str, on_frame: Callable[[], None]) -> _Pipeline:
    return _GstPipeline(description, on_frame)


class WebcamRelay:
    """Relays a host camera into the sim daemon's camera stream, in its own thread.

    A source that cannot start, reports an error, or delivers no frame for
    ``frame_timeout`` seconds is logged once (at ``ERROR``, with the macOS camera
    permission hint) and retried every ``retry`` seconds; a recovery is logged at
    ``INFO``. The daemon never stops on a camera problem.

    ``on_source_size`` is called with the resolution the camera negotiated, once frames are
    flowing — what the crop, and so the tracker's field of view, follows."""

    def __init__(
        self,
        device: str | int | None = None,
        *,
        on_source_size: Callable[[tuple[int, int]], None] | None = None,
        frame_timeout: float = _FRAME_TIMEOUT_S,
        retry: float = _RETRY_S,
        clock: Callable[[], float] = time.monotonic,
        open_pipeline: Callable[[str, Callable[[], None]], _Pipeline] | None = None,
        system: str | None = None,
    ) -> None:
        self._device = device
        self._on_source_size = on_source_size
        self._system = platform.system() if system is None else system
        # Built here so a device this platform cannot express is an error up front.
        self._source = webcam_source(device, self._system)
        self._frame_timeout = frame_timeout
        self._retry = retry
        self._clock = clock
        self._open = open_pipeline if open_pipeline is not None else _open_pipeline
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_frame = 0.0
        self._failing = False

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
        """Try each capture pipeline, best first, until one delivers frames.

        A camera that cannot give the stream's size fails the preferred pipeline outright
        (it never negotiates), which is not a problem to report — it is the reason the
        fallback exists. Only when none of them delivers is there something to say.
        """
        problem = ""
        for description in relay_pipeline_candidates(self._source):
            problem, delivered = self._run_pipeline(description)
            if delivered or self._stop.is_set():
                return problem
        return problem

    def _run_pipeline(self, description: str) -> tuple[str, bool]:
        """Run one pipeline until it fails or the relay stops: why it ended, and whether
        it ever delivered a frame."""
        seen_frame = False
        try:
            pipeline = self._open(description, self._on_frame)
        except Exception as e:  # noqa: BLE001 - a missing plugin, a bad device string
            return f"cannot build the capture pipeline: {e}", seen_frame
        try:
            self._last_frame = self._clock()
            if not pipeline.start():
                return "the camera source did not start", seen_frame
            started = self._last_frame
            while not self._stop.wait(0.2):
                error = pipeline.error()
                if error is not None:
                    return error, seen_frame
                if self._last_frame > started and not seen_frame:
                    seen_frame = True
                    self._report_camera(pipeline)
                    if self._failing:
                        _logger.info("webcam relay: frames flowing again")
                        self._failing = False
                if self._clock() - self._last_frame > self._frame_timeout:
                    return f"no frame for {self._frame_timeout:g} s", seen_frame
            return "", seen_frame
        finally:
            pipeline.stop()

    def _report_camera(self, pipeline: _Pipeline) -> None:
        """Name the camera that is actually feeding the stream, and hand its resolution
        to whoever needs it (the tracker's field of view)."""
        size = pipeline.source_size()
        name = pipeline.device_name()
        _logger.info(
            "webcam relay: %s open%s",
            name
            or ("the default camera" if self._device is None else repr(self._device)),
            "" if size is None else f" at {size[0]}x{size[1]}",
        )
        if size is None:  # frames without readable caps: the configured hfov stands
            return
        if self._on_source_size is not None:
            self._on_source_size(size)

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


class _RelayFactory(Protocol):
    """How ``corrected_backend`` builds the webcam relay (tests substitute their own)."""

    def __call__(
        self,
        device: str | int | None,
        *,
        on_source_size: Callable[[tuple[int, int]], None],
    ) -> Any: ...


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
    relay_factory: _RelayFactory = WebcamRelay,
) -> type:
    """A subclass of upstream's ``MujocoBackend`` carrying the corrections.

    - ``update_head_kinematics_model`` — which the MuJoCo loop calls once per control
      tick, where the robot loop calls it — steps tracking right after it (correction 1).
    - ``__init__`` records the camera's field of view for the tracker's intrinsics
      (correction 2): the scene's eye camera for ``sim``, the configured one for
      ``webcam``; then runs each extension's ``on_backend``.
    - With a ``webcam`` camera: ``set_tracking_face`` computes the aim from the neutral
      head pose (correction 3) — every other reader of the head pose sees the real one;
      the eye-camera render thread does nothing; the webcam relay runs with the loop and
      narrows the tracker's field of view to what the crop of its camera leaves.
    """
    camera = _Camera() if camera is None else camera
    webcam = camera.source == "webcam"

    def use_camera_size(size: tuple[int, int]) -> None:
        """The relay negotiated a camera: the frame the tracker reads is that camera
        centre-cropped, so its field of view is the configured one minus what the crop
        takes (nothing, for a camera at or narrower than the stream's aspect)."""
        hfov = cropped_hfov_deg(camera.hfov_deg, size)
        _TrackerCamera.hfov_deg = hfov
        _logger.info(
            "webcam relay: camera %dx%d cropped to %dx%d — %.1f deg of its %.1f deg "
            "horizontal field of view",
            *size,
            *STREAM_SIZE,
            hfov,
            camera.hfov_deg,
        )

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
                relay = relay_factory(camera.device, on_source_size=use_camera_size)
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
