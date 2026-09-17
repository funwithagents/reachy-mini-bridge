"""Fast-tier tests for `reachy_mini_bridge.testing.sim_scene` (specs/sim_scene.md).

Daemon-free and offline: the generated scene is loaded into MuJoCo (no rendering) to
check its geometry against the robot's eye camera and to drive the director through real
`mj_step`s via the control callback; the router runs in-process under FastAPI's test
client and behind a throwaway uvicorn server for the HTTP client. Tests that need
`mujoco` / `fastapi` skip where the sim extra is absent.
"""

from __future__ import annotations

import math
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from reachy_mini_bridge.errors import SimSceneError
from reachy_mini_bridge.testing import sim_scene
from reachy_mini_bridge.testing.sim_scene import (
    DEFAULT_FACE_IMAGE,
    BodyState,
    FacePlane,
    SceneDirector,
    SimSceneClient,
    upstream_scene_name,
    write_test_scene,
)

mujoco: Any = pytest.importorskip("mujoco", reason="sim extra (mujoco) not installed")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def scene_path(tmp_path: Path) -> Path:
    """The bridge's test scene, generated with its default props — today, one face
    plane that starts **hidden** (`FacePlane.visible` defaults to `False`)."""
    return write_test_scene(tmp_path)


@pytest.fixture
def model_and_data(scene_path: Path) -> tuple[Any, Any]:
    model = mujoco.MjModel.from_xml_path(str(scene_path))
    return model, mujoco.MjData(model)


@pytest.fixture
def control_callback() -> Iterator[None]:
    """Reset MuJoCo's process-global control callback after a test installs one."""
    yield
    mujoco.set_mjcb_control(None)


def _body_id(model: Any, name: str) -> int:
    return mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)


# --- the scene file ---


def test_face_scene_loads_with_a_mocap_face_in_the_eye_camera_view(
    model_and_data: tuple[Any, Any],
) -> None:
    """The generated scene includes the robot and puts the portrait plane — a mocap
    body, so it can be driven — upright, facing the head, inside the eye camera's frustum."""
    model, data = model_and_data
    mujoco.mj_forward(model, data)
    face = _body_id(model, "face")
    assert face >= 0 and model.body_mocapid[face] >= 0
    geom = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "face_geom")
    assert model.geom_bodyid[geom] == face
    assert model.geom_contype[geom] == 0 and model.geom_conaffinity[geom] == 0
    # `FacePlane.visible` defaults to False: the scene loads with the face out of view.
    assert model.geom_rgba[geom, 3] == 0.0

    cam = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, "eye_camera")
    assert cam >= 0, "the robot model (with its eye camera) is not included"
    rot = data.cam_xmat[cam].reshape(3, 3)
    in_cam = rot.T @ (data.xpos[face] - data.cam_xpos[cam])
    # MuJoCo cameras look down their -z axis; fovy is 80° on the 16:9 eye camera.
    assert in_cam[2] < -0.2, "the face is not in front of the camera"
    half_v = math.tan(math.radians(80 / 2))
    assert abs(in_cam[1] / -in_cam[2]) < half_v * 0.5
    assert abs(in_cam[0] / -in_cam[2]) < half_v * 16 / 9 * 0.5
    # The plane's thin axis points back at the robot (-x), its height axis up.
    plane = data.xmat[face].reshape(3, 3)
    assert np.allclose(plane[:, 2], [-1, 0, 0], atol=1e-6)
    assert np.allclose(plane[:, 1], [0, 0, 1], atol=1e-6)


def test_face_scene_floor_is_not_reflective(model_and_data: tuple[Any, Any]) -> None:
    """A reflective floor mirrors the face plane upside down, and the daemon-side
    detector can lock onto that reflection as readily as the real face (confirmed live
    on a rendered frame — specs/sim_scene.md "Tracking convergence"). Upstream's own
    scenes use `reflectance="0.2"`; the generated scene zeroes it."""
    model, _data = model_and_data
    material = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_MATERIAL, "groundplane")
    assert material >= 0
    assert model.mat_reflectance[material] == 0.0


def test_test_scene_takes_several_props_custom_images_and_starting_visibility(
    tmp_path: Path,
) -> None:
    other = tmp_path / "other.png"
    other.write_bytes(DEFAULT_FACE_IMAGE.read_bytes())
    path = write_test_scene(
        tmp_path,
        [
            FacePlane(),
            FacePlane(name="guest", image=other, pos=(0.5, 0.3, 0.2), visible=True),
        ],
    )
    model = mujoco.MjModel.from_xml_path(str(path))
    guest = _body_id(model, "guest")
    assert model.body_mocapid[guest] >= 0
    assert np.allclose(model.body_pos[guest], [0.5, 0.3, 0.2])
    assert str(other.resolve()) in path.read_text(encoding="utf-8")
    # per-prop `visible` is respected independently: the default face stays hidden,
    # the explicitly-visible guest starts shown.
    face_geom = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "face_geom")
    guest_geom = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "guest_geom")
    assert model.geom_rgba[face_geom, 3] == 0.0
    assert model.geom_rgba[guest_geom, 3] == 1.0


def test_test_scene_rejects_bad_props(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unique"):
        write_test_scene(tmp_path, [FacePlane(), FacePlane()])
    with pytest.raises(ValueError, match="unique"):
        write_test_scene(tmp_path, [])
    with pytest.raises(FileNotFoundError, match="nope.png"):
        write_test_scene(tmp_path, [FacePlane(image=tmp_path / "nope.png")])


def test_upstream_scene_name_resolves_back_to_the_file(scene_path: Path) -> None:
    """Upstream loads `<its mjcf dir>/scenes/<name>.xml`; the name must lead there."""
    name = upstream_scene_name(scene_path)
    scenes_dir = sim_scene._mjcf_root() / "scenes"
    assert (scenes_dir / f"{name}.xml").resolve() == scene_path.resolve()
    assert not name.endswith(".xml")


def test_upstream_scene_name_rejects_missing_or_non_xml_files(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        upstream_scene_name(tmp_path / "missing.xml")
    (tmp_path / "scene.txt").write_text("x")
    with pytest.raises(ValueError, match=r"\.xml"):
        upstream_scene_name(tmp_path / "scene.txt")


# --- the director through the physics loop ---


def test_director_moves_and_hides_the_face_through_mj_step(
    model_and_data: tuple[Any, Any], control_callback: None
) -> None:
    """Installed as MuJoCo's control callback, the director discovers the mocap body on
    the first step, then interpolates a timed move on its clock and toggles the geom's
    alpha — all observable on the physics state after the following step."""
    model, data = model_and_data
    clock = _Clock()
    director = SceneDirector(clock=clock)
    mujoco.set_mjcb_control(director.step)
    face = _body_id(model, "face")
    geom = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_GEOM, "face_geom")

    mujoco.mj_step(model, data)
    assert director.attached and director.names() == ["face"]
    start = director.state("face")
    assert start.pos == pytest.approx((0.45, 0.0, 0.20)) and not start.moving
    # seeded from the scene's compiled alpha (`FacePlane.visible` defaults to False).
    assert start.visible is False

    director.command("face", pos=(0.45, 0.2, 0.30), duration=2.0)
    assert director.state("face").moving
    clock.now += 1.0
    mujoco.mj_step(model, data)  # the callback writes mocap_pos ...
    mujoco.mj_step(model, data)  # ... which kinematics applies on the next step
    assert np.allclose(data.xpos[face], [0.45, 0.1, 0.25], atol=1e-6)
    clock.now += 1.5
    mujoco.mj_step(model, data)
    mujoco.mj_step(model, data)
    assert np.allclose(data.xpos[face], [0.45, 0.2, 0.30], atol=1e-6)
    assert not director.state("face").moving

    director.command("face", visible=False)
    mujoco.mj_step(model, data)
    assert model.geom_rgba[geom, 3] == 0.0
    assert director.state("face").pos == pytest.approx((0.45, 0.2, 0.30))
    director.command("face", visible=True)
    mujoco.mj_step(model, data)
    assert model.geom_rgba[geom, 3] == 1.0


def test_director_retargets_a_move_from_where_the_body_is(
    model_and_data: tuple[Any, Any], control_callback: None
) -> None:
    model, data = model_and_data
    clock = _Clock()
    director = SceneDirector(clock=clock)
    mujoco.set_mjcb_control(director.step)
    mujoco.mj_step(model, data)
    director.command("face", pos=(0.45, 0.4, 0.20), duration=4.0)
    clock.now += 2.0  # halfway: y = 0.2
    director.command("face", pos=(0.45, 0.0, 0.20), duration=1.0)
    clock.now += 0.5  # halfway back from 0.2: y = 0.1
    mujoco.mj_step(model, data)
    mujoco.mj_step(model, data)
    assert data.xpos[_body_id(model, "face")][1] == pytest.approx(0.1, abs=1e-6)


def test_director_validates_commands(model_and_data: tuple[Any, Any]) -> None:
    model, data = model_and_data
    director = SceneDirector()
    director.attach(model, data)
    with pytest.raises(KeyError, match="ghost"):
        director.command("ghost", pos=(0, 0, 0))
    with pytest.raises(ValueError, match="three numbers"):
        director.command("face", pos=(0, 0))
    with pytest.raises(ValueError, match="four numbers"):
        director.command("face", quat=(1, 0, 0))
    with pytest.raises(ValueError, match="duration"):
        director.command("face", pos=(0, 0, 0), duration=-1)
    # a quaternion is normalised on the way in
    state = director.command("face", quat=(2, 0, 0, 0))
    assert state.quat == (1.0, 0.0, 0.0, 0.0)


def test_body_state_round_trips_through_json_dicts() -> None:
    state = BodyState("face", (0.1, 0.2, 0.3), (1.0, 0.0, 0.0, 0.0), True, False)
    assert BodyState.from_dict(state.to_dict()) == state


# --- the router and the client over HTTP ---


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def served_director(
    model_and_data: tuple[Any, Any],
) -> Iterator[tuple[SceneDirector, int]]:
    """The sim-scene router mounted on a FastAPI app served by uvicorn in a thread."""
    fastapi = pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    model, data = model_and_data
    director = SceneDirector()
    director.attach(model, data)
    app = fastapi.FastAPI()
    app.include_router(sim_scene.build_router(director), prefix="/api/sim-scene")
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield director, port
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def test_client_drives_the_router(served_director: tuple[SceneDirector, int]) -> None:
    director, port = served_director
    client = SimSceneClient("127.0.0.1", port)
    bodies = client.bodies()
    assert set(bodies) == {"face"} and not bodies["face"].visible

    assert client.show("face").visible
    placed = client.place("face", (0.5, -0.1, 0.25), duration=0.3)
    assert placed.moving and placed.pos == (0.5, -0.1, 0.25)
    still = client.wait_still("face", timeout=5.0)
    assert not still.moving
    assert director.state("face").pos == (0.5, -0.1, 0.25)

    assert not client.hide("face").visible
    assert not director.state("face").visible
    assert client.show("face").visible

    with pytest.raises(SimSceneError, match="ghost"):
        client.place("ghost", (0, 0, 0))
    with pytest.raises(SimSceneError, match="three numbers"):
        client.place("face", (0, 0))
    with pytest.raises(SimSceneError, match="unknown field"):
        client._request("POST", "/bodies/face", {"colour": "red"})


def test_client_reports_a_daemon_without_the_endpoint() -> None:
    fastapi = pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            fastapi.FastAPI(), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.02)
    try:
        with pytest.raises(SimSceneError, match="not launched through"):
            SimSceneClient("127.0.0.1", port).bodies()
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def test_client_reports_an_unreachable_daemon() -> None:
    with pytest.raises(SimSceneError, match="failed"):
        SimSceneClient("127.0.0.1", _free_port(), timeout=0.5).bodies()


# --- the launcher ---


@pytest.fixture
def upstream_daemon_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """The launcher patches upstream's daemon module globals (the backend class,
    `create_app`, the tracker's intrinsics): undo them after the test."""
    from reachy_mini.daemon import daemon as upstream_daemon
    from reachy_mini.daemon.app import main as upstream_main
    from reachy_mini.vision import face_tracking

    from reachy_mini_bridge import sim_daemon

    monkeypatch.setattr(upstream_daemon, "MujocoBackend", upstream_daemon.MujocoBackend)
    monkeypatch.setattr(upstream_main, "create_app", upstream_main.create_app)
    monkeypatch.setattr(
        face_tracking, "intrinsics_for_size", face_tracking.intrinsics_for_size
    )
    monkeypatch.setattr(sim_daemon._TrackerCamera, "hfov_deg", None)


def test_run_daemon_rewrites_argv_installs_the_director_and_mounts_the_router(
    scene_path: Path, monkeypatch: pytest.MonkeyPatch, upstream_daemon_globals: None
) -> None:
    """`run_daemon` runs the sim daemon launcher with `--scene <name>` (name resolving to
    the file), forwards every other flag, and its extension installs the control callback
    once the backend has a model and mounts the sim-scene routes on upstream's own app."""
    fastapi = pytest.importorskip("fastapi")
    from reachy_mini.daemon import daemon as upstream_daemon
    from reachy_mini.daemon.app import main as upstream_main

    original_backend = upstream_daemon.MujocoBackend
    installed: list[Any] = []
    monkeypatch.setattr(mujoco, "set_mjcb_control", installed.append)
    seen_argv: list[list[str]] = []
    monkeypatch.setattr(upstream_main, "main", lambda: seen_argv.append(list(sys.argv)))
    monkeypatch.setattr(
        upstream_main, "create_app", lambda *a, **kw: fastapi.FastAPI(title="upstream")
    )
    monkeypatch.setattr(sys, "argv", ["untouched"])

    sim_scene.run_daemon(
        [
            "--scene-path",
            str(scene_path),
            "--headless",
            "--no-preload-datasets",
            "--log-level",
            "DEBUG",
        ]
    )

    assert len(seen_argv) == 1
    argv = seen_argv[0]
    assert argv[1:3] == ["--sim", "--scene"]
    assert (
        sim_scene._mjcf_root() / "scenes" / f"{argv[3]}.xml"
    ).resolve() == scene_path.resolve()
    assert argv[4:] == ["--headless", "--no-preload-datasets", "--log-level", "DEBUG"]
    # The director is installed by the backend the daemon will construct, after the
    # model is built — never before (a callback present during the load breaks it).
    assert installed == []
    backend_class = upstream_daemon.MujocoBackend
    assert backend_class is not original_backend
    assert issubclass(backend_class, original_backend)
    backend = backend_class(scene=argv[3], headless=True, use_audio=False)
    assert backend.model.nbody > 0
    assert len(installed) == 1 and callable(installed[0])
    upstream_args: Any = SimpleNamespace()
    app = upstream_main.create_app(upstream_args, None)
    assert app.title == "upstream"
    testclient = pytest.importorskip("fastapi.testclient")
    response = testclient.TestClient(app).get("/api/sim-scene/bodies")
    assert response.status_code == 200
    assert response.json() == {"attached": False, "bodies": {}}


def test_scene_extension_installs_the_director_once_the_model_exists(
    scene_path: Path, control_callback: None, upstream_daemon_globals: None
) -> None:
    """The reason the install waits for the model: with a control callback already
    installed, upstream's backend cannot even load the scene."""
    from reachy_mini.daemon.backend.mujoco.backend import MujocoBackend

    from reachy_mini_bridge.sim_daemon import corrected_backend

    name = upstream_scene_name(scene_path)
    mujoco.set_mjcb_control(lambda m, d: None)
    with pytest.raises(ValueError, match="Python exception raised"):
        MujocoBackend(scene=name, headless=True, use_audio=False)
    mujoco.set_mjcb_control(None)

    director = SceneDirector()
    backend = corrected_backend(
        MujocoBackend, extensions=[sim_scene.scene_extension(director)]
    )(scene=name, headless=True, use_audio=False)
    assert mujoco.get_mjcb_control() is not None
    mujoco.mj_step(backend.model, backend.data)
    assert director.attached and director.names() == ["face"]


def test_run_daemon_viewer_keeps_upstream_headfull_and_passes_the_camera(
    scene_path: Path, monkeypatch: pytest.MonkeyPatch, upstream_daemon_globals: None
) -> None:
    from reachy_mini.daemon.app import main as upstream_main

    seen_argv: list[list[str]] = []
    monkeypatch.setattr(upstream_main, "main", lambda: seen_argv.append(list(sys.argv)))
    monkeypatch.setattr(upstream_main, "create_app", lambda *a, **kw: None)
    monkeypatch.setattr(sys, "argv", ["untouched"])
    sim_scene.run_daemon(["--scene-path", str(scene_path), "--camera", "webcam"])
    assert "--headless" not in seen_argv[0]
    assert seen_argv[0][-1] == "--preload-datasets"
    assert "--camera" not in seen_argv[0]  # the launcher's own flag, not upstream's


def test_run_daemon_refuses_a_missing_scene(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sim_scene.run_daemon(["--scene-path", str(tmp_path / "missing.xml")])
