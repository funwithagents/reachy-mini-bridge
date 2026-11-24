"""Setting the bridge up from an application, as data (specs/core/setup.md).

Seven functions over the bridge's config document (the object
``ReachyMiniConfig.from_dict`` takes): what the config looks like (``config_schema``),
which extras a config needs (``required_extras``), what is wrong in it
(``check_config``), what this machine is missing to run it (``check_setup``: the
extras, the models to download, the voice's key), downloading what can be
(``prepare``), the choices of a named list (``options``), and one light, real run
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
import re
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


def _fetch_yunet() -> None:
    """Download the YuNet weights the way a session does: build upstream's detector
    (which fetches them into the Hugging Face cache), then let it go."""
    from .yunet import _upstream_detector  # pyright: ignore[reportPrivateUsage]

    _upstream_detector()


def _fetch_emotions() -> None:
    """Download the emotions dataset the way a session does: upstream's
    ``RecordedMoves`` over it (which fetches it when it is not cached)."""
    from reachy_mini.motion.recorded_move import (
        DEFAULT_EMOTIONS_DATASET,
        RecordedMoves,
    )

    RecordedMoves(DEFAULT_EMOTIONS_DATASET)


# id -> (title, what it is)
_FILES: dict[str, tuple[str, str]] = {
    _YUNET: ("Face detector weights (YuNet)", "upstream's YuNet face detection model"),
    _EMOTIONS: ("Emotions library", "the recorded emotions dataset"),
}


def _seams(item_id: str) -> tuple[Callable[[], bool], Callable[[], None]]:
    """The cache lookup and the download of a file item (looked up at call time, so a
    test replaces them)."""
    if item_id == _YUNET:
        return _yunet_cached, _fetch_yunet
    return _emotions_cached, _fetch_emotions


def _file_ids(config: dict[str, Any]) -> list[str]:
    ids = []
    face_detection = config.get("face_detection")
    if isinstance(face_detection, dict) and face_detection.get("detector") == "yunet":
        ids.append(_YUNET)
    if config.get("backend", "real") in ("real", "sim"):
        ids.append(_EMOTIONS)
    return ids


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
            else f"Not downloaded yet: {what}, fetched into the Hugging Face cache."
        )
    return {
        "id": item_id,
        "title": title,
        "status": status,
        "detail": detail,
        "preparable": True,
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
        }
    ]


def check_setup(
    config: dict[str, Any], *, base_dir: str | None = None
) -> list[dict[str, Any]]:
    items = [_extra_item(extra) for extra in _bridge_extras(config)]
    items += _scene_item(config, base_dir)
    items += [_file_item(item_id) for item_id in _file_ids(config)]
    tts = _tts_block(config)
    if tts is not None:
        items += tts_setup.check_setup(tts, base_dir=base_dir)
    return items


# --- prepare -----------------------------------------------------------------------


def prepare(
    config: dict[str, Any],
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    *,
    base_dir: str | None = None,
) -> None:
    for item_id in _file_ids(config):
        item = _file_item(item_id)
        if item["status"] == "ready":
            continue
        title = _FILES[item_id][0]
        _, fetch = _seams(item_id)
        if on_progress is not None:
            on_progress(
                {"id": item_id, "fraction": None, "message": f"Downloading {title}"}
            )
        try:
            fetch()
        except Exception as exc:
            raise BridgeError(f"Could not download the {title}: {exc}") from exc
    tts = _tts_block(config)
    if tts is not None:
        tts_setup.prepare(tts, on_progress, base_dir=base_dir)


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
