"""Reachy Mini Bridge — a stable layer between the Reachy Mini robot and whatever
drives it (a human, a service, an LLM/agent).

Three layers, one module each: the connection seam (``robot``, with a first-party
``fake`` backend), the human-units interaction api (``api`` + ``audio``), and the agent
tools (``tools``). Configure it with a ``ReachyMiniConfig`` (``config``) — a dict, a
JSON string, or a JSON file — and drive it through ``ReachyMiniApi``::

    from reachy_mini_bridge import ReachyMiniApi

    async with ReachyMiniApi.from_json_file("robot.json") as api:
        await api.say("hello")

See specs/_overview.md for the architecture and specs/_index.md for each concept.
"""

from .api import ReachyMiniApi
from .config import ReachyMiniConfig
from .errors import ConfigError

__all__ = ["ConfigError", "ReachyMiniApi", "ReachyMiniConfig"]
