"""The bridge's test scene: a MuJoCo scene with scriptable, hidden-by-default props
(specs/testing/sim_scene.md). Part of the shipped ``reachy_mini_bridge.testing`` package
(specs/testing/testing_support.md) — it is test-support tooling, not something application code
depends on.

Upstream's MuJoCo daemon loads a scene by *name* from inside its own package, ships
nothing to look at, and offers no way to add or move anything in it once it runs. This
module closes all three gaps for the ``sim`` backend: a scene with named props — images
on planes (``ImagePlane``): by default a pool of **faces** (``face_1`` … ``face_4``, one
person each), plus whatever kinds a consumer adds (a hand, an object) — that start
**hidden** and that a test spawns, moves and despawns over HTTP while the daemon runs. Showing the face exercises the real pipeline (the daemon's offscreen render → GStreamer → the
bridge's camera feed → its face detector → its head tracker's aim → the motion loop →
head IK) and, on top of it, the bridge's attention hand-back (specs/core/bridge.md "Attention"); hiding it again leaves the scene exactly as a test
that needs no face found it.

Three pieces, one process boundary:

- **Scene file** — ``write_test_scene`` writes an MJCF scene *outside* the upstream
  package (absolute ``<include>`` of the robot model) with one ``mocap`` body per prop
  (an ``ImagePlane``: a thin box textured with a PNG, upright and facing the camera),
  each starting at the visibility its dataclass says — **invisible** by default.
- **Launcher** (daemon side, ``python -m reachy_mini_bridge.testing.sim_scene``) —
  ``run_daemon`` starts the bridge's sim daemon (``reachy_mini_bridge.sim_daemon``, with
  its camera source and viewer displays) on that file (``upstream_scene_name`` turns the path into
  the ``--scene`` value upstream resolves to it), with the scene's extension: a
  ``SceneDirector`` installed as MuJoCo's control callback once the model is built, so the
  mocap bodies follow commanded poses from inside the physics loop, and a small REST
  router on the daemon's own FastAPI app (``/api/sim/inject/bodies``).
- **Client** (bridge side) — ``SimSceneClient`` drives that router: spawn a free prop of
  the pool, despawn it, clear the scene; per body list, place (with an optional move
  duration), hide, show.

``daemon.launch_command`` picks this launcher whenever ``DaemonConfig.scene`` is a path
ending in ``.xml`` (specs/daemon/daemon.md); the testing harness runs every sim it spawns on the
test scene (specs/testing/testing_support.md). Importing this module pulls
in neither ``mujoco`` nor ``fastapi``: the daemon-side pieces import them when they run.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
import re
import struct
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
    "DEFAULT_FACE_POS",
    "DEFAULT_FACE_SIZE",
    "FACE_HEIGHT_M",
    "FACE_IMAGES",
    "FACE_POOL_SIZE",
    "FACE_QUAT",
    "LOOKING_DOWN_FACE",
    "TURNED_FACE",
    "BodyState",
    "ImagePlane",
    "SceneDirector",
    "SimSceneClient",
    "face_pool",
    "image_pool",
    "png_size",
    "run_daemon",
    "scene_extension",
    "upstream_scene_name",
    "write_test_scene",
]

_logger = logging.getLogger(__name__)

_ASSETS = Path(__file__).parent / "assets"

# The bundled public-domain portraits (assets/ATTRIBUTION.md), one person each, in pool
# order: the pool's ``face_<n>`` shows the n-th (specs/testing/sim_scene.md "A pool of
# portraits"). Two frontal faces, then a head turned ~30° toward the image's right and a
# head tilted down — every one detected over the tracking tests' range.
FACE_IMAGES = (
    _ASSETS / "armstrong.png",
    _ASSETS / "chawla.png",
    _ASSETS / "schmid.png",
    _ASSETS / "malenchenko.png",
)
# The pool's first portrait, a frontal face.
DEFAULT_FACE_IMAGE = FACE_IMAGES[0]

# Orientation (w, x, y, z) of every prop's plane: the box's thin axis points at the robot (world
# -x, the eye camera looks along +x from the neutral head pose), its width axis runs
# right-to-left as seen from the camera and its height axis points up — the portrait
# reads upright and unmirrored.
FACE_QUAT = (0.5, 0.5, -0.5, -0.5)

# Where a prop sits by default: 0.45 m in front of the robot, at eye-camera height
# (~0.20 m from the neutral head). A portrait is life size, 36 cm tall (0.288 m wide for
# the bundled 320x400 photos — the width follows the image): the pool's framing
# puts the face box at 0.49 of its height, so YuNet's box spans FACE_BOX_HEIGHT_M (0.18 m)
# and a portrait's distance_m reads its true distance. Rendered from the eye camera
# (fovy 80°, 1280x720) the plane is ~340 px tall, its face ~170 px; after the detector's
# stride to 640 px wide ~85 px — well above the ~40 px where YuNet starts firing reliably
# (specs/testing/sim_scene.md "Geometry").
DEFAULT_FACE_POS = (0.45, 0.0, 0.20)
FACE_HEIGHT_M = 0.36

# The test scene's pool of hidden portraits (specs/testing/sim_scene.md "A pool of
# portraits"): MuJoCo compiles a scene at load, so the props a test may spawn are written
# into the file up front. One prop per person — no two props show the same face, so a
# test's users never look alike; a portrait costs a body and a box.
FACE_POOL_SIZE = len(FACE_IMAGES)
# The pool's props whose person is not looking at the robot, for the tests that need one.
TURNED_FACE = "face_3"
LOOKING_DOWN_FACE = "face_4"

# A prop's material is named after its image, so the director reads a body's image back
# from the compiled model (specs/testing/sim_scene.md "A pool of portraits").
_PROP_MATERIAL_PREFIX = "prop_"

_HTTP_TIMEOUT_S = 5.0
_ROUTE_PREFIX = "/api/sim/inject"
# The router's own paths under ``bodies/``: a prop cannot carry these names.
_RESERVED_NAMES = frozenset({"spawn", "clear"})


# --- the scene file --------------------------------------------------------------------


# The 8-byte signature every PNG file opens with, then the IHDR chunk: its length (13),
# its type, and the image's width and height as big-endian 32-bit integers.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path: str | Path) -> tuple[int, int]:
    """The (width, height) in pixels of the PNG at ``path``, read from its header.

    MuJoCo decodes PNG textures and nothing else — any other file fails the scene's
    compile inside the daemon (specs/testing/sim_scene.md "A prop is an image on a
    plane"), so the writer checks here, where the caller is. ``FileNotFoundError`` for a
    missing file, ``ValueError`` naming the file for anything that is not a PNG."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"prop image not found: {path}")
    with path.open("rb") as f:
        header = f.read(24)
    if len(header) < 24 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise ValueError(
            f"prop image {path} is not a PNG: MuJoCo reads PNG textures only — "
            "convert it to PNG"
        )
    width, height = struct.unpack(">II", header[16:24])
    if width == 0 or height == 0:
        raise ValueError(f"prop image {path} has no pixels ({width}x{height})")
    return width, height


@dataclass(frozen=True)
class ImagePlane:
    """One prop of a test scene: a ``mocap`` body named ``name`` carrying a thin box
    textured with ``image`` (a PNG), placed at ``pos`` (world metres) facing the robot
    (specs/testing/sim_scene.md "A prop is an image on a plane"). Exactly one of
    ``height`` and ``width`` (metres) is given; the other follows from the image's
    aspect ratio, so a photo is never stretched. ``visible`` is the prop's state as the
    daemon boots — **False** by default, so a scene loads with nothing in view until a
    test spawns it."""

    name: str
    image: str | Path
    pos: tuple[float, float, float] = DEFAULT_FACE_POS
    height: float | None = None
    width: float | None = None
    visible: bool = False

    def size(self, aspect: float) -> tuple[float, float]:
        """The plane's (width, height) in metres for an image of ``aspect`` = its width
        over its height. ``ValueError`` unless exactly one dimension is given, as a
        positive finite number."""
        given = [d for d in (self.height, self.width) if d is not None]
        if len(given) != 1:
            raise ValueError(
                f"prop {self.name!r}: give exactly one of height and width (metres), "
                "the other follows from the image"
            )
        dimension = given[0]
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int | float)
            or not math.isfinite(dimension)
            or dimension <= 0
        ):
            raise ValueError(
                f"prop {self.name!r}: a size is a positive number of metres, "
                f"got {dimension!r}"
            )
        if self.height is not None:
            return (self.height * aspect, self.height)
        return (dimension, dimension / aspect)


def _face_size() -> tuple[float, float]:
    width, height = png_size(DEFAULT_FACE_IMAGE)
    return (FACE_HEIGHT_M * width / height, FACE_HEIGHT_M)


# A bundled portrait's plane (width, height) in metres: every one shares one framing.
DEFAULT_FACE_SIZE = _face_size()


def _mjcf_root() -> Path:
    import reachy_mini

    return Path(str(files(reachy_mini).joinpath("descriptions/reachy_mini/mjcf")))


def image_pool(
    kind: str,
    images: Sequence[str | Path],
    *,
    count: int | None = None,
    height: float | None = None,
    width: float | None = None,
) -> tuple[ImagePlane, ...]:
    """The props of a pool (specs/testing/sim_scene.md "A prop is an image on a plane"):
    ``<kind>_1`` … ``<kind>_<count>``, hidden, parked at ``DEFAULT_FACE_POS``, their
    images assigned in order — one prop per image when ``count`` is ``None``,
    round-robin when it is larger — each sized by the one dimension given. A test spawns
    them with ``SimSceneClient.spawn(kind=kind)``, or by name."""
    if not kind or _kind_of(f"{kind}_1") != kind or _kind_of(kind) != kind:
        raise ValueError(
            f"a pool's kind is a non-empty name not ending in _<n>, got {kind!r}"
        )
    paths = [Path(i) for i in images]
    if not paths:
        raise ValueError(f"a {kind} pool needs at least one image")
    count = len(paths) if count is None else count
    if count < 1:
        raise ValueError(f"a {kind} pool needs at least one prop, got {count}")
    return tuple(
        ImagePlane(
            name=f"{kind}_{i + 1}",
            image=paths[i % len(paths)],
            height=height,
            width=width,
        )
        for i in range(count)
    )


def face_pool(
    count: int | None = None, images: Sequence[str | Path] | None = None
) -> tuple[ImagePlane, ...]:
    """The portraits of a pool (specs/testing/sim_scene.md "A pool of portraits"):
    ``face_1`` … ``face_<count>``, hidden, parked at ``DEFAULT_FACE_POS``, life size
    (``FACE_HEIGHT_M`` tall), their images assigned in order from ``images`` (the
    bundled ``FACE_IMAGES`` when ``None``) — one prop per image by default, round-robin
    when ``count`` is larger. A test spawns them with ``SimSceneClient.spawn``, by name
    for a given person."""
    return image_pool(
        "face",
        list(FACE_IMAGES) if images is None else images,
        count=count,
        height=FACE_HEIGHT_M,
    )


def write_test_scene(
    out_dir: str | Path, props: Sequence[ImagePlane] | None = None
) -> Path:
    """Write ``<out_dir>/scene.xml``: upstream's ``empty`` scene plus one prop per entry
    of ``props`` — by default ``face_pool()``, four hidden portraits, one person each.
    Returns the scene path (hand it to ``DaemonConfig.scene``).

    Each prop starts at its own ``visible`` — a pool starts hidden, so the scene this
    generates with no arguments loads with nothing in view; a test spawns, places and
    despawns props through ``SimSceneClient`` while the daemon runs.

    The scene includes the robot model and its assets by absolute path, so it loads from
    anywhere; textures are referenced by absolute path too, one texture and one material
    (``prop_<image stem>``) per distinct image, shared by every prop showing it. Prop
    names must be unique and non-empty, and neither ``spawn`` nor ``clear`` (the inject
    router's own paths under ``bodies/``); every image must be a PNG
    (``FileNotFoundError`` / ``ValueError`` here, not a daemon that fails to load).
    """
    if props is None:
        props = face_pool()
    names = [prop.name for prop in props]
    if not props or len(set(names)) != len(names) or any(not n for n in names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(
            "props need unique, non-empty names"
            + (f" (repeated: {', '.join(duplicates)})" if duplicates else "")
        )
    reserved = sorted(_RESERVED_NAMES.intersection(names))
    if reserved:
        raise ValueError(
            f"prop name(s) {', '.join(reserved)} are reserved: the inject router "
            "serves bodies/spawn and bodies/clear"
        )
    root = _mjcf_root()
    out = Path(out_dir)
    assets: list[str] = []
    bodies: list[str] = []
    materials: dict[Path, str] = {}  # resolved image -> its material's name
    for prop in props:
        image = Path(prop.image)
        pixels_w, pixels_h = png_size(image)
        w, h = prop.size(pixels_w / pixels_h)
        image = image.resolve()
        material = materials.get(image)
        if material is None:
            if _SAME_STEM_SUFFIX.search(image.stem):
                raise ValueError(
                    f"prop image {image.name!r}: a stem ending in '__<number>' cannot "
                    "be carried by the scene (that suffix marks two files of one stem)"
                )
            material = _PROP_MATERIAL_PREFIX + image.stem
            taken = set(materials.values())
            suffix = 2
            while material in taken:  # two files with the same stem
                material = f"{_PROP_MATERIAL_PREFIX}{image.stem}__{suffix}"
                suffix += 1
            materials[image] = material
            assets.append(
                f'    <texture type="2d" name={quoteattr(material + "_tex")} '
                f"file={quoteattr(str(image))}/>\n"
                f"    <material name={quoteattr(material)} "
                f'texture={quoteattr(material + "_tex")} emission="1" specular="0" '
                f'shininess="0" reflectance="0"/>'
            )
        x, y, z = prop.pos
        alpha = 1.0 if prop.visible else 0.0
        bodies.append(
            f'    <body name={quoteattr(prop.name)} mocap="true" '
            f'pos="{x:g} {y:g} {z:g}" quat="{" ".join(f"{q:g}" for q in FACE_QUAT)}">\n'
            f'      <geom name={quoteattr(prop.name + "_geom")} type="box" '
            f'size="{w / 2:g} {h / 2:g} 0.001" material={quoteattr(material)} '
            f'rgba="1 1 1 {alpha:g}" contype="0" conaffinity="0"/>\n'
            f"    </body>"
        )
    out.mkdir(parents=True, exist_ok=True)
    xml = f"""<mujoco model="scene">
  <!-- Generated by reachy_mini_bridge.testing.sim_scene (specs/testing/sim_scene.md). -->
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
    this machine — on Windows, any file on the drive upstream is installed on. Raises
    ``FileNotFoundError`` for a missing file, ``ValueError`` for a file that does not end
    in ``.xml`` or sits on another drive.
    """
    path = Path(scene_path).resolve()
    if path.suffix != ".xml":
        raise ValueError(f"a sim scene must be an .xml MJCF file, got {path}")
    if not path.is_file():
        raise FileNotFoundError(f"sim scene not found: {path}")
    scenes = _mjcf_root() / "scenes"
    if path.drive.lower() != scenes.drive.lower():
        raise ValueError(
            f"a sim scene must be on the drive reachy_mini is installed on "
            f"({scenes.drive}), got {path} — upstream names a scene relative to its own "
            f"directory; for the test scene, point TEMP at a folder on {scenes.drive}"
        )
    relative = os.path.relpath(path, scenes)
    return relative[: -len(".xml")]


# --- the director (daemon side) ---------------------------------------------------------


@dataclass(frozen=True)
class BodyState:
    """A scriptable body as the endpoint reports it: its kind (``face``) and image (the
    stem of its portrait's file, ``None`` without one), its commanded pose (world metres,
    quaternion w-x-y-z), whether it is shown, and whether a timed move is in flight."""

    name: str
    pos: tuple[float, float, float]
    quat: tuple[float, float, float, float]
    visible: bool
    moving: bool
    kind: str = ""
    image: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "image": self.image,
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
            kind=str(data.get("kind", "")),
            image=None if data.get("image") is None else str(data["image"]),
        )


def _kind_of(name: str) -> str:
    """A prop's kind: its name up to a trailing ``_<n>`` (``face_3`` -> ``face``)."""
    head, sep, tail = name.rpartition("_")
    return head if sep and head and tail.isdigit() else name


# The suffix ``write_test_scene`` adds to keep two same-stem files apart — two
# underscores and a number, which a stem may not end in itself, so a stem always reads
# back whole (``person_1.png`` is the image ``person_1``).
_SAME_STEM_SUFFIX = re.compile(r"__\d+$")


def _image_of(material: str | None) -> str | None:
    """A prop's image: its material's name without the ``prop_`` prefix and
    without the ``__<n>`` suffix ``write_test_scene`` adds for two same-stem files."""
    if not material or not material.startswith(_PROP_MATERIAL_PREFIX):
        return None
    return _SAME_STEM_SUFFIX.sub("", material[len(_PROP_MATERIAL_PREFIX) :])


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
    kind: str
    image: str | None
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

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
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
                material = None
                if geom_ids and int(model.geom_matid[geom_ids[0]]) >= 0:
                    material = mujoco.mj_id2name(
                        model,
                        mujoco.mjtObj.mjOBJ_MATERIAL,
                        int(model.geom_matid[geom_ids[0]]),
                    )
                self._bodies[name] = _Body(
                    mocap_id=mocap_id,
                    geom_ids=geom_ids,
                    kind=_kind_of(name),
                    image=_image_of(material),
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

    def spawn(
        self,
        pos: Any,
        quat: Any = None,
        *,
        name: str | None = None,
        kind: str = "face",
        duration: float = 0.0,
    ) -> BodyState:
        """Show a prop of the pool at ``pos`` — ``name`` when given (a given person),
        else the lowest-numbered hidden prop of ``kind`` — placed over ``duration``
        seconds from where it is parked, or at once, in one command
        (specs/testing/sim_scene.md "A pool of portraits"). ``KeyError`` for an unknown
        ``name``; ``LookupError`` when that prop is already shown, or when no prop of
        ``kind`` is free; ``ValueError`` for a malformed value."""
        if duration < 0:
            raise ValueError("duration must be >= 0")
        new_pos = _vec3(pos)
        new_quat = None if quat is None else _quat(quat)
        now = self._clock()
        with self._lock:
            if name is not None:
                body = self._body(name)
                if body.visible:
                    raise LookupError(
                        f"{name} is already shown — despawn it before spawning it again"
                    )
            else:
                of_kind = sorted(
                    (
                        (prop, body)
                        for prop, body in self._bodies.items()
                        if body.kind == kind
                    ),
                    key=lambda item: _pool_order(item[0]),
                )
                free = [(prop, body) for prop, body in of_kind if not body.visible]
                if not free:
                    raise LookupError(
                        f"no free {kind} prop: the scene's pool holds {len(of_kind)} "
                        f"{kind} prop(s), all shown — despawn one, or write a bigger "
                        "pool (image_pool(count=...))"
                    )
                name, body = free[0]
            current_pos, current_quat = self._pose_at(body, now)
            body.start_pos, body.start_quat = current_pos, current_quat
            body.pos = new_pos
            body.quat = new_quat if new_quat is not None else current_quat
            body.t0, body.duration = now, float(duration)
            body.visible = True
            return self._state(name, body, now)

    def clear(self) -> dict[str, BodyState]:
        """Hide every prop (their poses kept): the scene with nobody in view."""
        now = self._clock()
        with self._lock:
            for body in self._bodies.values():
                body.visible = False
            return {
                name: self._state(name, body, now)
                for name, body in self._bodies.items()
            }

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
            kind=body.kind,
            image=body.image,
        )


def _pool_order(name: str) -> tuple[int, str]:
    """``face_2`` before ``face_10``: a pooled prop's number, then its name."""
    _, _, tail = name.rpartition("_")
    return (int(tail) if tail.isdigit() else 0, name)


# --- the router (daemon side) -----------------------------------------------------------


def build_router(director: SceneDirector) -> Any:
    """A FastAPI ``APIRouter`` over ``director`` (mounted at ``/api/sim/inject``): every
    route acts on bodies, so every route is under ``bodies``. ``GET /bodies`` lists every
    scriptable body, ``POST /bodies/{name}`` commands one (JSON body with any of ``pos``,
    ``quat``, ``duration``, ``visible``) and returns its new state; an unknown body is
    404, a malformed value 400. ``POST /bodies/spawn`` shows a free prop of a pool (409
    when none is free), ``POST /bodies/clear`` hides them all — the two fixed paths are
    registered before ``{name}``, and a scene refuses props with those names."""
    from fastapi import APIRouter, Body, HTTPException

    router = APIRouter(tags=["sim-inject"])

    @router.get("/bodies")
    def list_bodies() -> dict[str, Any]:
        return {
            "attached": director.attached,
            "bodies": {name: s.to_dict() for name, s in director.states().items()},
        }

    spawn_body = Body(...)

    @router.post("/bodies/spawn")
    def spawn(payload: dict[str, Any] = spawn_body) -> dict[str, Any]:
        unknown = set(payload) - {"pos", "quat", "duration", "kind", "name"}
        if unknown:
            raise HTTPException(400, f"unknown field(s): {', '.join(sorted(unknown))}")
        if "pos" not in payload:
            raise HTTPException(400, "spawn needs a pos")
        name = payload.get("name")
        try:
            state = director.spawn(
                payload["pos"],
                payload.get("quat"),
                name=None if name is None else str(name),
                kind=str(payload.get("kind", "face")),
                duration=float(payload.get("duration", 0.0)),
            )
        except KeyError:
            raise HTTPException(404, f"unknown body {name!r}") from None
        except LookupError as e:
            raise HTTPException(409, str(e.args[0] if e.args else e)) from None
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        return state.to_dict()

    @router.post("/bodies/clear")
    def clear() -> dict[str, Any]:
        return {"bodies": {name: s.to_dict() for name, s in director.clear().items()}}

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
    callback once the backend has built its model, the inject router mounted on the
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
    other flag goes to ``run_sim_daemon`` (specs/daemon/sim_daemon.md), which adds the
    camera source and the viewer displays every bridge sim gets.
    """
    parser = argparse.ArgumentParser(
        prog="python -m reachy_mini_bridge.testing.sim_scene",
        description="Run the Reachy Mini MuJoCo daemon on the bridge's test scene "
        "(specs/testing/sim_scene.md), with the inject endpoint mounted. Every other flag "
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

    def spawn(
        self,
        pos: Sequence[float],
        quat: Sequence[float] | None = None,
        *,
        name: str | None = None,
        kind: str = "face",
        duration: float = 0.0,
    ) -> str:
        """Show a prop of the scene's pool at ``pos`` (world metres) — ``name`` when
        given, the same person every time (``face_2``, ``TURNED_FACE``), else the
        lowest-numbered hidden one of ``kind`` — and return its name, the handle to
        ``place`` or ``despawn`` it (specs/testing/sim_scene.md "A pool of portraits").
        ``SimSceneError`` when that prop is unknown or already shown, or the pool is
        exhausted."""
        body: dict[str, Any] = {"pos": list(pos), "duration": duration, "kind": kind}
        if quat is not None:
            body["quat"] = list(quat)
        if name is not None:
            body["name"] = name
        return BodyState.from_dict(self._request("POST", "/bodies/spawn", body)).name

    def despawn(self, name: str) -> BodyState:
        """Hide ``name`` and return it to the pool (it keeps its pose)."""
        return self.hide(name)

    def clear(self) -> dict[str, BodyState]:
        """Hide every prop: nobody in view."""
        payload = self._request("POST", "/bodies/clear", {})
        return {
            name: BodyState.from_dict(state)
            for name, state in payload["bodies"].items()
        }

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
        deadline = time.perf_counter() + timeout
        while True:
            state = self.bodies()[name]
            if not state.moving:
                return state
            if time.perf_counter() >= deadline:
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
                    f"no inject endpoint at {self._base}: the daemon was not launched "
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
