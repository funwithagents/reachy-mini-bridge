"""Spike: two Reachy Minis in one MuJoCo simulation (README.md)."""

import os
import sys

# MuJoCo picks its GL backend when it is first imported. On a Linux box with no display
# (a container, a CI runner) the offscreen eye cameras need EGL, as the bridge's sim
# launcher sets for its headless render; an environment that names a backend keeps it.
if sys.platform == "linux" and not os.environ.get("DISPLAY"):
    os.environ.setdefault("MUJOCO_GL", "egl")
