"""The setup contract over the bridge's config document (specs/core/setup.md)."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import tts_engine.setup as tts_setup
from module_setup.contract import check_contract, parse_description
from tts_engine.modules import REGISTRY
from tts_engine.modules.base import VoiceInfo, key_env_field
from tts_engine.modules.tone.module import ToneModule

from reachy_mini_bridge import BridgeError, ReachyMiniBridge, ReachyMiniConfig, setup
from reachy_mini_bridge import bridge as bridge_module
from reachy_mini_bridge.fake_reachy_mini import FakeReachyMini

_ROOT = Path(__file__).resolve().parent.parent
_EXAMPLES = _ROOT / "examples"

# --- the examples the conformance check runs over -----------------------------------


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _examples() -> list[dict[str, Any]]:
    """Every config the repo ships: the profiles, the greeter's, the voice tour's (each
    voice config is a `tts` block, put onto the fake)."""
    found = [
        _load(p)
        for folder in ("configs", "greeter/configs", "voice_tour/configs")
        for p in sorted((_EXAMPLES / folder).glob("*.json"))
    ]
    found += [
        {"backend": "fake", **_load(p)}
        for p in sorted((_EXAMPLES / "voice_tour" / "voice-configs").glob("*.json"))
    ]
    return found


# The tts modules no shipped config selects whose required fields have no default.
_UNCOVERED_VOICES = [
    {"backend": "fake", "tts": {"module": {"type": "openai-speech-compatible",
                                           "base_url": "http://127.0.0.1:8880/v1",
                                           "voice": "af_heart"}}},
    {"backend": "fake", "tts": {"module": {"type": "audiofile",
                                           "files": [{"text": "hi",
                                                      "file": "hi.wav"}]}}},
]  # fmt: skip


def test_the_contract_holds_over_every_shipped_config() -> None:
    check_contract(setup, _examples() + _UNCOVERED_VOICES)


def test_every_shipped_config_is_accepted() -> None:
    refused = {
        i: setup.check_config(example)
        for i, example in enumerate(_examples())
        if setup.check_config(example)
    }
    assert refused == {}


# --- config_schema ------------------------------------------------------------------


def test_the_schema_has_backend_as_a_field_tts_delegated_and_the_rest_raw() -> None:
    raw = setup.config_schema()
    schema = raw["schema"]
    described = parse_description(raw).schema
    by_name = {p.name: p for p in described.properties}

    assert raw["distribution"] == "reachy-mini-bridge"
    assert schema["properties"]["backend"]["enum"] == ["real", "sim", "fake"]
    assert schema["properties"]["backend"]["default"] == "real"
    for name in ("robot", "daemon", "audio", "face_detection", "motion"):
        assert getattr(by_name[name], "free_form", False), name
    # the voice: tts-engine's module choice and gain, never its player
    tts = schema["properties"]["tts"]
    engine = tts_setup.config_schema()["schema"]
    assert tts["properties"]["module"] == engine["properties"]["module"]
    assert set(tts["properties"]) == set(engine["properties"]) - {"player"}
    assert "tts" not in schema.get("required", [])
    assert setup.config_schema() == raw


# --- required_extras ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"backend": "sim", "daemon": {"spawn": "auto"}}, ["reachy-mini-bridge[sim]"]),
        ({"backend": "sim"}, []),  # a borrowed sim daemon: nothing here
        (
            {
                "backend": "real",
                "daemon": {"spawn": "auto", "kinematics_engine": "placo"},
            },
            ["reachy-mini-bridge[placo]"],
        ),
        ({"backend": "real", "daemon": {"kinematics_engine": "placo"}}, []),
        (
            {
                "backend": "sim",
                "daemon": {"spawn": "always", "kinematics_engine": "placo"},
                "tts": {"module": {"type": "gradium"}},
            },
            [
                "reachy-mini-bridge[sim]",
                "reachy-mini-bridge[placo]",
                "tts-engine[gradium]",
            ],
        ),
        ({"backend": "fake", "tts": {"module": {"type": "tone"}}}, []),
    ],
)
def test_required_extras_follows_the_deciding_fields(
    config: dict[str, Any], expected: list[str]
) -> None:
    assert setup.required_extras(config) == expected


@pytest.mark.parametrize(
    "config",
    [
        {"daemon": "auto"},
        {"backend": 3, "daemon": {"spawn": ["auto"]}},
        {"tts": "phonon"},
        {"tts": {"module": "phonon"}},
    ],
)
def test_required_extras_never_raises_on_a_malformed_config(
    config: dict[str, Any],
) -> None:
    assert setup.required_extras(config) == []


# --- check_config -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "path", "words"),
    [
        ({"backend": "robot"}, "/backend", "'backend' must be one of"),
        ({"daemon": {"speed": 1}}, "/daemon/speed", "unknown key(s) in 'daemon'"),
        ({"colour": "red"}, "/colour", "unknown key(s) in 'config'"),
        (
            {"backend": "sim", "daemon": {"camera": {"source": "screen"}}},
            "/daemon/camera/source",
            "'daemon.camera.source' must be one of",
        ),
        (
            {"motion": {"tracking": {"mirroring": {"max_deg": 90}}}},
            "/motion/tracking/mirroring/max_deg",
            "",
        ),
        ({"robot": {"use_sim": True}}, "/robot/use_sim", "is reserved"),
        ({"robot": {"hots": "x"}}, "/robot/hots", "not a reachy_mini.ReachyMini"),
        (
            {"face_detection": {"enabled": True}},
            "/face_detection/enabled",
            "need a face detector",
        ),
        ({"motion": []}, "/motion", "'motion' must be an object"),
        ({"tts": "phonon"}, "/tts", "'tts' must be an object"),
    ],
)
def test_a_refused_config_is_reported_at_its_pointer_with_the_loaders_words(
    config: dict[str, Any], path: str, words: str
) -> None:
    [problem] = setup.check_config(config)

    assert problem["path"] == path
    assert words in problem["message"]
    if not path.startswith("/tts"):
        with pytest.raises(ValueError, match=".") as raised:
            ReachyMiniConfig.from_dict(config)
        assert str(raised.value) == problem["message"]


def test_the_voice_is_checked_by_tts_engine_under_tts() -> None:
    config = {
        "backend": "fake",
        "tts": {"module": {"type": "elevenlabs", "api_key": "sk-123"}},
    }

    problems = setup.check_config(config)

    assert {p["path"] for p in problems} == {
        "/tts/module/api_key",  # a key is never written in a config
        "/tts/module/voice_id",
    }
    assert any("api_key_env" in p["message"] for p in problems)


def test_a_bridge_problem_and_a_voice_problem_are_both_reported() -> None:
    problems = setup.check_config(
        {"backend": "moon", "tts": {"module": {"type": "nope"}}}
    )

    assert [p["path"] for p in problems] == ["/backend", "/tts/module/type"]


def test_a_tts_player_is_still_accepted() -> None:
    config = {"tts": {"module": {"type": "tone"}, "player": {"discard": True}}}

    assert setup.check_config({"backend": "fake", **config}) == []
    assert setup.check_config({"backend": "fake", "tts": None}) == []


# --- check_setup and prepare --------------------------------------------------------


class _Cache:
    """The two cache lookups and downloads, scripted: what is cached, what was
    fetched."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, cached: set[str]) -> None:
        self.cached = set(cached)
        self.fetched: list[str] = []
        self.fail: str | None = None
        for name in ("yunet", "emotions"):
            monkeypatch.setattr(setup, f"_{name}_cached", self._lookup(name))
            monkeypatch.setattr(setup, f"_fetch_{name}", self._fetch(name))

    def _lookup(self, name: str) -> Any:
        def cached() -> bool:
            if self.fail == name:
                raise AttributeError("upstream moved its model")
            return name in self.cached

        return cached

    def _fetch(self, name: str) -> Any:
        def fetch() -> None:
            if self.fail == name:
                raise OSError("no network")
            self.fetched.append(name)
            self.cached.add(name)

        return fetch


def _statuses(items: list[dict[str, Any]]) -> dict[str, str]:
    return {item["id"]: item["status"] for item in items}


def test_the_files_are_items_only_when_the_config_uses_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Cache(monkeypatch, cached={"emotions"})

    assert setup.check_setup({"backend": "fake"}) == []
    assert _statuses(setup.check_setup({"backend": "real"})) == {
        "dataset:emotions": "ready"
    }
    found = setup.check_setup(
        {"backend": "fake", "face_detection": {"detector": "yunet"}}
    )
    assert _statuses(found) == {"weights:yunet": "missing"}
    assert found[0]["preparable"] is True


def test_a_failed_lookup_is_unknown_not_a_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache(monkeypatch, cached=set())
    cache.fail = "emotions"

    [item] = setup.check_setup({"backend": "sim"})

    assert item["status"] == "unknown"
    assert "upstream moved its model" in item["detail"]


def test_an_extra_is_ready_when_its_import_works_and_missing_with_its_hint_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Cache(monkeypatch, cached={"emotions"})
    config = {
        "backend": "sim",
        "daemon": {"spawn": "auto", "kinematics_engine": "placo"},
    }
    monkeypatch.setitem(sys.modules, "mujoco", object())  # importable
    monkeypatch.setitem(sys.modules, "placo", None)  # an import that fails

    items = setup.check_setup(config)

    assert [item["id"] for item in items] == [
        "extra:reachy-mini-bridge[sim]",
        "extra:reachy-mini-bridge[placo]",
        "dataset:emotions",
    ]
    sim, placo, _ = items
    assert sim["status"] == "ready"
    assert placo["status"] == "missing" and not placo["preparable"]
    if sys.platform != "win32":
        assert "install reachy-mini-bridge[placo]" in placo["detail"]


class _KeyedTone(ToneModule):
    """The tone fixture behind a key, the way a hosted module reads one."""

    title = "Keyed tone"
    fields = (*ToneModule.fields, key_env_field("Keyed tone", "KEYED_TONE_KEY"))

    def __init__(
        self, config: dict[str, Any], *, secrets: Mapping[str, str] | None = None
    ) -> None:
        self.key = self.resolve_api_key(config, secrets)
        super().__init__({k: v for k, v in config.items() if k != "api_key_env"})

    @classmethod
    def list_voices(cls, config: dict[str, Any]) -> list[VoiceInfo]:
        return [VoiceInfo(id="a4", name="A4", config={"frequency": 440.0})]


@pytest.fixture
def keyed_tone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setitem(REGISTRY, "keyed-tone", _KeyedTone)
    monkeypatch.delenv("KEYED_TONE_KEY", raising=False)
    return "keyed-tone"


def test_the_voices_items_come_after_the_bridges(
    monkeypatch: pytest.MonkeyPatch, keyed_tone: str
) -> None:
    _Cache(monkeypatch, cached=set())
    config = {"backend": "real", "tts": {"module": {"type": keyed_tone}}}

    before = setup.check_setup(config)
    monkeypatch.setenv("KEYED_TONE_KEY", "k")
    after = setup.check_setup(config)

    assert _statuses(before) == {
        "dataset:emotions": "missing",
        "key:KEYED_TONE_KEY": "missing",
    }
    assert _statuses(after)["key:KEYED_TONE_KEY"] == "ready"


def test_prepare_fetches_only_what_is_missing_and_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache(monkeypatch, cached={"emotions"})
    config = {"backend": "sim", "face_detection": {"detector": "yunet"}}
    progress: list[dict[str, Any]] = []

    setup.prepare(config, progress.append)
    setup.prepare(config, progress.append)  # idempotent

    assert cache.fetched == ["yunet"]
    assert [p["id"] for p in progress] == ["weights:yunet"]
    assert _statuses(setup.check_setup(config)) == {
        "weights:yunet": "ready",
        "dataset:emotions": "ready",
    }


def test_prepare_raises_with_a_message_when_a_download_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _Cache(monkeypatch, cached=set())
    cache.fail = "emotions"  # an unknown lookup is fetched too, and the fetch fails

    with pytest.raises(BridgeError, match="Could not download the Emotions library"):
        setup.prepare({"backend": "real"})


def test_the_cache_lookups_read_the_hugging_face_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real lookups, over an empty cache then one holding the two entries."""
    import huggingface_hub.constants as hf_constants
    from reachy_mini.motion.recorded_move import DEFAULT_EMOTIONS_DATASET
    from reachy_mini.vision import face_detector as upstream

    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path))
    assert not setup._yunet_cached()
    assert not setup._emotions_cached()

    repo = "models--" + upstream._MODEL_REPO.replace("/", "--")
    weights = tmp_path / repo / "snapshots" / upstream._MODEL_REVISION
    weights.mkdir(parents=True)
    (weights / upstream._MODEL_FILE).write_bytes(b"onnx")
    dataset = tmp_path / ("datasets--" + DEFAULT_EMOTIONS_DATASET.replace("/", "--"))
    (dataset / "refs").mkdir(parents=True)
    (dataset / "refs" / "main").write_text("abc123")
    (dataset / "snapshots" / "abc123").mkdir(parents=True)

    assert setup._yunet_cached()
    assert setup._emotions_cached()


# --- options ------------------------------------------------------------------------


def test_the_voices_are_the_tts_blocks(keyed_tone: str) -> None:
    config = {"backend": "fake", "tts": {"module": {"type": keyed_tone}}}

    assert setup.options("voices", config) == [
        {"label": "A4", "fields": {"frequency": 440.0}}
    ]
    assert setup.options("webcams", config) == []
    assert setup.options("voices", {"backend": "fake"}) == []


# --- probe --------------------------------------------------------------------------


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> list[FakeReachyMini]:
    """Every fake robot a session builds."""
    built: list[FakeReachyMini] = []
    build = bridge_module.build_robot

    def recording(backend: str = "real", **opts: Any) -> Any:
        robot = build(backend, **opts)
        if isinstance(robot, FakeReachyMini):
            built.append(robot)
        return robot

    monkeypatch.setattr(bridge_module, "build_robot", recording)
    return built


def test_the_probe_connects_reads_and_leaves_without_moving_the_robot(
    fakes: list[FakeReachyMini],
) -> None:
    config = {
        "backend": "fake",
        "motion": {"presence": True, "idle": "breathing"},
    }

    result = setup.probe(config)

    assert result["ok"], result
    [robot] = fakes
    status = robot.client.get_status().backend_status
    assert status is not None
    assert result["message"] == (
        f"Connected to the fake robot (motors {status.motor_control_mode}), "
        "then disconnected."
    )
    assert robot.targets == []  # nothing commanded: presence off for the probe
    assert ("media.stop_recording", {}) in robot.commands  # the session was left


def test_the_probe_runs_the_voices_own_check_played_nowhere(
    fakes: list[FakeReachyMini],
) -> None:
    config = {"backend": "fake", "tts": {"module": {"type": "tone"}}}

    result = setup.probe(config)

    assert result["ok"], result
    assert "nothing was played on a device" in result["message"]


def test_a_voice_that_cannot_build_fails_the_probe_without_raising(
    fakes: list[FakeReachyMini], keyed_tone: str
) -> None:
    config = {"backend": "fake", "tts": {"module": {"type": keyed_tone}}}

    result = setup.probe(config)

    assert not result["ok"]
    assert "KEYED_TONE_KEY" in result["message"]
    assert result["duration_s"] >= 0


def test_a_config_with_a_problem_fails_the_probe_with_it() -> None:
    result = setup.probe({"backend": "moon"})

    assert not result["ok"]
    assert result["message"].startswith("The config has a problem at /backend")


# --- the keys a caller holds in code ------------------------------------------------


def test_a_key_passed_in_secrets_builds_the_voice(keyed_tone: str) -> None:
    config = ReachyMiniConfig.from_dict(
        {"backend": "fake", "tts": {"module": {"type": keyed_tone}}}
    )

    without = ReachyMiniBridge(config)
    with_key = ReachyMiniBridge(config, secrets={"KEYED_TONE_KEY": "k-123"})

    assert "KEYED_TONE_KEY" in str(without.synthesizer_error)
    assert with_key.synthesizer_error is None
