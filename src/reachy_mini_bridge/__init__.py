"""Reachy Mini Bridge — a stable layer between the Reachy Mini robot and whatever
drives it (a human, a service, an LLM/agent).

Two layers: the connection seam (``robot``, with a first-party ``fake`` backend) and
the human-units interaction bridge (``bridge`` and its sessions). Configure it with a ``ReachyMiniConfig`` (``config``) — a dict, a
JSON string, or a JSON file — and drive it through ``ReachyMiniBridge``::

    from reachy_mini_bridge import ReachyMiniBridge

    async with ReachyMiniBridge.from_json_file("robot.json") as bridge:
        await bridge.say("hello")

See specs/_overview.md for the architecture and specs/_index.md for each concept.
"""

from .audio import SpeechSynthesizer, TTSEngineSynthesizer
from .bridge import ReachyMiniBridge
from .camera import CameraFrame
from .config import ReachyMiniConfig
from .errors import (
    BridgeError,
    ConfigError,
    GravityCompensationUnsupportedError,
    MotorsNotEnabledError,
)
from .face_detection import Face, FaceDetector, FaceReport, PixelFace
from .motion import IdleMove, IdleOffsets
from .observable import Observable

__all__ = [
    "BridgeError",
    "CameraFrame",
    "ConfigError",
    "Face",
    "FaceDetector",
    "FaceReport",
    "GravityCompensationUnsupportedError",
    "IdleMove",
    "IdleOffsets",
    "MotorsNotEnabledError",
    "Observable",
    "PixelFace",
    "ReachyMiniBridge",
    "ReachyMiniConfig",
    "SpeechSynthesizer",
    "TTSEngineSynthesizer",
]
