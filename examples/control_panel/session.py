"""The panel's one session, with no gradio (specs/examples/control_panel.md "The session").

`RobotSession` builds a `ControlPanelController` from the app's saved config when Start asks
for it, and stops it at Stop. It tells app-setup's model when the config is in use
(`locked`), so the Setup panel refuses to change it while a session runs on it.
"""

from __future__ import annotations

import threading

from app_setup.app import AppModel

from examples.control_panel.controller import ControlPanelController
from reachy_mini_bridge import BridgeError, ReachyMiniConfig

__all__ = ["SECTION", "RobotSession"]

# The app's section holding the bridge's config document.
SECTION = "reachy"


class RobotSession:
    """The bridge session the Robot tab drives, built from the saved config at Start."""

    def __init__(self) -> None:
        self._lock = threading.Lock()  # one Start or Stop at a time
        self._busy = False  # a Start or a Stop in progress
        self.controller: ControlPanelController | None = None
        # Why the last Start started nothing (empty once a session runs).
        self.reasons: list[str] = []

    def locked(self) -> bool:
        """Whether the saved config is in use: from Start until the stop finished."""
        return self._busy or self.controller is not None

    def start(self, model: AppModel, *, timeout: float | None = None) -> list[str]:
        """Start a session on the saved config; why it cannot, as a list, instead (kept
        on `reasons`). Already running: nothing to do."""
        with self._lock:
            self.reasons = self._start(model, timeout)
            return self.reasons

    def _start(self, model: AppModel, timeout: float | None) -> list[str]:
        if self.controller is not None:
            return []
        saved = model.saved_config()
        blockers = model.blockers(saved)
        if blockers:
            return blockers
        self._busy = True
        try:
            config = ReachyMiniConfig.from_dict(model.used_config(saved)[SECTION])
            controller = ControlPanelController(config)
            controller.start(timeout=timeout)
            self.controller = controller
        except Exception as exc:  # noqa: BLE001 - said on the page
            return [f"The session could not start: {str(exc) or type(exc).__name__}"]
        finally:
            self._busy = False
        return []

    def stop(self) -> bool:
        """Stop the session (its verbs, the bridge, a daemon it spawned); False when
        nothing ran."""
        with self._lock:
            controller = self.controller
            if controller is None:
                return False
            self._busy = True
            try:
                controller.stop()
            finally:
                self.controller = None
                self._busy = False
            return True

    def require(self) -> ControlPanelController:
        """The running session's controller."""
        controller = self.controller
        if controller is None or not controller.running:
            raise BridgeError("no session is running: press Start")
        return controller
