"""The `tts-*` extras mirror tts-engine's extras one-to-one (specs/project.md).

tts-engine is a git dependency, so `reachy-mini-bridge[tts-<extra>]` is how a consumer
installs a provider; a tts-engine bump that adds a provider or a bundle fails here
until its alias exists.
"""

import tomllib
from importlib.metadata import metadata
from pathlib import Path

from tts_engine.modules import REGISTRY

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
# tts-engine's server transports, and its `all` bundle that only adds them to
# `all-modules`: the bridge runs no tts-engine server.
_NOT_MIRRORED = {"mcp", "all"}


def _bridge_tts_extras() -> dict[str, list[str]]:
    extras = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    return {name: reqs for name, reqs in extras.items() if name.startswith("tts-")}


def _engine_extras() -> set[str]:
    return set(metadata("tts-engine").get_all("Provides-Extra") or []) - _NOT_MIRRORED


def test_every_tts_engine_extra_has_its_alias() -> None:
    expected = {
        f"tts-{extra.removesuffix('-modules')}": [f"tts-engine[{extra}]"]
        for extra in _engine_extras()
    }
    assert _bridge_tts_extras() == expected, (
        'one `tts-<extra> = ["tts-engine[<extra>]"]` line per tts-engine extra, '
        "a bundle's `-modules` suffix dropped from the alias"
    )


def test_every_provider_installs_through_its_module_type() -> None:
    """The alias rule the adapter's install hint relies on: `tts-<module.type>`."""
    providers = {name for name, module in REGISTRY.items() if module.kind != "fixture"}
    assert {f"tts-{p}" for p in providers} <= set(_bridge_tts_extras())
