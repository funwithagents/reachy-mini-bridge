"""The bridge's test scene: a MuJoCo scene with scriptable, hidden-by-default props
(specs/sim_scene.md). Part of the shipped ``reachy_mini_bridge.testing`` package
(specs/testing_support.md) — it is test-support tooling, not something application code
depends on.

Upstream's MuJoCo daemon loads a scene by *name* from inside its own package, ships
nothing to look at, and offers no way to add or move anything in it once it runs. This
module closes all three gaps for the ``sim`` backend: a scene with one or more named
props — today a **face** (a portrait plane), room for more later — that start **hidden**
and that a test shows, places, moves and hides over HTTP while the daemon runs. Showing
the face exercises the real daemon-side pipeline (offscreen render → GStreamer → YuNet
face detector → tracking aim → head IK) and, on top of it, the bridge's attention
hand-back (specs/api.md "Attention"); hiding it again leaves the scene exactly as a test
that needs no face found it.

Three pieces, one process boundary:

- **Scene file** — ``write_test_scene`` writes an MJCF scene *outside* the upstream
  package (absolute ``<include>`` of the robot model) with one ``mocap`` body per prop
  (``FacePlane`` today: a thin box textured with a portrait photo, upright and facing the
  camera), each starting at the visibility its dataclass says — the default face starts
  **invisible**.
- **Launcher** (daemon side, ``python -m reachy_mini_bridge.testing.sim_scene``) —
  ``run_daemon`` starts the bridge's sim daemon (``reachy_mini_bridge.sim_daemon``, with
  its face-tracking corrections) on that file (``upstream_scene_name`` turns the path into
  the ``--scene`` value upstream resolves to it), with the scene's extension: a
  ``SceneDirector`` installed as MuJoCo's control callback once the model is built, so the
  mocap bodies follow commanded poses from inside the physics loop, and a small REST
  router on the daemon's own FastAPI app (``/api/sim-scene/bodies``).
- **Client** (bridge side) — ``SimSceneClient`` drives that router: list, place (with an
  optional move duration), hide, show.

``daemon.launch_command`` picks this launcher whenever ``DaemonConfig.scene`` is a path
ending in ``.xml`` (specs/daemon.md); the testing harness runs every sim it spawns on the
test scene (specs/testing_support.md). Importing this module pulls
in neither ``mujoco`` nor ``fastapi``: the daemon-side pieces import them when they run.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

from ..errors import SimSceneError
from ..sim_daemon import SimDaemonExtension, run_sim_daemon

__all__ = [
    "DEFAULT_FACE_IMAGE",
    "FACE_QUAT",
    "BodyState",
    "FacePlane",
    "SceneDirector",
    "SimSceneClient",
    "run_daemon",
    "scene_extension",
    "upstream_scene_name",
    "write_test_scene",
]

_logger = logging.getLogger(__name__)

# The bundled public-domain portrait (assets/ATTRIBUTION.md).
DEFAULT_FACE_IMAGE = Path(__file__).parent / "assets" / "face.png"

# Orientation (w, x, y, z) of a face plane: the box's thin axis points at the robot (world
# -x, the eye camera looks along +x from the neutral head pose), its width axis runs
# right-to-left as seen from the camera and its height axis points up — the portrait
# reads upright and unmirrored.
FACE_QUAT = (0.5, 0.5, -0.5, -0.5)

# Where a face sits by default: 0.45 m in front of the robot, at eye-camera height
# (~0.20 m from the neutral head), a 20 x 25 cm portrait. Rendered from the eye camera
# (fovy 80°, 1280x720) that is ~250 px tall; after the tracker's downscale to 320 px wide
# ~60 px — well above the ~40 px where the YuNet detector starts firing reliably
# (specs/sim_scene.md "Geometry").
DEFAULT_FACE_POS = (0.45, 0.0, 0.20)
DEFAULT_FACE_SIZE = (0.20, 0.25)

_HTTP_TIMEOUT_S = 5.0
_ROUTE_PREFIX = "/api/sim-scene"


# --- the scene file --------------------------------------------------------------------


@dataclass(frozen=True)
class FacePlane:
    """One portrait plane in a test scene: a ``mocap`` body named ``name`` carrying a thin
    box textured with ``image`` (a PNG; the bundled portrait when ``None``), ``size`` =
    (width, height) in metres, placed at ``pos`` (world metres) facing the robot.
    ``visible`` is the prop's state as the daemon boots — **False** by default, so a test
    scene loads with nothing in view until a test asks for it (``SimSceneClient.show``)."""

    name: str = "face"
    image: Path | None = None
    pos: tuple[float, float, float] = DEFAULT_FACE_POS
    size: tuple[float, float] = DEFAULT_FACE_SIZE
    visible: bool = False


def _mjcf_root() -> Path:
    import reachy_mini

    return Path(str(files(reachy_mini).joinpath("descriptions/reachy_mini/mjcf")))


def write_test_scene(
    out_dir: str | Path, faces: Sequence[FacePlane] = (FacePlane(),)
) -> Path:
    """Write ``<out_dir>/scene.xml``: upstream's ``empty`` scene plus one prop per entry
    of ``faces`` (only one prop shape exists today, the portrait plane). Returns the
    scene path (hand it to ``DaemonConfig.scene``).

    Each prop starts at its own ``visible`` — the default ``FacePlane`` starts hidden, so
    the scene this generates with no arguments loads with nothing in view; a test shows,
    places and hides props through ``SimSceneClient`` while the daemon runs.

    The scene includes the robot model and its assets by absolute path, so it loads from
    anywhere; textures are referenced by absolute path too. Prop names must be unique
    and non-empty.
    """
    names = [face.name for face in faces]
    if not faces or len(set(names)) != len(names) or any(not n for n in names):
        raise ValueError("face planes need unique, non-empty names")
    root = _mjcf_root()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    assets: list[str] = []
    bodies: list[str] = []
    for face in faces:
        image = Path(face.image if face.image is not None else DEFAULT_FACE_IMAGE)
        if not image.is_file():
            raise FileNotFoundError(f"face image not found: {image}")
        w, h = face.size
        x, y, z = face.pos
        assets.append(
            f'    <texture type="2d" name={quoteattr(face.name + "_tex")} '
            f"file={quoteattr(str(image.resolve()))}/>\n"
            f"    <material name={quoteattr(face.name + '_mat')} "
            f'texture={quoteattr(face.name + "_tex")} emission="1" specular="0" '
            f'shininess="0" reflectance="0"/>'
        )
        alpha = 1.0 if face.visible else 0.0
        bodies.append(
            f'    <body name={quoteattr(face.name)} mocap="true" '
            f'pos="{x:g} {y:g} {z:g}" quat="{" ".join(f"{q:g}" for q in FACE_QUAT)}">\n'
            f'      <geom name={quoteattr(face.name + "_geom")} type="box" '
            f'size="{w / 2:g} {h / 2:g} 0.001" material={quoteattr(face.name + "_mat")} '
            f'rgba="1 1 1 {alpha:g}" contype="0" conaffinity="0"/>\n'
            f"    </body>"
        )
    xml = f"""<mujoco model="scene">
  <!-- Generated by reachy_mini_bridge.testing.sim_scene (specs/sim_scene.md). -->
  <include file={quoteattr(str(root / "reachy_mini.xml"))}/>
  <compiler meshdir={quoteattr(str(root / "assets"))}/>
  <visual>
    <headlight diffuse="0.6 0.6 0.6" ambient="0.3 0.3 0.3" specular="0 0 0"/>
    <rgba haze="0.15 0.25 0.35 1"/>
    <global azimuth="160" elevation="-20" offwidth="1280" offheight="720"/>
  </visual>
  <asset>
    <texture type="skybox" builtin="gradient" rgb1="0.3 0.5 0.7" rgb2="0 0 0" width="512" height="3072"/>
    <texture type="2d" name="groundplane" builtin="checker" mark="edge" rgb1="0.2 0.3 0.4" rgb2="0.1 0.2 0.3" markrgb="0.8 0.8 0.8" width="300" height="300"/>
    <material name="groundplane" texture="groundplane" texuniform="true" texrepeat="5 5" reflectance="0"/>
{chr(10).join(assets)}
  </asset>
  <worldbody>
    <light pos="0 0 3.5" dir="0 0 -1" directional="true"/>
    <geom name="floor" size="0 0 0.05" pos="0 0 0" type="plane" material="groundplane"/>
{chr(10).join(bodies)}
  </worldbody>
</mujoco>
"""
    path = out / "scene.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def upstream_scene_name(scene_path: str | Path) -> str:
    """The ``--scene`` value that makes upstream's daemon load ``scene_path``.

    Upstream resolves ``--scene NAME`` to ``<its mjcf dir>/scenes/NAME.xml``; a path
    relative to that directory, minus the ``.xml`` suffix, therefore names any file on
    this machine. Raises ``FileNotFoundError`` for a missing file, ``ValueError`` for a
    file that does not end in ``.xml``.
    """
    path = Path(scene_path).resolve()
    if path.suffix != ".xml":
        raise ValueError(f"a sim scene must be an .xml MJCF file, got {path}")
    if not path.is_file():
        raise FileNotFoundError(f"sim scene not found: {path}")
    relative = os.path.relpath(path, _mjcf_root() / "scenes")
    return relative[: -len(".xml")]


# --- the director (daemon side) ---------------------------------------------------------


@dataclass(frozen=True)
class BodyState:
    """A scriptable body as the endpoint reports it: its commanded pose (world metres,
    quaternion w-x-y-z), whether it is shown, and whether a timed move is in flight."""

    name: str
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float]
    visible: bool
    moving: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pos": list(self.pos),
            "quat": list(self.quat),
            "visible": self.visible,
            "moving": self.moving,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BodyState:
        return cls(
            name=str(data["name"]),
            pos=_vec3(data["pos"]),
            quat=_quat(data["quat"]),
            visible=bool(data["visible"]),
            moving=bool(data["moving"]),
        )


def _vec3(value: Any) -> tuple[float, float, float]:
    try:
        x, y, z = (float(v) for v in value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"a position is three numbers, got {value!r}") from e
    return (x, y, z)


def _quat(value: Any) -> tuple[float, float, float, float]:
    try:
        w, x, y, z = (float(v) for v in value)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"a quaternion is four numbers (w x y z), got {value!r}"
        ) from e
    norm = math.sqrt(w * w + x * x + y * y + z * z)
    if norm == 0.0:
        raise ValueError("a quaternion cannot be all zeros")
    return (w / norm, x / norm, y / norm, z / norm)


@dataclass
class _Body:
    mocap_id: int
    geom_ids: list[int]
    # the commanded pose (where the body is, or is heading)
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float]
    visible: bool = True
    # a timed move: from `start_*` at `t0`, reaching `pos`/`quat` after `duration`
    start_pos: tuple[float, float, float] | None = None
    start_quat: tuple[float, float, float, float] | None = None
    t0: float = 0.0
    duration: float = 0.0
    # what the physics last saw (written by step); None until the first step
    applied: tuple[Any, Any, bool] | None = field(default=None, repr=False)


class SceneDirector:
    """Drives a scene's ``mocap`` bodies from inside MuJoCo's physics loop.

    Installed as ``mujoco.set_mjcb_control(director.step)``: the callback runs on the
    daemon's physics thread at every ``mj_step``, discovers the mocap bodies on its first
    call (``attach``), and writes each body's commanded pose into ``data.mocap_pos`` /
    ``data.mocap_quat`` — interpolating linearly over a move's ``duration`` on the wall
    clock (a move commanded with duration 0 lands on the next step) — and its visibility
    into the alpha of the body's geoms (``model.geom_rgba``; the renderer skips alpha 0,
    so a hidden face vanishes from the eye camera and the viewer alike). Commands arrive
    from the HTTP handler thread under a lock; the physics thread holds it only for the
    few floats it copies.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._bodies: dict[str, _Body] = {}
        self._attached = False

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self, model: Any, data: Any) -> None:
        """Discover the model's mocap bodies (idempotent; ``step`` calls it first).

        Each body's initial ``visible`` is read from its first geom's compiled alpha
        (the scene file sets it explicitly: ``write_test_scene`` writes ``rgba="1 1 1
        0"`` for a prop that starts hidden, ``"1 1 1 1"`` for one that starts shown), so
        the director's idea of "currently visible" matches what actually loaded, whether
        or not this director wrote the scene.
        """
        if self._attached:
            return
        # mujoco ships no stubs for these names (upstream's backend uses them the same way).
        mujoco: Any = importlib.import_module("mujoco")

        with self._lock:
            for body_id in range(int(model.nbody)):
                mocap_id = int(model.body_mocapid[body_id])
                if mocap_id < 0:
                    continue
                name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, body_id)
                if not name:
                    continue
                geom_ids = [
                    g
                    for g in range(int(model.ngeom))
                    if int(model.geom_bodyid[g]) == body_id
                ]
                visible = (
                    bool(model.geom_rgba[geom_ids[0], 3] > 0.5) if geom_ids else True
                )
                self._bodies[name] = _Body(
                    mocap_id=mocap_id,
                    geom_ids=geom_ids,
                    pos=_vec3(data.mocap_pos[mocap_id]),
                    quat=_quat(data.mocap_quat[mocap_id]),
                    visible=visible,
                )
            self._attached = True

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._bodies)

    def states(self) -> dict[str, BodyState]:
        now = self._clock()
        with self._lock:
            return {
                name: self._state(name, body, now)
                for name, body in self._bodies.items()
            }

    def state(self, name: str) -> BodyState:
        with self._lock:
            return self._state(name, self._body(name), self._clock())

    def command(
        self,
        name: str,
        *,
        pos: Any = None,
        quat: Any = None,
        duration: float = 0.0,
        visible: bool | None = None,
    ) -> BodyState:
        """Command ``name``: a new pose (reached over ``duration`` seconds, from wherever
        the body currently is), a visibility, or both. ``KeyError`` for an unknown body,
        ``ValueError`` for a malformed value."""
        if duration < 0:
            raise ValueError("duration must be >= 0")
        new_pos = None if pos is None else _vec3(pos)
        new_quat = None if quat is None else _quat(quat)
        now = self._clock()
        with self._lock:
            body = self._body(name)
            if new_pos is not None or new_quat is not None:
                current_pos, current_quat = self._pose_at(body, now)
                body.start_pos, body.start_quat = current_pos, current_quat
                body.pos = new_pos if new_pos is not None else current_pos
                body.quat = new_quat if new_quat is not None else current_quat
                body.t0, body.duration = now, float(duration)
            if visible is not None:
                body.visible = bool(visible)
            return self._state(name, body, now)

    def step(self, model: Any, data: Any) -> None:
        """MuJoCo's control callback: apply every body's pose and visibility."""
        try:
            if not self._attached:
                self.attach(model, data)
            now = self._clock()
            with self._lock:
                for body in self._bodies.values():
                    pos, quat = self._pose_at(body, now)
                    applied = (pos, quat, body.visible)
                    if body.applied == applied:
                        continue
                    data.mocap_pos[body.mocap_id] = pos
                    data.mocap_quat[body.mocap_id] = quat
                    alpha = 1.0 if body.visible else 0.0
                    for geom_id in body.geom_ids:
                        model.geom_rgba[geom_id, 3] = alpha
                    body.applied = applied
        except Exception:  # never let the physics loop die on a director bug
            _logger.exception("sim scene director failed")

    def _body(self, name: str) -> _Body:
        try:
            return self._bodies[name]
        except KeyError:
            raise KeyError(f"unknown sim scene body {name!r}") from None

    @staticmethod
    def _progress(body: _Body, now: float) -> float:
        if body.start_pos is None or body.duration <= 0.0:
            return 1.0
        return min(1.0, max(0.0, (now - body.t0) / body.duration))

    def _pose_at(
        self, body: _Body, now: float
    ) -> tuple[tuple[float, float, float], tuple[float, float, float, float]]:
        s = self._progress(body, now)
        if s >= 1.0 or body.start_pos is None or body.start_quat is None:
            return body.pos, body.quat
        pos = tuple(a + (b - a) * s for a, b in zip(body.start_pos, body.pos))
        quat = _quat(tuple(a + (b - a) * s for a, b in zip(body.start_quat, body.quat)))
        return (pos[0], pos[1], pos[2]), quat

    def _state(self, name: str, body: _Body, now: float) -> BodyState:
        return BodyState(
            name=name,
            pos=body.pos,
            quat=body.quat,
            visible=body.visible,
            moving=self._progress(body, now) < 1.0,
        )


# --- the router (daemon side) -----------------------------------------------------------


def build_router(director: SceneDirector) -> Any:
    """A FastAPI ``APIRouter`` over ``director`` (mounted at ``/api/sim-scene``):
    ``GET /bodies`` lists every scriptable body, ``POST /bodies/{name}`` commands one
    (JSON body with any of ``pos``, ``quat``, ``duration``, ``visible``) and returns
    its new state; an unknown body is 404, a malformed value 400."""
    from fastapi import APIRouter, Body, HTTPException

    router = APIRouter(tags=["sim-scene"])

    @router.get("/bodies")
    def list_bodies() -> dict[str, Any]:
        return {
            "attached": director.attached,
            "bodies": {name: s.to_dict() for name, s in director.states().items()},
        }

    payload_body = Body(default={})

    @router.post("/bodies/{name}")
    def command_body(
        name: str, payload: dict[str, Any] = payload_body
    ) -> dict[str, Any]:
        unknown = set(payload) - {"pos", "quat", "duration", "visible"}
        if unknown:
            raise HTTPException(400, f"unknown field(s): {', '.join(sorted(unknown))}")
        try:
            state = director.command(
                name,
                pos=payload.get("pos"),
                quat=payload.get("quat"),
                duration=float(payload.get("duration", 0.0)),
                visible=payload.get("visible"),
            )
        except KeyError:
            raise HTTPException(404, f"unknown body {name!r}") from None
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        return state.to_dict()

    return router


# --- the launcher (daemon side) ---------------------------------------------------------


def scene_extension(director: SceneDirector) -> SimDaemonExtension:
    """The test scene as a sim daemon extension: ``director`` installed as MuJoCo's control
    callback once the backend has built its model, the sim-scene router mounted on the
    daemon's app.

    Installed after the model is built, never before: MuJoCo's compiler calls the control
    callback on the half-built model while loading the scene, and the Python binding
    fails wrapping that model before any callback code runs, so a callback present during
    ``MjModel.from_xml_path`` makes every load fail with ``engine error: Python exception
    raised``. Per backend instance, so a daemon restart re-installs it.
    """

    def install_director(_backend: Any) -> None:
        mujoco: Any = importlib.import_module("mujoco")
        mujoco.set_mjcb_control(director.step)

    def mount_router(app: Any) -> None:
        app.include_router(build_router(director), prefix=_ROUTE_PREFIX)

    return SimDaemonExtension(on_backend=install_director, on_app=mount_router)


def run_daemon(argv: Sequence[str] | None = None) -> None:
    """Run the sim daemon on a bridge scene file, with the director and its router
    installed. ``python -m reachy_mini_bridge.testing.sim_scene --scene-path S
    [--headless] [--no-preload-datasets] [--camera ...] [upstream args...]``; under
    ``mjpython`` for the viewer.

    ``--scene-path`` becomes the upstream scene name that resolves to the file; every
    other flag goes to ``run_sim_daemon`` (specs/sim_daemon.md), which carries the
    face-tracking corrections every bridge sim gets.
    """
    parser = argparse.ArgumentParser(
        prog="python -m reachy_mini_bridge.testing.sim_scene",
        description="Run the Reachy Mini MuJoCo daemon on the bridge's test scene "
        "(specs/sim_scene.md), with the sim-scene endpoint mounted. Every other flag "
        "is the sim daemon launcher's (python -m reachy_mini_bridge.sim_daemon --help).",
        add_help=True,
    )
    parser.add_argument("--scene-path", required=True, help="an MJCF scene file (.xml)")
    args, rest = parser.parse_known_args(argv)
    scene = upstream_scene_name(args.scene_path)
    _logger.info("sim scene %s -> upstream scene name %r", args.scene_path, scene)
    run_sim_daemon(
        ["--scene", scene, *rest],
        extensions=[scene_extension(SceneDirector())],
        prog="python -m reachy_mini_bridge.testing.sim_scene",
    )


# --- the client (bridge side) -----------------------------------------------------------


class SimSceneClient:
    """Drives the scriptable bodies of a daemon launched through this module, over the
    daemon's HTTP port. Every call raises ``SimSceneError`` when the daemon is not
    reachable, does not serve the endpoint, or refuses the request."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8000,
        timeout: float = _HTTP_TIMEOUT_S,
    ) -> None:
        self._base = f"http://{host}:{port}{_ROUTE_PREFIX}"
        self._timeout = timeout

    def bodies(self) -> dict[str, BodyState]:
        """Every scriptable body, by name."""
        payload = self._request("GET", "/bodies")
        return {
            name: BodyState.from_dict(state)
            for name, state in payload["bodies"].items()
        }

    def place(
        self,
        name: str,
        pos: Sequence[float],
        *,
        quat: Sequence[float] | None = None,
        duration: float = 0.0,
    ) -> BodyState:
        """Move ``name`` to ``pos`` (world metres) — over ``duration`` seconds from where
        it is, or at once — optionally with a new orientation. Returns immediately; see
        ``wait_still``."""
        body: dict[str, Any] = {"pos": list(pos), "duration": duration}
        if quat is not None:
            body["quat"] = list(quat)
        return BodyState.from_dict(self._request("POST", f"/bodies/{name}", body))

    def show(self, name: str) -> BodyState:
        return BodyState.from_dict(
            self._request("POST", f"/bodies/{name}", {"visible": True})
        )

    def hide(self, name: str) -> BodyState:
        """Make ``name`` invisible to the camera and the viewer (it keeps its pose)."""
        return BodyState.from_dict(
            self._request("POST", f"/bodies/{name}", {"visible": False})
        )

    def wait_still(self, name: str, timeout: float = 10.0) -> BodyState:
        """Poll until ``name``'s timed move has landed (``SimSceneError`` on timeout)."""
        deadline = time.monotonic() + timeout
        while True:
            state = self.bodies()[name]
            if not state.moving:
                return state
            if time.monotonic() >= deadline:
                raise SimSceneError(
                    f"sim scene body {name!r} still moving after {timeout}s"
                )
            time.sleep(0.05)

    def _request(self, method: str, path: str, body: Any | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code == 404 and "unknown body" not in detail:
                raise SimSceneError(
                    f"no sim-scene endpoint at {self._base}: the daemon was not launched "
                    "through reachy_mini_bridge.testing.sim_scene (DaemonConfig.scene must be a "
                    ".xml path)"
                ) from e
            raise SimSceneError(
                f"sim scene request {method} {path} failed: {detail}"
            ) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SimSceneError(f"sim scene request {method} {path} failed: {e}") from e


if __name__ == "__main__":  # pragma: no cover - the daemon-side entry point
    run_daemon()
