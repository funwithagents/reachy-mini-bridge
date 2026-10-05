"""The sim daemon launcher: every MuJoCo daemon the bridge starts (specs/daemon/sim_daemon.md).

``python -m reachy_mini_bridge.sim_daemon [--scene NAME] [--headless]
[--[no-]preload-datasets] [--camera sim|webcam] [--webcam-device D] [--webcam-hfov DEG]
[--sim-display NAME]... [upstream flags...]`` runs upstream's daemon with a choice of
camera source and the viewer displays. The daemon's own face tracking is left as upstream
ships it and never armed: the bridge detects faces on the host, from the camera stream the
daemon serves, and aims the head with its own tracker (specs/vision/user_perception.md,
specs/motion/head_tracking.md).

``--camera webcam`` relays a host camera into the stream the MuJoCo daemon's media server
reads (RTP raw video on UDP 5005) instead of the eye-camera render, so the detector and
every client see the person in front of the computer. The capture pipeline constrains
nothing about the source — a camera offers the modes it has — and centre-crops whatever it
negotiates into the stream's 1280x720.

``--headless`` with the ``sim`` camera renders the eye camera offscreen wherever MuJoCo
can draw without a display — Linux, through Mesa's EGL (``MUJOCO_GL`` defaulted to
``egl``) — so a headless sim there has its camera; on macOS and Windows a headless sim
stays camera-less (specs/daemon/sim_daemon.md "The headless camera").

``--sim-display NAME`` turns a sim display on (specs/daemon/sim_displays.md,
``sim_displays.py``). The launcher's part is the wiring: it builds the displays that are
on, hands them the viewer handle, and feeds the camera overlay — off a second branch of
the relay pipeline, or off the eye-camera renderer; the stream clients read is untouched.

Importing this module pulls in neither ``mujoco`` nor GStreamer; the daemon-side pieces
import them when they run.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import platform
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Protocol

import numpy as np

from .config import CAMERA_SOURCES, DEFAULT_WEBCAM_HFOV_DEG, SIM_DISPLAYS
from .sim_displays import (
    DISPLAYS_ROUTE_PREFIX,
    OVERLAY_SOURCE_SIZE,
    FaceMarkersView,
    RobotGazeView,
    SceneLayer,
    ViewerDisplay,
    ViewerOverlay,
    build_router,
    capture_viewer,
    resample_nearest,
)

__all__ = [
    "CAMERA_SOURCES",
    "DEFAULT_WEBCAM_HFOV_DEG",
    "SIM_DISPLAYS",
    "SimDaemonExtension",
    "WebcamRelay",
    "bridge_backend",
    "offscreen_gl_backend",
    "relay_pipeline_candidates",
    "run_sim_daemon",
    "webcam_source",
]

_logger = logging.getLogger(__name__)

# The capture pipeline's source element, named so the relay can read back which of its own
# modes the camera negotiated.
_SOURCE_NAME = "camera"
# The stream the MuJoCo daemon renders its eye camera into: upstream's render thread sends
# 1280x720 RGB as RTP raw video to this port, and the media server's sim source reads it
# (GStreamerUDPCamera / _build_sim_source).
STREAM_SIZE = (1280, 720)
STREAM_PORT = 5005
_STREAM_FPS = 25
# The scene's head-mounted camera upstream renders the stream from (its `CAMERA_REACHY`).
EYE_CAMERA = "eye_camera"
# MuJoCo's GL backend variable, read once when `mujoco` is imported; the headless render on
# Linux draws through this backend unless the environment names another.
_MUJOCO_GL = "MUJOCO_GL"
_OFFSCREEN_GL_DEFAULT = "egl"
# How long the run waits for the offscreen render thread after upstream's loop returns:
# the thread checks the stop flag once per frame, so this is slack, not a wait.
_RENDER_JOIN_S = 2.0

# The webcam relay's watchdog: a source that delivers no frame for this long is a failure,
# and a failed source is retried this often.
_FRAME_TIMEOUT_S = 5.0
_RETRY_S = 5.0

# The relay's appsink for the camera overlay's branch, named so the pipeline can find it.
_OVERLAY_SINK_NAME = "overlay"


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


def relay_pipeline_candidates(source: str, *, overlay: bool = False) -> list[str]:
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
            source, f"video/x-raw,width={width},height={height}", overlay=overlay
        ),
        relay_pipeline_description(source, overlay=overlay),
    ]


def relay_pipeline_description(
    source: str, source_caps: str = "video/x-raw", *, overlay: bool = False
) -> str:
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

    With ``overlay``, a ``tee`` after the RGB caps adds a second branch — a leaky
    one-buffer queue, a scale to ``OVERLAY_SOURCE_SIZE``, a horizontal flip and an
    ``appsink`` named for the pipeline to find — that feeds the viewer overlay without
    ever making the stream branch wait; the stream branch itself is the same as without
    it. The flip is the overlay's alone: a person sees themselves as in a mirror, while
    the stream, and so the tracker, keep what the camera sees.
    """
    width, height = STREAM_SIZE
    divisor = math.gcd(width, height)
    head = (
        f"{source} name={_SOURCE_NAME} ! {source_caps} ! "
        f"aspectratiocrop aspect-ratio={width // divisor}/{height // divisor} ! "
        f"videoscale ! videoconvert ! videorate ! "
        f"video/x-raw,format=RGB,width={width},height={height},"
        f"framerate={_STREAM_FPS}/1 ! "
    )
    stream = (
        "queue leaky=downstream max-size-buffers=2 ! "
        "rtpvrawpay mtu=1400 name=pay ! application/x-rtp,payload=96 ! "
        f"udpsink host=127.0.0.1 port={STREAM_PORT} sync=false"
    )
    if not overlay:
        return head + stream
    overlay_width, overlay_height = OVERLAY_SOURCE_SIZE
    return (
        f"{head}tee name=t ! {stream} t. ! "
        "queue leaky=downstream max-size-buffers=1 ! videoscale ! "
        f"video/x-raw,width={overlay_width},height={overlay_height} ! "
        "videoflip method=horizontal-flip ! "
        f"appsink name={_OVERLAY_SINK_NAME} emit-signals=true max-buffers=1 drop=true "
        "sync=false"
    )


class _Pipeline(Protocol):
    """What the relay needs from a running capture pipeline (tests fake it)."""

    def start(self) -> bool: ...
    def error(self) -> str | None: ...
    def source_size(self) -> tuple[int, int] | None: ...
    def device_name(self) -> str | None: ...
    def stop(self) -> None: ...


class _GstPipeline:
    """A ``Gst.parse_launch`` pipeline reporting each buffer that reaches the payloader,
    and handing each frame of the overlay branch (when the description has one and a
    receiver was given) to ``on_overlay_frame`` as an RGB ``uint8`` array."""

    def __init__(
        self,
        description: str,
        on_frame: Callable[[], None],
        on_overlay_frame: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        # gi ships inside the gstreamer wheel's own site-packages, invisible to pyright.
        import gi  # pyright: ignore[reportMissingImports]

        gi.require_version("Gst", "1.0")
        from gi.repository import (  # pyright: ignore[reportMissingImports]
            Gst,  # pyright: ignore[reportAttributeAccessIssue]
        )

        Gst.init(None)
        self._gst: Any = Gst
        self._pipeline: Any = Gst.parse_launch(description)
        pay = self._pipeline.get_by_name("pay")

        def probe(_pad: Any, _info: Any) -> Any:
            on_frame()
            return Gst.PadProbeReturn.OK

        pay.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, probe)
        sink = self._pipeline.get_by_name(_OVERLAY_SINK_NAME)
        if sink is not None and on_overlay_frame is not None:

            def on_sample(appsink: Any) -> Any:
                sample = appsink.emit("pull-sample")
                frame = None if sample is None else _sample_frame(sample, Gst)
                if frame is not None:
                    on_overlay_frame(frame)
                return Gst.FlowReturn.OK

            sink.connect("new-sample", on_sample)
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


def _sample_frame(sample: Any, Gst: Any) -> np.ndarray | None:
    """The RGB frame of an appsink sample as a ``(height, width, 3)`` copy, ``None`` when
    the buffer cannot be mapped or is short."""
    structure = sample.get_caps().get_structure(0)
    _, width = structure.get_int("width")
    _, height = structure.get_int("height")
    buffer = sample.get_buffer()
    mapped, info = buffer.map(Gst.MapFlags.READ)
    if not mapped:
        return None
    try:
        data = np.frombuffer(info.data, dtype=np.uint8)
        size = width * height * 3
        if width <= 0 or height <= 0 or data.size < size:
            return None
        return data[:size].reshape(height, width, 3).copy()
    finally:
        buffer.unmap(info)


def _bin_children(element: Any) -> list[Any]:
    """The elements inside ``element`` when it is a bin (``autovideosrc`` wraps the real
    camera source in one), empty otherwise."""
    iterator = getattr(element, "iterate_elements", None)
    if iterator is None:
        return []
    import gi  # pyright: ignore[reportMissingImports]

    gi.require_version("Gst", "1.0")
    from gi.repository import (  # pyright: ignore[reportMissingImports]
        Gst,  # pyright: ignore[reportAttributeAccessIssue]
    )

    children: list[Any] = []
    walk = iterator()
    while True:
        result, child = walk.next()
        if result != Gst.IteratorResult.OK:
            return children
        children.append(child)


class _PipelineOpener(Protocol):
    """How the relay builds a capture pipeline (tests substitute their own)."""

    def __call__(
        self,
        description: str,
        on_frame: Callable[[], None],
        on_overlay_frame: Callable[[np.ndarray], None] | None = None,
    ) -> _Pipeline: ...


def _open_pipeline(
    description: str,
    on_frame: Callable[[], None],
    on_overlay_frame: Callable[[np.ndarray], None] | None = None,
) -> _Pipeline:
    return _GstPipeline(description, on_frame, on_overlay_frame)


class _Overlay(Protocol):
    """What the backend and the relay ask of the viewer overlay (``ViewerOverlay``; the
    tests substitute a recorder)."""

    def attach(self, handle: Any) -> None: ...
    def show(self, frame: np.ndarray) -> None: ...
    def label(self, text: str) -> None: ...
    def stop(self) -> None: ...


class WebcamRelay:
    """Relays a host camera into the sim daemon's camera stream, in its own thread.

    A source that cannot start, reports an error, or delivers no frame for
    ``frame_timeout`` seconds is logged once (at ``ERROR``, with the macOS camera
    permission hint) and retried every ``retry`` seconds; a recovery is logged at
    ``INFO``. The daemon never stops on a camera problem.

    Once frames flow, the camera feeding the stream is named in the log with the
    resolution it negotiated. With an ``overlay``, the pipeline grows its overlay branch,
    whose frames go to ``overlay.show``, and the camera's name and size become the
    overlay's label."""

    def __init__(
        self,
        device: str | int | None = None,
        *,
        overlay: _Overlay | None = None,
        frame_timeout: float = _FRAME_TIMEOUT_S,
        retry: float = _RETRY_S,
        clock: Callable[[], float] = time.monotonic,
        open_pipeline: _PipelineOpener | None = None,
        system: str | None = None,
    ) -> None:
        self._device = device
        self._overlay = overlay
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
        overlay = self._overlay is not None
        for description in relay_pipeline_candidates(self._source, overlay=overlay):
            problem, delivered = self._run_pipeline(description)
            if delivered or self._stop.is_set():
                return problem
        return problem

    def _run_pipeline(self, description: str) -> tuple[str, bool]:
        """Run one pipeline until it fails or the relay stops: why it ended, and whether
        it ever delivered a frame."""
        seen_frame = False
        on_overlay_frame = None if self._overlay is None else self._overlay.show
        try:
            pipeline = self._open(description, self._on_frame, on_overlay_frame)
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
        """Name the camera that is actually feeding the stream, and the resolution it
        negotiated — which one the platform default turned out to be is not otherwise
        visible."""
        size = pipeline.source_size()
        name = pipeline.device_name()
        camera = name or (
            "the default camera" if self._device is None else repr(self._device)
        )
        _logger.info(
            "webcam relay: %s open%s",
            camera,
            "" if size is None else f" at {size[0]}x{size[1]}",
        )
        if self._overlay is not None:
            self._overlay.label(
                f"webcam: {camera}" + ("" if size is None else f" {size[0]}x{size[1]}")
            )

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


class _TappedRenderer:
    """Upstream's offscreen eye-camera renderer with every rendered frame also handed to
    the overlay (at ``OVERLAY_SOURCE_SIZE``), on the render thread, before the frame goes
    to the stream. Everything else is the renderer's."""

    def __init__(self, renderer: Any, overlay: _Overlay) -> None:
        self._renderer = renderer
        self._overlay = overlay
        self._fed = False

    def render(self, *args: Any, **kwargs: Any) -> Any:
        frame = self._renderer.render(*args, **kwargs)
        self._overlay.show(resample_nearest(frame, OVERLAY_SOURCE_SIZE))
        if not self._fed:
            self._fed = True
            _logger.info(
                "camera overlay: fed by the eye camera render (%dx%d)",
                frame.shape[1],
                frame.shape[0],
            )
        return frame

    def __getattr__(self, name: str) -> Any:
        return getattr(self._renderer, name)


# --- the headless camera (specs/daemon/sim_daemon.md "The headless camera") ------------


def offscreen_gl_backend(
    system: str | None = None, environ: Mapping[str, str] | None = None
) -> str | None:
    """The GL backend a headless ``sim``-camera run renders the eye camera through with
    no display, or ``None`` where MuJoCo has no display-less backend.

    On Linux: what ``MUJOCO_GL`` names when it is set (``osmesa`` works too, slower), else
    ``egl`` — Mesa's, drawing with no window and no X server. On macOS and Windows the
    context can only come from the window server (GLFW, the viewer's), so ``None``: a
    headless sim stays camera-less there. ``system`` is ``platform.system()``'s word.
    """
    system = platform.system() if system is None else system
    environ = os.environ if environ is None else environ
    if system != "Linux":
        return None
    named = environ.get(_MUJOCO_GL, "").strip()
    return named or _OFFSCREEN_GL_DEFAULT


# --- the backend subclass (camera source wiring, the overlay, the hooks) -------------


@dataclass(frozen=True)
class SimDaemonExtension:
    """Adds to the launched daemon: ``on_backend(backend)`` runs at the end of the
    backend's ``__init__`` (the model exists); ``on_app(app)`` on the FastAPI app
    upstream's ``create_app`` built."""

    on_backend: Callable[[Any], None] | None = None
    on_app: Callable[[Any], None] | None = None


class _RelayFactory(Protocol):
    """How ``bridge_backend`` builds the webcam relay (tests substitute their own)."""

    def __call__(
        self, device: str | int | None, *, overlay: _Overlay | None = None
    ) -> Any: ...


@dataclass(frozen=True)
class _Displays:
    """The viewer displays turned on (``--sim-display``; ``SIM_DISPLAYS``)."""

    camera_overlay: bool = False
    robot_gaze: bool = False
    face_markers: bool = False

    @classmethod
    def named(cls, names: Sequence[str]) -> _Displays:
        return cls(**{name: name in names for name in SIM_DISPLAYS})


@dataclass(frozen=True)
class _Camera:
    source: str = "sim"
    device: str | int | None = None
    hfov_deg: float = DEFAULT_WEBCAM_HFOV_DEG


def bridge_backend(
    backend_class: type,
    *,
    camera: _Camera | None = None,
    displays: _Displays | None = None,
    extensions: Sequence[SimDaemonExtension] = (),
    relay_factory: _RelayFactory = WebcamRelay,
    overlay_factory: Callable[[], _Overlay] = ViewerOverlay,
    layer_factory: Callable[[Sequence[Any]], ViewerDisplay] = SceneLayer,
    on_face_markers: Callable[[FaceMarkersView], None] | None = None,
    viewer_module: Any | None = None,
    headless_render: str | None = None,
) -> type:
    """A subclass of upstream's ``MujocoBackend`` wiring the launcher's additions in
    (specs/daemon/sim_daemon.md "The backend subclass"). It overrides nothing of the daemon's
    control loop, kinematics or face tracking.

    - ``__init__`` runs each extension's ``on_backend`` once the model exists.
    - With a ``webcam`` camera: the eye-camera render thread does nothing, and the webcam
      relay runs with the loop.
    - With ``headless_render`` (the GL backend's name — the launcher passes it for a
      headless run with the ``sim`` camera where ``offscreen_gl_backend`` found one):
      ``run()`` starts upstream's eye-camera render thread itself, which upstream starts
      only under the viewer, and joins it after the run; a renderer that cannot build its
      context logs one error and the daemon runs on without a camera.
    - With ``displays.camera_overlay``: a ``ViewerOverlay`` (from ``overlay_factory``)
      is fed by the relay's overlay branch (webcam) or a tap on the eye-camera renderer
      (sim).
    - With ``displays.robot_gaze`` / ``displays.face_markers``: a scene layer (from
      ``layer_factory``) over the views that are on, built once the model exists; the
      face markers view is also handed to ``on_face_markers``, for the displays router.
    - ``run()`` hands every display that is on the viewer upstream launches
      (``viewer_module``, the ``mujoco.viewer`` module by default) and stops them with
      the run.
    """
    camera = _Camera() if camera is None else camera
    displays = _Displays() if displays is None else displays
    webcam = camera.source == "webcam"

    class BridgeMujocoBackend(backend_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._viewer_overlay: _Overlay | None = (
                overlay_factory() if displays.camera_overlay else None
            )
            views: list[Any] = []
            if displays.robot_gaze:
                views.append(RobotGazeView(self.model, self.data))
            if displays.face_markers:
                markers = FaceMarkersView.for_backend(self)
                views.append(markers)
                if on_face_markers is not None:
                    on_face_markers(markers)
            self._scene_layer: ViewerDisplay | None = (
                layer_factory(views) if views else None
            )
            for extension in extensions:
                if extension.on_backend is not None:
                    extension.on_backend(self)

        if webcam:

            def rendering_loop(self, *args: Any, **kwargs: Any) -> None:
                return None  # the webcam relay feeds the camera stream

        if displays.camera_overlay and not webcam:

            def _get_renderer(self, camera_name: str) -> Any:
                renderer = super()._get_renderer(camera_name)
                overlay = self._viewer_overlay
                if overlay is None:
                    return renderer
                overlay.label(f"eye camera {STREAM_SIZE[0]}x{STREAM_SIZE[1]}")
                return _TappedRenderer(renderer, overlay)

        def _offscreen_rendering(self) -> None:
            try:
                self.rendering_loop(EYE_CAMERA, STREAM_PORT)
            except Exception as e:  # noqa: BLE001 - the daemon runs on without a camera
                _logger.error(
                    "offscreen camera render failed on %s=%s: %s — the headless sim "
                    "runs on without a camera; is Mesa's EGL installed?",
                    _MUJOCO_GL,
                    headless_render,
                    e,
                )

        def run(self) -> None:
            overlay = self._viewer_overlay
            relay = relay_factory(camera.device, overlay=overlay) if webcam else None
            if relay is not None:
                relay.start()
            render: threading.Thread | None = None
            if headless_render is not None and not webcam:
                render = threading.Thread(
                    target=self._offscreen_rendering,
                    name="offscreen-camera-render",
                    daemon=True,
                )
                render.start()
            shown: list[ViewerDisplay] = [
                display
                for display in (overlay, self._scene_layer)
                if display is not None
            ]
            try:
                with capture_viewer(shown, viewer_module) if shown else nullcontext():
                    super().run()
            finally:
                if relay is not None:
                    relay.stop()
                if render is not None:
                    render.join(_RENDER_JOIN_S)
                for display in shown:
                    display.stop()

    BridgeMujocoBackend.__name__ = backend_class.__name__
    BridgeMujocoBackend.__qualname__ = backend_class.__qualname__
    return BridgeMujocoBackend


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
        description="Run the Reachy Mini MuJoCo daemon with the bridge's camera source "
        "and viewer displays (specs/daemon/sim_daemon.md). Unrecognised flags go to upstream's "
        "daemon.",
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
    parser.add_argument(
        "--sim-display",
        action="append",
        choices=SIM_DISPLAYS,
        default=[],
        metavar="NAME",
        help=f"a viewer display to turn on ({', '.join(SIM_DISPLAYS)}); repeatable",
    )
    return parser


def run_sim_daemon(
    argv: Sequence[str] | None = None,
    *,
    extensions: Sequence[SimDaemonExtension] = (),
    prog: str = "python -m reachy_mini_bridge.sim_daemon",
) -> None:
    """Run upstream's MuJoCo daemon with the bridge's backend and ``extensions`` installed.

    Upstream's ``main()`` parses ``sys.argv``; this rewrites it to ``--sim [--scene S]
    [--headless] --[no-]preload-datasets`` plus anything unrecognised, substitutes the
    backend class the daemon constructs, wraps ``create_app`` for the extensions'
    ``on_app``, and calls ``main()``.
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
    if args.sim_display and args.headless:
        parser.error("--sim-display needs the viewer (drop --headless)")
    displays = _Displays.named(args.sim_display)
    # The headless camera: the GL backend is chosen — and MUJOCO_GL set — before anything
    # imports mujoco, which reads the variable once.
    headless_render: str | None = None
    if args.headless and camera.source == "sim":
        headless_render = offscreen_gl_backend()
        if headless_render is not None and not os.environ.get(_MUJOCO_GL, "").strip():
            os.environ[_MUJOCO_GL] = headless_render
    # The face markers view exists once the backend is built; the displays router,
    # mounted on the app before that, reaches the latest one through this holder.
    face_markers: list[FaceMarkersView] = []

    from reachy_mini.daemon import daemon as upstream_daemon
    from reachy_mini.daemon.app import main as upstream_main

    upstream_daemon.MujocoBackend = bridge_backend(
        upstream_daemon.MujocoBackend,
        camera=camera,
        displays=displays,
        extensions=extensions,
        on_face_markers=face_markers.append,
        headless_render=headless_render,
    )
    original_create_app = upstream_main.create_app

    def create_app(*a: Any, **kw: Any) -> Any:
        app = original_create_app(*a, **kw)
        if displays.face_markers:
            app.include_router(
                build_router(lambda: face_markers[-1] if face_markers else None),
                prefix=DISPLAYS_ROUTE_PREFIX,
            )
        for extension in extensions:
            if extension.on_app is not None:
                extension.on_app(app)
        return app

    upstream_main.create_app = create_app
    sys.argv = [
        "reachy-mini-daemon",
        "--sim",
        *(["--scene", args.scene] if args.scene else []),
        *(["--headless"] if args.headless else []),
        "--preload-datasets" if args.preload_datasets else "--no-preload-datasets",
        *passthrough,
    ]
    _logger.info(
        "sim daemon: camera %s, displays %s, headless render %s",
        camera,
        args.sim_display or "none",
        headless_render or "none",
    )
    upstream_main.main()


if __name__ == "__main__":  # pragma: no cover - the daemon-side entry point
    run_sim_daemon()
