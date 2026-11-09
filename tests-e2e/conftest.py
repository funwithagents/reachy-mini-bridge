# Live tier shared fixtures.
#
# This tier is NOT collected by the default `uv run pytest` (testpaths = ["tests"]);
# run it explicitly with `uv run pytest tests-e2e`.
#
# The harness itself is shipped library code — `reachy_mini_bridge.testing` — so consumers
# of the bridge reuse it (see specs/testing/testing_support.md). The bridge dogfoods its own
# shipped harness here: its plugin, `reachy_mini_bridge.testing.fixtures`, brings the
# `live_bridge` fixture (and the session-scoped `live_daemon` it depends on — one daemon
# per run, one bridge session per file), the `requires` marker and its gate, and the
# `live target` summary. A downstream project opts in from its root conftest with
# `pytest_plugins = ["reachy_mini_bridge.testing.fixtures"]`; this conftest isn't the
# rootdir conftest, so it registers the plugin itself (`pytest_configure` below).
#
# The tier is one file per subject, each gated through its `pytestmark`
# (specs/testing/testing.md): test_motors / test_motion (`motion`), test_audio (`audio`),
# test_perception (`camera`, then `props:face`), test_head_tracking and test_custom_faces
# (`camera` + `props:face`), test_sim_displays (`camera` + `props:face` + `face_markers`),
# test_scene_props (`camera` + `props:card`); a test needing more adds its own mark. The
# fixtures several of them share — `face_scene`, `emotions_library` — are the plugin
# module's too.
#
# The tier also declares a prop of its own through the plugin's hook — `card_1`, a
# striped landscape PNG generated below — so the path a consumer's hand or object takes
# (specs/testing/testing_support.md "Props and capabilities a consumer declares") runs
# in the bridge's own tier.
#
# Mirror any isolation fixture the fast tier uses here — tests-e2e/ isn't a package that
# can import from tests/, so the few lines are duplicated rather than shared. (The library
# holds no process-global state today, so no reset fixture is needed yet — see
# specs/testing/testing.md.)

import struct
import tempfile
import zlib
from pathlib import Path

import pytest

from reachy_mini_bridge.testing.sim_scene import ImagePlane, image_pool

# The card's size in metres, 3:2 like its image: about a hand photo's.
CARD_WIDTH_M = 0.36


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.import_plugin("reachy_mini_bridge.testing.fixtures")


def card_png() -> Path:
    """A 300x200 PNG of saturated magenta and green stripes — nothing in the empty scene
    looks like it — written with zlib and struct, so no image library is needed."""
    width, height = 300, 200
    rows = b"".join(
        b"\x00" + (b"\xff\x00\xff" if (y // 25) % 2 else b"\x00\xc0\x00") * width
        for y in range(height)
    )

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path = Path(tempfile.gettempdir()) / "reachy-mini-bridge-e2e" / "card.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return path


def pytest_reachy_mini_scene_props() -> tuple[ImagePlane, ...]:
    return image_pool("card", [card_png()], width=CARD_WIDTH_M)


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config: pytest.Config) -> None:
    """Run this tier serially, overriding the ``-n auto`` the fast tier sets in
    ``addopts``: every module borrows or spawns the one daemon at
    ``REACHY_MINI_HOST:REACHY_MINI_PORT``, and xdist workers would race to spawn it,
    borrow each other's, and tear it down under one another. This conftest loads only
    when ``tests-e2e`` is among the paths collected, so the fast tier keeps its
    workers; forcing zero is harmless wherever it runs, controller or worker."""
    config.option.numprocesses = 0
