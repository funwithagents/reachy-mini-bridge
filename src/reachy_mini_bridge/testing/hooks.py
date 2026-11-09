"""The hooks a consumer's ``conftest.py`` implements to extend the harness
(specs/testing/testing_support.md "Props and capabilities a consumer declares").

The plugin adds these specifications to the run (``pytest_addhooks``) and calls both
once, when it is configured — before collection, before any fixture — so the props are
in the scene of the daemon the harness spawns and the declared names are in the
vocabulary every ``requires`` mark is checked against. Every implementation's answer is
used, so several conftests or plugins add up.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from reachy_mini_bridge.bridge import ReachyMiniBridge
    from reachy_mini_bridge.testing.sim_scene import ImagePlane

hookspec = pytest.hookspec


@hookspec
def pytest_reachy_mini_scene_props() -> Sequence[ImagePlane]:  # type: ignore[empty-body]
    """The props the harness adds to the scene of every sim it spawns, after the pool of
    portraits: images on planes (``image_pool`` builds a kind's). Each kind among them
    becomes a capability, ``props:<kind>``, probed true when the daemon's scene carries
    a body of that kind."""


@hookspec
def pytest_reachy_mini_capabilities() -> Mapping[  # type: ignore[empty-body]
    str, Callable[[ReachyMiniBridge], bool]
]:
    """Capabilities the run adds to the vocabulary: ``{name: probe}``. Each probe is
    called once per run, with the built-in probes, given the started bridge of the first
    session over the daemon (its ``config.robot`` holds the daemon's ``host`` and
    ``port``), and returns whether the target has the capability. A probe that raises
    fails the probing fixture: a broken probe is not an absent capability."""
