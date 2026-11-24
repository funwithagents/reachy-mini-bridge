"""The panel's one session, with no gradio (specs/examples/control_panel.md "The session").

`RobotSession` builds a `ControlPanelController` from the app's saved config when Start asks
for it, and stops it at Stop. It tells app-setup's model when the config is in use
(`locked`), so the Setup panel refuses to change it while a session runs on it.
"""

from __future__ import annotations

import threading
from typing import Literal

from app_setup.app import AppModel

from examples.control_panel.controller import ControlPanelController
from reachy_mini_bridge import BridgeError, ReachyMiniConfig

__all__ = ["SECTION", "RobotSession", "Status"]

# Where the session is: what the Start and Stop buttons follow.
Status = Literal["stopped", "starting", "running", "stopping"]

# The app's section holding the bridge's config document.
SECTION = "reachy"


class RobotSession:
    """The bridge session the Robot tab drives, built from the saved config at Start."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # one Start or Stop at a time
        self._status: Status = "stopped"
        self.controller: ControlPanelController | None = None
        # Why the last Start started nothing (empty once a session runs).
        self.reasons: list[str] = []

    @property
    def status(self) -> Status:
        """`starting` from a Start until the session runs (or did not start),
        `running`, `stopping` from a Stop until it is stopped, else `stopped`."""
        return self._status

    def locked(self) -> bool:
        """Whether the saved config is in use: from Start until the stop finished."""
        return self._status != "stopped" or self.controller is not None

    def start(self, model: AppModel, *, timeout: float | None = None) -> list[str]:
        """Start a session on the saved config; why it cannot, as a list, instead (kept
        on `reasons`). Already running: nothing to do."""
        with self._lock:
            if self.controller is None:
                self._status = "starting"
            try:
                self.reasons = self._start(model, timeout)
            finally:
                self._status = "running" if self.controller is not None else "stopped"
            return self.reasons

    def _start(self, model: AppModel, timeout: float | None) -> list[str]:
        if self.controller is not None:
            return []
        saved = model.saved_config()
        blockers = model.blockers(saved)
        if blockers:
            return blockers
        try:
            config = ReachyMiniConfig.from_dict(model.used_config(saved)[SECTION])
            controller = ControlPanelController(config)
            controller.start(timeout=timeout)
            self.controller = controller
        except Exception as exc:  # noqa: BLE001 - said on the page
            return [f"The session could not start: {str(exc) or type(exc).__name__}"]
        return []

    def stop(self) -> bool:
        """Stop the session (its verbs, the bridge, a daemon it spawned); False when
        nothing ran."""
        with self._lock:
            controller = self.controller
            if controller is None:
                return False
            self._status = "stopping"
            try:
                controller.stop()
            finally:
                self.controller = None
                self._status = "stopped"
            return True

    def require(self) -> ControlPanelController:
        """The running session's controller."""
        controller = self.controller
        if controller is None or not controller.running:
            raise BridgeError("no session is running: press Start")
        return controller
