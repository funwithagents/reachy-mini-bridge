"""Setting the bridge up from an application, as data (specs/core/setup.md).

Seven functions over the bridge's config document (the object
``ReachyMiniConfig.from_dict`` takes): what the config looks like (``config_schema``),
which extras a config needs (``required_extras``), what is wrong in it
(``check_config``), what this machine is missing to run it (``check_setup``: the
extras, the scene file, the models and the media registry a session would otherwise
get itself, the voice's items), preparing one of them (``prepare``), the choices of a named list (``options``), and one light, real run
(``probe``). They implement version 1 of the module contract and exchange
JSON-compatible data only.

The ``tts`` block is a tts-engine engine block, and tts-engine implements the same
contract: each function delegates that block to ``tts_engine.setup``, its pointers
prefixed with ``/tts``. The bridge's other blocks are raw JSON, checked by the bridge's
own loader. Nothing here is imported by the bridge itself.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import io
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import tts_engine.setup as tts_setup

from .config import BACKENDS, ReachyMiniConfig, resolve_scene
from .errors import BridgeError
from .fields import Field, fields_schema

__all__ = [
    "check_config",
    "check_setup",
    "config_schema",
    "options",
    "prepare",
    "probe",
    "required_extras",
]

_DISTRIBUTION = "reachy-mini-bridge"

# The one declared field (specs/core/setup.md "Fields declared as data").
_FIELDS = (
    Field(
        "backend",
        "string",
        "Which robot the bridge drives: the robot itself ('real': a Lite over USB, or "
        "a wireless robot on the network), the MuJoCo simulator ('sim'), or an "
        "offline stand-in that needs nothing ('fake').",
        default="real",
        choices=BACKENDS,
    ),
)

# The bridge's blocks, edited as raw JSON and checked by its loader: (title,
# description, advanced).
_RAW_BLOCKS: dict[str, tuple[str, str, bool]] = {
    "robot": (
        "Connection",
        (
            "The connection options of upstream's ReachyMini client (host, port, "
            "connection_mode, media_backend, timeout, ...), as JSON."
        ),
        True,
    ),
    "daemon": (
        "Daemon",
        (
            "How the bridge brings up the robot's daemon: spawn (never, auto, always), "
            "headless, scene, kinematics_engine, the sim's camera and audio devices, the "
            "viewer's displays, preload_datasets, startup_timeout; as JSON."
        ),
        True,
    ),
    "audio": (
        "Audio processor",
        (
            "The XVF3800 audio processor's profile applied when a session starts "
            "(xvf3800), as JSON."
        ),
        True,
    ),
    "face_detection": (
        "Face detection",
        (
            "Which face detector runs on the camera (detector: null or 'yunet'), whether "
            "it runs from the start (enabled), and its cost (width, score_threshold, "
            "target_fps); as JSON."
        ),
        False,
    ),
    "motion": (
        "Behaviour at rest",
        (
            "What the robot does when nothing else commands it: presence, the idle move, "
            "wobbling while it talks, head tracking and its posture, the base following "
            "the person, the tilt limit; as JSON."
        ),
        False,
    ),
}

# The bridge's own extras: the imports each provides, tried by check_setup.
_EXTRA_IMPORTS = {"sim": ("mujoco",), "placo": ("placo",)}
_EXTRA_WHAT = {"sim": "MuJoCo simulator", "placo": "Placo kinematics engine"}

_YUNET = "weights:yunet"
_EMOTIONS = "dataset:emotions"
_GSTREAMER = "media:gstreamer-registry"


# --- config_schema ---------------------------------------------------------------


def _tts_schema() -> dict[str, Any]:
    """tts-engine's schema of the engine block, without `player`: the bridge plays
    through the robot's speaker, never through tts-engine's player."""
    schema = copy.deepcopy(tts_setup.config_schema()["schema"])
    schema["properties"].pop("player", None)
    return {
        "title": "Text to speech",
        "description": "The robot's voice: what turns text into speech, played on "
        "the robot's speaker. Off: the robot has no voice.",
        **schema,
    }


def config_schema() -> dict[str, Any]:
    properties: dict[str, Any] = dict(fields_schema(_FIELDS)["properties"])
    for name in ("robot", "daemon", "audio"):
        properties[name] = _raw_block(name)
    properties["tts"] = _tts_schema()
    for name in ("face_detection", "motion"):
        properties[name] = _raw_block(name)
    return {
        "contract": 1,
        "distribution": _DISTRIBUTION,
        "title": "Reachy Mini",
        "schema": {"type": "object", "properties": properties},
    }


def _raw_block(name: str) -> dict[str, Any]:
    title, description, advanced = _RAW_BLOCKS[name]
    block: dict[str, Any] = {
        "type": "object",
        "title": title,
        "description": description,
        "additionalProperties": True,
    }
    if advanced:
        block["x-advanced"] = True
    return block


# --- required_extras ---------------------------------------------------------------


def _bridge_extras(config: Any) -> list[str]:
    """The bridge's own extras this config needs, read from the deciding fields only;
    anything malformed is ignored (check_config reports it)."""
    if not isinstance(config, dict):
        return []
    daemon = config.get("daemon")
    daemon = daemon if isinstance(daemon, dict) else {}
    if daemon.get("spawn", "never") == "never":
        return []  # a borrowed daemon runs whatever it was started with
    backend = config.get("backend", "real")
    extras = []
    if backend == "sim":
        extras.append("sim")
    if backend in ("sim", "real") and daemon.get("kinematics_engine") == "placo":
        extras.append("placo")
    return extras


def _tts_block(config: Any) -> dict[str, Any] | None:
    tts = config.get("tts") if isinstance(config, dict) else None
    return tts if isinstance(tts, dict) else None


def required_extras(config: dict[str, Any]) -> list[str]:
    found = [f"{_DISTRIBUTION}[{extra}]" for extra in _bridge_extras(config)]
    tts = _tts_block(config)
    if tts is not None:
        found += tts_setup.required_extras(tts)
    return found


# --- check_config ------------------------------------------------------------------

_TOP_LEVEL = ("backend", "robot", "daemon", "tts", "audio", "face_detection", "motion")
_UNKNOWN_KEYS = re.compile(r"unknown key\(s\) in '([^']+)'[^:]*: ([^,\s]+)")
_QUOTED_NAME = re.compile(r"'([A-Za-z_][\w.]*)'")


def _pointer(message: str) -> str:
    """The JSON Pointer of the field a loader message is about: the message quotes it
    (`'daemon.camera.source' must be ...`), or names the block an unknown key is in;
    "" when it names none (the document as a whole)."""
    unknown = _UNKNOWN_KEYS.search(message)
    if unknown is not None:
        block, key = unknown.groups()
        parts = ([] if block == "config" else block.split(".")) + [key]
        return "/" + "/".join(parts)
    for name in _QUOTED_NAME.findall(message):
        parts = name.split(".")
        if parts[0] in _TOP_LEVEL:
            return "/" + "/".join(parts)
    return ""


def check_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return [{"path": "", "message": "'config' must be an object"}]
    problems: list[dict[str, Any]] = []
    try:
        ReachyMiniConfig.from_dict({k: v for k, v in config.items() if k != "tts"})
    except Exception as exc:  # noqa: BLE001 - every refusal is a problem, never a raise
        message = str(exc) or type(exc).__name__
        problems.append({"path": _pointer(message), "message": message})
    tts = config.get("tts")
    if tts is not None:
        if not isinstance(tts, dict):
            problems.append({"path": "/tts", "message": "'tts' must be an object"})
        else:
            problems += [
                {"path": "/tts" + problem["path"], "message": problem["message"]}
                for problem in tts_setup.check_config(tts)
            ]
    return problems


# --- check_setup -------------------------------------------------------------------


def _extra_item(extra: str) -> dict[str, Any]:
    requirement = f"{_DISTRIBUTION}[{extra}]"
    try:
        for name in _EXTRA_IMPORTS[extra]:
            importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001 - reported: a broken install is missing too
        status = "missing"
        reason = f"{type(exc).__name__}: {exc}"
        detail = f"Not usable ({reason}): install {requirement}."
        if extra == "placo" and sys.platform == "win32":
            detail = (
                "Unavailable on Windows: the Placo engine publishes no Windows "
                "wheels. Use the 'analytical' or 'nn' kinematics engine."
            )
    else:
        status, detail = "ready", "Installed."
    return {
        "id": f"extra:{requirement}",
        "title": f"{_EXTRA_WHAT[extra]} ({extra} extra)",
        "status": status,
        "detail": detail,
        "preparable": False,
        "required": True,
    }


def _yunet_cached() -> bool:
    """Whether upstream's YuNet weights are in the Hugging Face cache (no network)."""
    from huggingface_hub import try_to_load_from_cache
    from reachy_mini.vision import face_detector as upstream

    found = try_to_load_from_cache(
        upstream._MODEL_REPO,  # pyright: ignore[reportPrivateUsage]
        upstream._MODEL_FILE,  # pyright: ignore[reportPrivateUsage]
        revision=upstream._MODEL_REVISION,  # pyright: ignore[reportPrivateUsage]
    )
    return isinstance(found, str)


def _emotions_cached() -> bool:
    """Whether the emotions dataset is in the Hugging Face cache (no network)."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import LocalEntryNotFoundError
    from reachy_mini.motion.recorded_move import DEFAULT_EMOTIONS_DATASET

    try:
        snapshot_download(
            DEFAULT_EMOTIONS_DATASET, repo_type="dataset", local_files_only=True
        )
    except LocalEntryNotFoundError:
        return False
    return True


class _Progress:
    """Turns the progress bars a Hugging Face download opens into the contract's
    events: the bytes of a single file against its size, or the files of a snapshot
    done against their count (its bytes' total grows as each file starts, so it is
    shown, not divided)."""

    def __init__(self, report: Callable[[float | None, str], None]) -> None:
        self._report = report
        self._lock = threading.Lock()
        self._bars: list[Any] = []
        self.bar_class = self._make_bar_class()

    def _make_bar_class(self) -> type:
        from tqdm.std import tqdm

        progress = self

        class _Bar(tqdm):  # type: ignore[type-arg]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs.pop("name", None)
                kwargs["file"] = io.StringIO()
                kwargs["disable"] = False
                super().__init__(*args, **kwargs)
                with progress._lock:
                    progress._bars.append(self)

            def update(self, n: float | None = 1) -> bool | None:
                shown = super().update(n)
                progress.changed()
                return shown

        return _Bar

    def changed(self) -> None:
        with self._lock:
            bars = list(self._bars)
        files = [b for b in bars if getattr(b, "unit", "") != "B" and b.total]
        sizes = [b for b in bars if getattr(b, "unit", "") == "B"]
        received = max((b.n for b in sizes), default=0)
        if files:
            bar = files[0]
            self._report(
                min(bar.n / bar.total, 1.0),
                f"Downloading files: {bar.n:g} of {bar.total:g} ({_mb(received)})",
            )
        elif len(sizes) == 1 and sizes[0].total:
            bar = sizes[0]
            self._report(
                min(bar.n / bar.total, 1.0),
                f"Downloading {_mb(bar.n)} of {_mb(bar.total)}",
            )
        else:
            self._report(None, f"Downloading ({_mb(received)})")


def _mb(n: float) -> str:
    return f"{n / 1e6:.1f} MB"


def _fetch_yunet(progress: _Progress) -> None:
    """Download the YuNet weights with upstream's own call (the model, file and
    revision its ``FaceDetector`` fetches), its progress reported."""
    from huggingface_hub import hf_hub_download
    from reachy_mini.vision import face_detector as upstream

    hf_hub_download(
        upstream._MODEL_REPO,  # pyright: ignore[reportPrivateUsage]
        upstream._MODEL_FILE,  # pyright: ignore[reportPrivateUsage]
        revision=upstream._MODEL_REVISION,  # pyright: ignore[reportPrivateUsage]
        tqdm_class=progress.bar_class,  # pyright: ignore[reportArgumentType]
    )


def _fetch_emotions(progress: _Progress) -> None:
    """Download the emotions dataset with upstream's own call (``RecordedMoves``'
    ``snapshot_download`` of it), its progress reported."""
    from huggingface_hub import snapshot_download
    from reachy_mini.motion.recorded_move import DEFAULT_EMOTIONS_DATASET

    snapshot_download(
        DEFAULT_EMOTIONS_DATASET,
        repo_type="dataset",
        tqdm_class=progress.bar_class,  # pyright: ignore[reportArgumentType]
    )


# --- the media registry: GStreamer's plugin registry --------------------------------

# Loads GStreamer and builds its plugin registry, in the environment a spawned daemon
# runs in; prints the number of plugins.
_GSTREAMER_WARM = (
    "import gi; gi.require_version('Gst', '1.0'); from gi.repository import Gst; "
    "Gst.init(None); print(len(Gst.Registry.get().get_plugin_list()))"
)
# A cold registry took 155 s to build on a Mac (2026-11-24); bounded well above that.
GSTREAMER_WARM_TIMEOUT_S = 900.0


def _gstreamer_registry() -> Path | None:
    """The registry file a daemon the bridge spawns reads, when this install names it
    (the GStreamer bundle's, under the environment's prefix); None when GStreamer
    keeps it in a place of its own (a system install)."""
    name = os.environ.get("GST_REGISTRY_1_0")
    return Path(name) if name else None


def _gstreamer_ready() -> bool | None:
    """Whether the registry is built: True / False when the bundle names its file,
    None when this install cannot tell without building it."""
    path = _gstreamer_registry()
    if path is None:
        return None
    return path.is_file() and path.stat().st_size > 0


def _warm_gstreamer() -> None:
    """Load GStreamer and build its plugin registry once, in a child with the
    environment a spawned daemon gets (``scrubbed_env``): no daemon started."""
    from .daemon import scrubbed_env

    try:
        done = subprocess.run(
            [sys.executable, "-c", _GSTREAMER_WARM],
            env=scrubbed_env(),
            capture_output=True,
            text=True,
            timeout=GSTREAMER_WARM_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BridgeError(
            f"GStreamer's plugin registry was not built within "
            f"{GSTREAMER_WARM_TIMEOUT_S:g} s"
        ) from exc
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()[-3:]
        raise BridgeError(
            "Could not load GStreamer to build its plugin registry "
            f"(exit {done.returncode}): {' / '.join(tail)}"
        )


# --- the file items -----------------------------------------------------------------

# id -> (title, what it is, the size to mention)
_FILES: dict[str, tuple[str, str]] = {
    _YUNET: ("Face detector weights (YuNet)", "upstream's YuNet face detection model"),
    _EMOTIONS: ("Emotions library", "the recorded emotions dataset"),
}


def _seams(item_id: str) -> tuple[Callable[[], bool], Callable[[_Progress], None]]:
    """The cache lookup and the download of a file item (looked up at call time, so a
    test replaces them)."""
    if item_id == _YUNET:
        return _yunet_cached, _fetch_yunet
    return _emotions_cached, _fetch_emotions


def _spawns_daemon(config: dict[str, Any]) -> bool:
    daemon = config.get("daemon")
    return (
        isinstance(daemon, dict)
        and daemon.get("spawn", "never") != "never"
        and config.get("backend", "real") in ("sim", "real")
    )


def _file_ids(config: dict[str, Any]) -> list[str]:
    ids = []
    face_detection = config.get("face_detection")
    if isinstance(face_detection, dict) and face_detection.get("detector") == "yunet":
        ids.append(_YUNET)
    if config.get("backend", "real") in ("real", "sim"):
        ids.append(_EMOTIONS)
    return ids


# What a session does when a not-required item is missing, for its detail.
_SELF_SERVED = {
    _YUNET: "the first session with detection fetches it itself",
    _EMOTIONS: "a spawned daemon preloads it and the first emotion fetches it itself",
}


def _file_item(item_id: str) -> dict[str, Any]:
    title, what = _FILES[item_id]
    cached, _ = _seams(item_id)
    try:
        ready = cached()
    except Exception as exc:  # noqa: BLE001 - an upstream change: unknown, not a raise
        status = "unknown"
        detail = f"Could not look it up in the Hugging Face cache ({exc})."
    else:
        status = "ready" if ready else "missing"
        detail = (
            "In the Hugging Face cache."
            if ready
            else f"Not downloaded yet: {what}; {_SELF_SERVED[item_id]}, slower."
        )
    return {
        "id": item_id,
        "title": title,
        "status": status,
        "detail": detail,
        "preparable": True,
        "required": False,
    }


def _gstreamer_item() -> dict[str, Any]:
    ready = _gstreamer_ready()
    if ready is None:
        status = "unknown"
        detail = (
            "GStreamer keeps its plugin registry in a place of its own here; "
            "preparing builds it if it is not."
        )
    elif ready:
        status, detail = "ready", "Built."
    else:
        status = "missing"
        detail = (
            "Not built yet: the daemon's first start would build it, a minute or "
            "more after an install."
        )
    return {
        "id": _GSTREAMER,
        "title": "Media plugin registry (GStreamer)",
        "status": status,
        "detail": detail,
        "preparable": True,
        "required": False,
    }


def _scene_item(config: dict[str, Any], base_dir: str | None) -> list[dict[str, Any]]:
    """The scene file a sim the bridge spawns loads (``daemon.scene`` ending in
    ``.xml``), resolved against ``base_dir`` as the loader resolves it: ``ready`` when
    it exists, else ``missing``; never preparable. None for anything else."""
    daemon = config.get("daemon")
    if not isinstance(daemon, dict) or config.get("backend", "real") != "sim":
        return []
    scene = daemon.get("scene")
    if daemon.get("spawn", "never") == "never" or not isinstance(scene, str):
        return []
    if not scene.endswith(".xml"):
        return []
    path = Path(resolve_scene(scene, base_dir))
    found = path.is_file()
    return [
        {
            "id": "file:daemon.scene",
            "title": "Scene file",
            "status": "ready" if found else "missing",
            "detail": f"{path}" if found else f"No file at {path}.",
            "preparable": False,
            "required": True,
        }
    ]


def check_setup(
    config: dict[str, Any], *, base_dir: str | None = None
) -> list[dict[str, Any]]:
    items = [_extra_item(extra) for extra in _bridge_extras(config)]
    items += _scene_item(config, base_dir)
    items += [_file_item(item_id) for item_id in _file_ids(config)]
    if _spawns_daemon(config):
        items.append(_gstreamer_item())
    tts = _tts_block(config)
    if tts is not None:
        items += tts_setup.check_setup(tts, base_dir=base_dir)
    return items


# --- prepare -----------------------------------------------------------------------


def _bridge_preparable(config: dict[str, Any]) -> list[str]:
    ids = _file_ids(config)
    if _spawns_daemon(config):
        ids.append(_GSTREAMER)
    return ids


def prepare(
    config: dict[str, Any],
    item: str,
    *,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    base_dir: str | None = None,
) -> None:
    """Make one preparable item of this config ready: the bridge's own (the YuNet
    weights, the emotions dataset, GStreamer's plugin registry), or the ``tts``
    block's, handed to tts-engine's ``prepare``. Idempotent; safe to call for two
    different items at once; ``ValueError`` for an id that is no preparable item."""

    def report(fraction: float | None, message: str) -> None:
        if on_progress is not None:
            on_progress({"fraction": fraction, "message": message})

    if item not in _bridge_preparable(config):
        tts = _tts_block(config)
        if tts is None:
            raise ValueError(f"{item!r} is not a preparable item of this config")
        tts_setup.prepare(tts, item, on_progress=on_progress, base_dir=base_dir)
        return
    if item == _GSTREAMER:
        if _gstreamer_ready():
            report(1.0, "Built.")
            return
        report(None, "Building GStreamer's plugin registry (minutes the first time)")
        _warm_gstreamer()
        report(1.0, "Built.")
        return
    title = _FILES[item][0]
    if _file_item(item)["status"] == "ready":
        report(1.0, "In the Hugging Face cache.")
        return
    _, fetch = _seams(item)
    report(0.0, f"Downloading the {title}")
    try:
        fetch(_Progress(report))
    except Exception as exc:
        raise BridgeError(f"Could not download the {title}: {exc}") from exc
    report(1.0, "In the Hugging Face cache.")


# --- options -----------------------------------------------------------------------


def options(
    name: str, config: dict[str, Any], *, base_dir: str | None = None
) -> list[dict[str, Any]]:
    tts = _tts_block(config)
    if name != "voices" or tts is None:
        return []
    return tts_setup.options(name, tts, base_dir=base_dir)


# --- probe -------------------------------------------------------------------------


def _quiet(config: ReachyMiniConfig) -> ReachyMiniConfig:
    """The config as the probe runs it: no voice, nothing that moves the robot or
    watches the camera."""
    motion = config.motion
    return replace(
        config,
        tts=None,
        face_detection=replace(config.face_detection, enabled=False),
        motion=replace(
            motion,
            presence=False,
            wobbling=False,
            tracking=replace(motion.tracking, enabled=False),
        ),
    )


def _connect(config: ReachyMiniConfig) -> str:
    """Enter a session, read the motor state, leave; the state read."""
    from .bridge import ReachyMiniBridge

    async def run() -> str:
        async with ReachyMiniBridge(config) as bridge:
            return await bridge.get_motors_state()

    return asyncio.run(run())


def _probe(config: dict[str, Any], base_dir: str | None) -> tuple[bool, str]:
    problems = check_config(config)
    if problems:
        first = problems[0]
        where = f" at {first['path']}" if first["path"] else ""
        return False, f"The config has a problem{where}: {first['message']}"
    loaded = ReachyMiniConfig.from_dict(
        {k: v for k, v in config.items() if k != "tts"}, base_dir=base_dir
    )
    try:
        state = _connect(_quiet(loaded))
    except Exception as exc:  # noqa: BLE001 - reported
        reason = str(exc) or type(exc).__name__
        return False, f"Could not connect to the {loaded.backend} robot: {reason}"
    message = (
        f"Connected to the {loaded.backend} robot (motors {state}), then disconnected."
    )
    tts = _tts_block(config)
    if tts is None:
        return True, message
    # The voice's own light check: it plays nothing.
    voice = tts_setup.probe(tts, base_dir=base_dir)
    return bool(voice["ok"]), f"{message} Voice: {voice['message']}"


def probe(config: dict[str, Any], *, base_dir: str | None = None) -> dict[str, Any]:
    """Connect, read the robot's state, disconnect; and the voice's own probe, played
    nowhere. Never raises."""
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            outcome["ok"], outcome["message"] = _probe(config, base_dir)
        except Exception as exc:  # noqa: BLE001 - a probe reports, it never raises
            outcome["ok"], outcome["message"] = False, str(exc) or type(exc).__name__

    started = time.monotonic()
    # Its own thread: `asyncio.run` needs one without a running loop, and the caller
    # may be on a loop's thread.
    worker = threading.Thread(target=work, name="reachy-mini-bridge-probe")
    worker.start()
    worker.join()
    return {
        "ok": outcome["ok"],
        "message": outcome["message"],
        "duration_s": round(time.monotonic() - started, 3),
    }
