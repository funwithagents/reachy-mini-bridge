"""One MuJoCo world, two Reachy Minis, one upstream daemon per robot — in one process.

Spike code (see README.md beside this file). Run it with

    uv run python -m spikes.two_robot_sim.host --headless          # no window
    uv run mjpython -m spikes.two_robot_sim.host                   # macOS viewer

Layout:

* ``PhysicsHost`` owns the one ``MjModel`` / ``MjData``, the one viewer, the one thread
  that calls ``mj_step``, and the one render thread (one GL context, one frame per robot
  per tick, each sent to that robot's UDP port).
* ``robot_view(...)`` is a subclass of upstream's ``MujocoBackend`` bound to one robot's
  name prefix. It loads nothing and steps nothing: its ``run()`` attaches it to the host,
  which calls its ``control_tick()`` from the physics thread every 20 ms.
* ``robot_media_server(...)`` is a subclass of upstream's ``GstMediaServer`` with the
  robot's own UDP port, camera IPC socket, signalling port and audio devices.
* Each robot gets upstream's own FastAPI app (``create_app``) on its own port, both
  served from one event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from typing import Any

import mujoco
import mujoco.viewer
import numpy as np

from spikes.two_robot_sim.world import FACING, STREAM_SIZE, RobotPlacement, build_world

logger = logging.getLogger("two_robot_sim")

_TIMESTEP_S = 0.002  # upstream's: 500 Hz physics
_DECIMATION = 10  # upstream's: a 50 Hz control loop
_RENDER_PERIOD_S = 0.04  # upstream's: 25 Hz
_HEAD_Z_OFFSET_M = 0.177  # upstream's get_mj_present_head_pose
_VISUAL_GROUPS = (2, 1)  # robot A's visual meshes keep upstream's group, robot B's move
_ACTUATORS = (
    "yaw_body",
    "stewart_1",
    "stewart_2",
    "stewart_3",
    "stewart_4",
    "stewart_5",
    "stewart_6",
    "right_antenna",
    "left_antenna",
)


@dataclass(frozen=True)
class RobotEndpoints:
    """Everything one robot's daemon binds or opens, none of it shared with the other."""

    placement: RobotPlacement
    rest_port: int
    udp_port: int  # the RTP stream from the render thread to this robot's media server
    signalling_port: int  # webrtcsink's embedded signalling server
    camera_socket: str  # the IPC socket a local client's get_frame() reads
    audio_input: str | None = None
    audio_output: str | None = None
    discard_audio_output: bool = False

    @property
    def name(self) -> str:
        return self.placement.prefix.rstrip("_")


class PhysicsHost:
    """The single owner of the model, the data, ``mj_step``, the viewer and the renderer."""

    def __init__(
        self,
        model: mujoco.MjModel,
        *,
        headless: bool,
        render: bool = True,
        full_render: bool = False,
    ) -> None:
        self.model = model
        self.data = mujoco.MjData(model)
        self.model.opt.timestep = _TIMESTEP_S
        self.headless = headless
        self.render = render
        self.full_render = full_render
        self.should_stop = threading.Event()
        self.settled = threading.Event()
        # Guards model/data between the physics thread and the render thread. Upstream
        # reads ``data`` from its render thread unguarded; with one extra lock the frame
        # is a consistent state.
        self.lock = threading.Lock()
        self._views: list[Any] = []
        self._views_lock = threading.Lock()
        self._cameras: list[tuple[str, int]] = []  # (camera name, udp port)
        self._threads: list[threading.Thread] = []
        self.frames_sent: dict[str, int] = {}
        self.render_ms: dict[str, float] = {}  # running means, per camera frame
        self.step_count = 0

    # -- wiring -------------------------------------------------------------------

    def add_camera(self, camera_name: str, udp_port: int) -> None:
        """Register a robot's eye camera; cameras are rendered in registration order."""
        index = len(self._cameras)
        self._cameras.append((camera_name, udp_port))
        model = self.model
        root = int(model.body_rootid[model.cam_bodyid[model.camera(camera_name).id]])
        for geom in range(model.ngeom):
            if (
                int(model.body_rootid[model.geom_bodyid[geom]]) == root
                and model.geom_group[geom] == 2
            ):
                model.geom_group[geom] = _VISUAL_GROUPS[index]

    def attach(self, view: Any) -> None:
        with self._views_lock:
            self._views.append(view)

    def detach(self, view: Any) -> None:
        with self._views_lock:
            if view in self._views:
                self._views.remove(view)

    def start(self) -> None:
        thread = threading.Thread(target=self._run, name="physics", daemon=True)
        self._threads.append(thread)
        thread.start()

    def stop(self) -> None:
        self.should_stop.set()
        for thread in self._threads:
            thread.join(timeout=3.0)

    # -- the physics thread -------------------------------------------------------

    def _settle(self, prefixes: list[str]) -> None:
        """Upstream's start-up, once for the whole world: the sleep pose on every robot,
        collisions off while it settles, then on."""
        from reachy_mini.reachy_mini import (
            SLEEP_ANTENNAS_JOINT_POSITIONS,
            SLEEP_HEAD_JOINT_POSITIONS,
        )

        model, data = self.model, self.data
        sleep = np.array(
            list(SLEEP_HEAD_JOINT_POSITIONS)
            + [SLEEP_ANTENNAS_JOINT_POSITIONS[1], SLEEP_ANTENNAS_JOINT_POSITIONS[0]]
        )
        for prefix in prefixes:
            for name, value in zip(_ACTUATORS, sleep, strict=True):
                data.qpos[model.joint(prefix + name).qposadr] = value
                data.ctrl[model.actuator(prefix + name).id] = value
        unnamed_colliders = [
            i
            for i in range(model.ngeom)
            if model.geom_contype[i] != 0
            and mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_GEOM, i) is None
        ]
        for i in unnamed_colliders:
            model.geom_contype[i] = 0
            model.geom_conaffinity[i] = 0
        mujoco.mj_forward(model, data)
        for _ in range(100):
            mujoco.mj_step(model, data)
        for i in unnamed_colliders:
            model.geom_contype[i] = 1
            model.geom_conaffinity[i] = 1
        for _ in range(101):
            mujoco.mj_step(model, data)

    def _run(self) -> None:
        viewer = None
        try:
            if not self.headless:
                viewer = mujoco.viewer.launch_passive(
                    self.model, self.data, show_left_ui=False, show_right_ui=False
                )
                with viewer.lock():
                    viewer.cam.type = mujoco.mjtCamera.mjCAMERA_FREE
                    viewer.cam.distance = 1.3
                    viewer.cam.azimuth = 90  # side on: A on the left, B on the right
                    viewer.cam.elevation = -15
                    viewer.cam.lookat[:] = [0.3, 0, 0.15]
            prefixes = sorted(
                {name.rsplit("eye_camera", 1)[0] for name, _ in self._cameras}
            )
            self._settle(prefixes)
            if viewer is not None:
                viewer.sync()
            self.settled.set()
            if self.render and self._cameras:
                render = threading.Thread(
                    target=self._render_loop, name="render", daemon=True
                )
                self._threads.append(render)
                render.start()
            step = 1
            next_t = time.perf_counter()
            while not self.should_stop.is_set():
                with self.lock:
                    if step % _DECIMATION == 0:
                        with self._views_lock:
                            views = list(self._views)
                        for view in views:
                            try:
                                view.control_tick()
                            except Exception:
                                logger.exception(
                                    "control tick of %s failed", view.prefix
                                )
                    mujoco.mj_step(self.model, self.data)
                if viewer is not None and step % _DECIMATION == 0:
                    viewer.sync()
                step += 1
                self.step_count = step
                next_t += _TIMESTEP_S
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -0.1:
                    next_t = (
                        time.perf_counter()
                    )  # fell behind: do not sprint to catch up
        except Exception:
            logger.exception("physics thread died")
        finally:
            if viewer is not None:
                viewer.close()

    # -- the render thread --------------------------------------------------------

    def _render_loop(self) -> None:
        from reachy_mini.media.gstreamer_udp_camera import GStreamerUDPCamera

        width, height = STREAM_SIZE
        try:
            renderer = mujoco.Renderer(self.model, height=height, width=width)
        except Exception:
            logger.exception("no GL context for the eye cameras: no camera stream")
            return
        senders = []
        for index, (name, port) in enumerate(self._cameras):
            sender = GStreamerUDPCamera(dest_port=port, width=width, height=height)
            sender.start()
            option = mujoco.MjvOption()
            if not self.full_render:
                # Each camera leaves its own robot out. A robot's visual meshes are moved
                # to a geom group of its own (the first stays in upstream's group 2, the
                # second goes to group 1 — both shown by the viewer), so the choice is a
                # per-camera MjvOption and the shared model, which the viewer also draws,
                # is never touched while running.
                option.geomgroup[_VISUAL_GROUPS[index]] = 0
            senders.append((name, self.model.camera(name).id, sender, option))
            self.frames_sent[name] = 0
        if not self.full_render:
            for flag in (
                mujoco.mjtRndFlag.mjRND_SHADOW,
                mujoco.mjtRndFlag.mjRND_REFLECTION,
            ):
                renderer.scene.flags[flag] = 0
        logger.info(
            "rendering %s", ", ".join(f"{n} -> udp {p}" for n, p in self._cameras)
        )
        while not self.should_stop.is_set():
            start = time.perf_counter()
            for name, camera_id, sender, option in senders:
                t0 = time.perf_counter()
                with self.lock:
                    t1 = time.perf_counter()
                    renderer.update_scene(self.data, camera_id, scene_option=option)
                t2 = time.perf_counter()
                frame = renderer.render()
                t3 = time.perf_counter()
                sender.send_frame(frame)
                t4 = time.perf_counter()
                self.frames_sent[name] += 1
                for key, value in (
                    ("lock", t1 - t0),
                    ("scene", t2 - t1),
                    ("draw", t3 - t2),
                    ("send", t4 - t3),
                ):
                    self.render_ms[key] = (
                        0.95 * self.render_ms.get(key, value * 1e3) + 0.05 * value * 1e3
                    )
            time.sleep(max(0.0, _RENDER_PERIOD_S - (time.perf_counter() - start)))
        renderer.close()


def robot_view(host: PhysicsHost, placement: RobotPlacement) -> type:
    """Upstream's ``MujocoBackend`` as a view of one robot of ``host``'s world."""
    from reachy_mini.daemon.backend.abstract import Backend
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend
    from reachy_mini.io.protocol import HeadPoseMsg, JointPositionsMsg

    prefix = placement.prefix
    base_inverse = np.linalg.inv(placement.base_pose())

    class RobotView(MujocoBackend):  # type: ignore[misc, valid-type]
        def __init__(
            self,
            scene: str = "empty",  # ignored: the host built the world
            check_collision: bool = False,
            kinematics_engine: str = "AnalyticalKinematics",
            headless: bool = False,
            use_audio: bool = False,
        ) -> None:
            # Upstream's MujocoBackend.__init__ loads a scene file and looks names up
            # unprefixed, so it is skipped: the base class's __init__ runs, then what
            # MujocoBackend.__init__ would have set, bound to this robot.
            Backend.__init__(
                self,
                check_collision=check_collision,
                kinematics_engine=kinematics_engine,
                use_audio=use_audio,
            )
            self.prefix = prefix
            self.headless = True  # this view never opens a viewer nor renders
            self.model = host.model
            self.data = host.data
            self.decimation = _DECIMATION
            self.rendering_timestep = _RENDER_PERIOD_S
            self.streaming_timestep = _RENDER_PERIOD_S
            self.head_site_id = self.model.site(prefix + "head").id
            self.current_head_pose = np.eye(4)
            self.joint_names = [prefix + name for name in _ACTUATORS]
            self.joint_ids = [self.model.joint(n).id for n in self.joint_names]
            self.joint_qpos_addr = [
                int(self.model.joint(n).qposadr[0]) for n in self.joint_names
            ]
            self.ctrl_ids = [self.model.actuator(n).id for n in self.joint_names]
            self.col_inds: list[int] = []
            self._kinematics_seeded = False

        # -- the loop: upstream's, minus everything the host owns -----------------

        def run(self) -> None:
            host.settled.wait()
            host.attach(self)
            try:
                self.should_stop.wait()
            finally:
                host.detach(self)

        def control_tick(self) -> None:
            """One 50 Hz iteration of upstream's ``run()`` loop body, called by the host
            on the physics thread, under its lock."""
            if not self._kinematics_seeded:
                self.head_kinematics.ik(
                    self.get_mj_present_head_pose(), no_iterations=20
                )
                self.head_kinematics.fk(
                    self.get_present_head_joint_positions(), no_iterations=20
                )
                self._kinematics_seeded = True
            self.current_head_joint_positions = self.get_present_head_joint_positions()
            self.current_antenna_joint_positions = (
                self.get_present_antenna_joint_positions()
            )
            self.update_head_kinematics_model(
                self.current_head_joint_positions, self.current_antenna_joint_positions
            )
            self.current_head_pose = self.get_mj_present_head_pose()
            if self.ik_required:
                try:
                    self.update_target_head_joints_from_ik(
                        self.target_head_pose, self.target_body_yaw
                    )
                except ValueError as error:
                    self.logger.warning("IK error (%s): %s", prefix, error)
            # Upstream writes ctrl[:7] and ctrl[-2:]: the whole array is one robot's.
            if self.target_head_joint_positions is not None:
                self.data.ctrl[self.ctrl_ids[:7]] = self.target_head_joint_positions
            if self.target_antenna_joint_positions is not None:
                self.data.ctrl[
                    self.ctrl_ids[-2:]
                ] = -self.target_antenna_joint_positions
            if (
                self.joint_positions_publisher is not None
                and self.pose_publisher is not None
            ):
                if not self.is_shutting_down:
                    self.joint_positions_publisher.put(
                        JointPositionsMsg(
                            head_joint_positions=self.current_head_joint_positions.tolist(),
                            antennas_joint_positions=self.current_antenna_joint_positions.tolist(),
                        )
                    )
                    self.pose_publisher.put(
                        HeadPoseMsg(head_pose=self.get_present_head_pose().tolist())
                    )
                self.ready.set()

        def rendering_loop(self, camera_name: str, port: int) -> None:
            raise RuntimeError("the physics host renders; a robot view never does")

        # -- state in the robot's own frame ---------------------------------------

        def get_mj_present_head_pose(self) -> Any:
            # Upstream reads the head site in the world frame, which is the robot's own
            # only for a robot standing at the origin.
            world = np.eye(4)
            world[:3, :3] = self.data.site_xmat[self.head_site_id].reshape(3, 3)
            world[:3, 3] = self.data.site_xpos[self.head_site_id]
            pose = base_inverse @ world
            pose[2, 3] -= _HEAD_Z_OFFSET_M
            return pose

    RobotView.__name__ = f"RobotView_{prefix.rstrip('_')}"
    return RobotView


_media_patch_lock = threading.RLock()


def robot_media_server(endpoints: RobotEndpoints) -> type:
    """Upstream's ``GstMediaServer`` on one robot's ports, socket and audio devices."""
    import reachy_mini.media.media_server as upstream

    from reachy_mini_bridge import audio_devices

    resolved = audio_devices.resolve_devices(
        endpoints.audio_input, endpoints.audio_output
    )

    @contextlib.contextmanager
    def own_lookups() -> Any:
        """Upstream's builders read module globals (the socket path, the device lookup);
        point them at this robot for the length of one builder call."""
        with _media_patch_lock:
            saved = (
                upstream.CAMERA_SOCKET_PATH,
                upstream.is_local_camera_available,
                upstream.get_audio_device,
            )
            original = saved[2]

            def get_audio_device(device_type: str = "Source") -> str | None:
                chosen = resolved.source if device_type == "Source" else resolved.sink
                return chosen if chosen is not None else original(device_type)

            upstream.CAMERA_SOCKET_PATH = endpoints.camera_socket
            upstream.is_local_camera_available = lambda: os.path.exists(
                endpoints.camera_socket
            )
            upstream.get_audio_device = get_audio_device
            try:
                yield
            finally:
                (
                    upstream.CAMERA_SOCKET_PATH,
                    upstream.is_local_camera_available,
                    upstream.get_audio_device,
                ) = saved

    class RobotMediaServer(upstream.GstMediaServer):  # type: ignore[misc, name-defined]
        endpoints_ = endpoints

        def _configure_webrtc(self, pipeline: Any) -> Any:
            webrtcsink = super()._configure_webrtc(pipeline)
            webrtcsink.set_property("signalling-server-port", endpoints.signalling_port)
            webrtcsink.get_property("signaller").set_property(
                "uri", f"ws://127.0.0.1:{endpoints.signalling_port}"
            )
            return webrtcsink

        def _build_sim_source(self) -> list[Any]:
            elements = super()._build_sim_source()
            elements[0].set_property("port", endpoints.udp_port)  # upstream: 5005
            return elements

        def _build_ipc_branch(self, *args: Any, **kwargs: Any) -> None:
            with own_lookups():
                super()._build_ipc_branch(*args, **kwargs)

        def _build_audio_source(self) -> Any:
            with own_lookups():
                return super()._build_audio_source()

        def _build_audiosink_element(self) -> Any:
            if endpoints.discard_audio_output:
                return audio_devices.discarding_sink()
            with own_lookups():
                return super()._build_audiosink_element()

    return RobotMediaServer


def robot_app(
    host: PhysicsHost, endpoints: RobotEndpoints, *, media: bool, kinematics: str
) -> Any:
    """Upstream's FastAPI daemon app for one robot of the host's world."""
    import reachy_mini.media.media_server as upstream_media
    from reachy_mini.daemon.app.main import Args, create_app

    args = Args(
        sim=True,
        headless=True,
        no_media=not media,
        kinematics_engine=kinematics,
        preload_datasets=False,
        dataset_update_interval_hours=0,
        robot_name=f"reachy_mini_{endpoints.name}",
        fastapi_port=endpoints.rest_port,
        log_level="INFO",
    )
    # Daemon.__init__ builds its media server from the module attribute, at call time.
    stock = upstream_media.GstMediaServer
    upstream_media.GstMediaServer = robot_media_server(endpoints) if media else stock
    try:
        app = create_app(args)
    finally:
        upstream_media.GstMediaServer = stock
    view_class = robot_view(host, endpoints.placement)

    def setup_backend(**kwargs: Any) -> Any:
        return view_class(
            check_collision=kwargs["check_collision"],
            kinematics_engine=kwargs["kinematics_engine"],
            use_audio=kwargs["use_audio"],
        )

    # An instance attribute: this daemon's backend is this robot's view. The module
    # attribute upstream reads (daemon.MujocoBackend) is one per process.
    app.state.daemon._setup_backend = setup_backend

    async def no_central_relay() -> None:
        # Upstream's relay to Hugging Face's central signalling (remote WebRTC access,
        # started whenever an HF token is found) is one per process and dials the fixed
        # local signalling port 8443; a local two-robot sim has no use for it.
        return None

    app.state.daemon._start_central_signaling_relay = no_central_relay

    @app.get("/api/spike/robot")
    def spike_robot() -> dict[str, Any]:
        """What a client must know to reach this robot and no other."""
        return {
            "prefix": endpoints.placement.prefix,
            "base_pos": list(endpoints.placement.pos),
            "base_quat": list(endpoints.placement.quat),
            "rest_port": endpoints.rest_port,
            "camera_socket": endpoints.camera_socket if media else None,
            "udp_port": endpoints.udp_port,
            "signalling_port": endpoints.signalling_port,
            "audio": {
                "input_device": endpoints.audio_input,
                "output_device": endpoints.audio_output,
                "discard_output": endpoints.discard_audio_output,
            },
            "frames_sent": host.frames_sent.get(
                endpoints.placement.prefix + "eye_camera", 0
            ),
            "render_ms": {k: round(v, 2) for k, v in host.render_ms.items()},
            "physics_steps": host.step_count,
        }

    # The bridge's own report, so a ReachyMiniBridge follows this robot's audio devices.
    @app.get("/api/bridge/daemon")
    def bridge_report() -> dict[str, Any]:
        return {
            "launcher": "sim",
            "camera": {"source": "sim", "hfov_deg": None},
            "audio": {
                "input_device": endpoints.audio_input,
                "output_device": endpoints.audio_output,
                "discard_output": endpoints.discard_audio_output,
            },
            "scene": "two_robot_scene",
            "headless": host.headless,
            "preload_datasets": False,
            "sim_displays": [],
        }

    return app


async def serve(apps: list[tuple[Any, int]]) -> None:
    import uvicorn

    servers = [
        uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        )
        for app, port in apps
    ]

    def stop_all(*_: Any) -> None:
        for server in servers:
            server.should_exit = True

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop_all)
        for server in servers:  # one owner of the signals: this function
            server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    stop_all()
    await asyncio.gather(*tasks, return_exceptions=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-media", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument(
        "--full-render",
        action="store_true",
        help="upstream's render: shadows, own body",
    )
    parser.add_argument(
        "--ports", type=int, nargs=2, default=[8011, 8012], metavar=("A", "B")
    )
    parser.add_argument("--kinematics-engine", default="AnalyticalKinematics")
    parser.add_argument("--discard-audio-output", action="store_true")
    for robot in ("a", "b"):
        parser.add_argument(f"--audio-input-device-{robot}")
        parser.add_argument(f"--audio-output-device-{robot}")
        parser.add_argument(f"--discard-audio-output-{robot}", action="store_true")
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    host = PhysicsHost(
        build_world(),
        headless=ns.headless,
        render=not (ns.no_media or ns.no_render),
        full_render=ns.full_render,
    )
    apps = []
    for index, placement in enumerate(FACING):
        name = placement.prefix.rstrip("_")
        endpoints = RobotEndpoints(
            placement=placement,
            rest_port=ns.ports[index],
            udp_port=5011 + index,
            signalling_port=8451 + index,
            camera_socket=f"/tmp/reachymini_camera_socket_{name}",
            audio_input=getattr(ns, f"audio_input_device_{name}"),
            audio_output=getattr(ns, f"audio_output_device_{name}"),
            discard_audio_output=ns.discard_audio_output
            or getattr(ns, f"discard_audio_output_{name}"),
        )
        host.add_camera(placement.prefix + "eye_camera", endpoints.udp_port)
        apps.append(
            (
                robot_app(
                    host,
                    endpoints,
                    media=not ns.no_media,
                    kinematics=ns.kinematics_engine,
                ),
                endpoints.rest_port,
            )
        )
        logger.info("robot %s: %s", name, endpoints)
    host.start()
    # Upstream's Daemon.start gives its backend 2 s to report ready: the world is
    # settled before either daemon starts.
    if not host.settled.wait(timeout=60.0):
        raise SystemExit("the physics host did not settle")
    try:
        asyncio.run(serve(apps))
    finally:
        host.stop()


if __name__ == "__main__":
    main()
