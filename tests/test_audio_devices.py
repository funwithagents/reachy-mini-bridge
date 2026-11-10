"""The robot's audio devices opened by name (specs/audio/audio.md "The robot's audio
devices"): matching on upstream's own matcher, the substitution of upstream's lookup, and
— in a child process, where GStreamer may initialise — upstream's ``GStreamerAudio``
opening the chosen devices for the session and for every sound file after it."""

from __future__ import annotations

import json
import subprocess
import sys
import types
from typing import Any

import pytest

from reachy_mini_bridge import audio_devices
from reachy_mini_bridge.audio_devices import AudioDeviceError, AudioDevices, substitute


def _device(name: str, device_id: str, **props: str) -> Any:
    """A device as upstream's listing describes it, carrying the id upstream's matcher
    reads on every platform (CoreAudio, PipeWire, WASAPI)."""
    from reachy_mini.media.device_detection import DeviceInfo

    return DeviceInfo(
        display_name=name,
        device_class="Audio/Source",
        properties={
            "unique-id": device_id,
            "node.name": device_id,
            "device.api": "wasapi2",
            "device.id": device_id,
            **props,
        },
    )


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    devices: dict[str, list[Any]] = {
        "Source": [
            _device("MacBook Pro Microphone", "mic-id"),
            _device("BlackHole 2ch", "blackhole-in"),
        ],
        "Sink": [
            _device("MacBook Pro Speakers", "speakers-id"),
            _device("BlackHole 2ch", "blackhole-out"),
            _device("BlackHole 16ch", "blackhole16-out"),
        ],
    }
    monkeypatch.setattr(audio_devices, "_list_devices", lambda d: devices[d])
    return devices


def test_a_name_resolves_to_the_one_device_containing_it(host: Any) -> None:
    """Upstream's rule: the name is contained in the device's name."""
    assert audio_devices.resolve("BlackHole", "Source") == "blackhole-in"
    assert audio_devices.resolve("BlackHole 2ch", "Sink") == "blackhole-out"
    assert audio_devices.resolve_devices("MacBook", None) == AudioDevices(
        source="mic-id"
    )


def test_no_device_or_several_is_an_error_naming_the_devices(host: Any) -> None:
    with pytest.raises(AudioDeviceError, match="no audio input device matches") as e:
        audio_devices.resolve("Loopback", "Source")
    assert "'MacBook Pro Microphone', 'BlackHole 2ch'" in str(e.value)
    with pytest.raises(AudioDeviceError, match="ambiguous: it matches 'BlackHole 2ch'"):
        audio_devices.resolve("BlackHole", "Sink")


def test_upstream_s_matcher_decides_what_a_microphone_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Linux a PulseAudio monitor is not a microphone to upstream's matcher, and so not
    to the bridge (specs/audio/audio.md)."""
    from reachy_mini.media import device_detection

    monitor = _device(
        "Monitor of Null Output", "null.monitor", **{"device.class": "monitor"}
    )
    monkeypatch.setattr(audio_devices, "_list_devices", lambda d: [monitor])
    real = device_detection.find_audio_device
    monkeypatch.setattr(
        device_detection,
        "find_audio_device",
        lambda devices, kind, target_name: real(devices, kind, "Linux", target_name),
    )
    with pytest.raises(AudioDeviceError, match="no audio input device"):
        audio_devices.resolve("Null Output", "Source")


def _module() -> types.ModuleType:
    module = types.ModuleType("fake_upstream_audio")
    module.get_audio_device = lambda device_type="Source": f"upstream-{device_type}"  # type: ignore[attr-defined]
    return module


def test_the_substitution_answers_named_directions_and_restores_upstream() -> None:
    module = _module()
    with substitute(module, AudioDevices(source="blackhole-in")):
        assert module.get_audio_device("Source") == "blackhole-in"
        assert module.get_audio_device("Sink") == "upstream-Sink"
    assert module.get_audio_device("Source") == "upstream-Source"


def test_the_substitution_is_shared_by_the_same_devices_and_refuses_others() -> None:
    """One process opens one robot's devices: a second holder of the same devices shares
    the substitution, upstream's lookup comes back with the last, other devices fail."""
    module = _module()
    chosen = AudioDevices(source="blackhole-in", sink="blackhole-out")
    with substitute(module, chosen):
        with substitute(module, chosen):
            pass
        assert module.get_audio_device("Sink") == "blackhole-out"  # still held
        with (
            pytest.raises(AudioDeviceError, match="already opens other audio devices"),
            substitute(module, AudioDevices(source="mic-id")),
        ):
            pass
        assert module.get_audio_device("Source") == "blackhole-in"
    assert module.get_audio_device("Source") == "upstream-Source"
    with substitute(module, AudioDevices()):  # nothing named: upstream's, untouched
        assert module.get_audio_device("Source") == "upstream-Source"


# Run in a child: GStreamer initialises there (an xdist worker inherits the bundle's
# doubled variables and `Gst.init` exits it — tests/test_audio.py, `scrubbed_env`).
_CHILD = """
import json
from reachy_mini_bridge.audio_devices import AudioDevices, substitute


def chosen_ids(element):
    found = [element]
    if hasattr(element, "iterate_recurse"):
        it, found = element.iterate_recurse(), []
        while True:
            ok, el = it.next()
            if ok.value_nick != "ok":
                break
            found.append(el)
    ids = []
    for el in found:
        for prop in ("unique-id", "device"):
            if el.find_property(prop) is not None:
                ids.append(str(el.get_property(prop)))
    return ids


with substitute(
    "reachy_mini.media.audio_gstreamer", AudioDevices("chosen-mic", "chosen-speaker")
):
    from reachy_mini.media.audio_gstreamer import GStreamerAudio

    try:
        audio = GStreamerAudio()
    except RuntimeError as e:  # no platform audio element on this host
        print(json.dumps({"skip": str(e)}))
        raise SystemExit(0)
    print(json.dumps({
        "session": chosen_ids(audio._pipeline),
        "sound_file": chosen_ids(audio._build_audiosink_element()),
    }))
"""


def test_upstream_s_audio_opens_the_chosen_devices_for_the_session_and_every_sound():
    """The mechanism rests on upstream looking the device up through
    ``audio_gstreamer.get_audio_device`` — when the client is built and for every sound
    file's sink: an upstream release that looks it up another way fails here, rather
    than the bridge opening the host's microphone without a word."""
    pytest.importorskip("gi")
    from reachy_mini_bridge.daemon import scrubbed_env

    done = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=scrubbed_env(),
    )
    answer = json.loads(done.stdout.strip().splitlines()[-1])
    if "skip" in answer:
        pytest.skip(f"no platform audio element: {answer['skip']}")
    assert "chosen-mic" in answer["session"]
    assert "chosen-speaker" in answer["session"]
    assert "chosen-speaker" in answer["sound_file"]


def _module_with_speaker() -> types.ModuleType:
    """An upstream audio module: its lookup, and a class building the speaker's sink."""
    module = _module()

    class Audio:
        def _build_audiosink_element(self) -> str:
            return "the host's speaker"

    Audio.__module__ = module.__name__
    module.Audio = Audio  # type: ignore[attr-defined]
    return module


def test_a_discarded_output_replaces_the_sink_builder_and_restores_it() -> None:
    """The output discarded, the class building the speaker's sink builds the discarding
    sink instead — the microphone's lookup still the substituted one — shared by holders
    of the same devices, refused to others, upstream's builder back with the last."""
    module = _module_with_speaker()
    audio = module.Audio()
    chosen = AudioDevices(source="blackhole-in", discard_sink=True)
    with substitute(module, chosen):
        assert module.Audio._build_audiosink_element is audio_devices.discarding_sink
        assert module.get_audio_device("Source") == "blackhole-in"
        with substitute(module, chosen):
            pass
        assert module.Audio._build_audiosink_element is audio_devices.discarding_sink
        with (
            pytest.raises(AudioDeviceError, match="already opens other audio devices"),
            substitute(module, AudioDevices(source="blackhole-in")),
        ):
            pass
    assert audio._build_audiosink_element() == "the host's speaker"
    assert module.get_audio_device("Source") == "upstream-Source"


def test_a_discarded_output_needs_a_sink_builder_to_replace() -> None:
    """An upstream release that builds its sink another way fails loudly, rather than
    the robot's sound reaching the host's speaker."""
    module = _module()
    with (
        pytest.raises(AudioDeviceError, match="no _build_audiosink_element"),
        substitute(module, AudioDevices(discard_sink=True)),
    ):
        pass
    assert module.get_audio_device("Sink") == "upstream-Sink"
    with pytest.raises(ValueError, match="names no"):
        AudioDevices(sink="speakers-id", discard_sink=True)
    with pytest.raises(ValueError, match="names no output device"):
        audio_devices.resolve_devices(None, "Speakers", discard_output=True)


_DISCARD_CHILD = """
import json
import time

from reachy_mini.media.audio_gstreamer import GStreamerAudio
from reachy_mini.media.media_server import GstMediaServer
from reachy_mini_bridge.audio_devices import AudioDevices, substitute

from gi.repository import Gst

BUILDER = "_build_audiosink_element"
upstream = (vars(GStreamerAudio)[BUILDER], vars(GstMediaServer)[BUILDER])


def sinks(element):
    it, found = element.iterate_recurse(), []
    while True:
        ok, el = it.next()
        if ok.value_nick != "ok":
            break
        if el.get_factory() is not None and el.get_factory().get_name().endswith("sink"):
            found.append(
                [el.get_factory().get_name(), bool(el.get_property("sync"))]
            )
    return found


def seconds_to_consume(sink, seconds):
    pipeline = Gst.Pipeline.new()
    src = Gst.ElementFactory.make("audiotestsrc")
    src.set_property("samplesperbuffer", 480)
    src.set_property("num-buffers", int(seconds * 100))
    caps = Gst.ElementFactory.make("capsfilter")
    caps.set_property("caps", Gst.Caps.from_string("audio/x-raw,rate=48000,channels=1"))
    for el in (src, caps, sink):
        pipeline.add(el)
    src.link(caps)
    caps.link(sink)
    start = time.monotonic()
    pipeline.set_state(Gst.State.PLAYING)
    msg = pipeline.get_bus().timed_pop_filtered(
        10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR
    )
    elapsed = time.monotonic() - start
    pipeline.set_state(Gst.State.NULL)
    assert msg is not None and msg.type == Gst.MessageType.EOS, msg
    return elapsed


discard = AudioDevices(source="chosen-mic", discard_sink=True)
with (
    substitute("reachy_mini.media.audio_gstreamer", discard),
    substitute("reachy_mini.media.media_server", discard),
):
    try:
        audio = GStreamerAudio()
    except RuntimeError as e:  # no platform audio element on this host
        print(json.dumps({"skip": str(e)}))
        raise SystemExit(0)
    answer = {
        "session": sinks(audio._pipeline),
        "sound_file": sinks(audio._build_audiosink_tee_bin()),
        "server": [
            GstMediaServer._build_audiosink_element(None).get_factory().get_name()
        ],
        "half_a_second": seconds_to_consume(audio._build_audiosink_element(), 0.5),
    }
answer["restored"] = (
    vars(GStreamerAudio)[BUILDER] is upstream[0]
    and vars(GstMediaServer)[BUILDER] is upstream[1]
)
print(json.dumps(answer))
"""


def test_a_discarded_output_is_a_real_time_fakesink_in_upstream_s_audio():
    """The mechanism rests on upstream building every speaker sink through
    ``_build_audiosink_element`` — the client's session pipeline, a sound file's sink,
    the daemon's media server — and the discarding sink consuming the audio at real-time
    pace (``say`` lasts as long as the speech): 0.5 s of audio takes 0.5 s to drop.
    Upstream's builders are back after."""
    pytest.importorskip("gi")
    from reachy_mini_bridge.daemon import scrubbed_env

    done = subprocess.run(
        [sys.executable, "-c", _DISCARD_CHILD],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=scrubbed_env(),
    )
    assert done.returncode == 0, done.stderr
    answer = json.loads(done.stdout.strip().splitlines()[-1])
    if "skip" in answer:
        pytest.skip(f"no platform audio element: {answer['skip']}")
    speaker = [name for name, _ in answer["session"] if name != "appsink"]
    assert speaker == ["fakesink"]
    assert ["fakesink", True] in answer["session"]
    assert ["fakesink", True] in answer["sound_file"]
    assert answer["server"] == ["fakesink"]
    assert 0.45 <= answer["half_a_second"] < 2.0
    assert answer["restored"]
