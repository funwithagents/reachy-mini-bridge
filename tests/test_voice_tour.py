"""The voice tour on the ``fake``: tone voices stand in for the providers, so the parts run
through the real ``TTSEngineSynthesizer`` and the bridge's real ``say`` timing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from tts_engine.modules import REGISTRY

from examples.voice_tour.app import (
    DEFAULT_TEXT,
    VOICES,
    Beat,
    Line,
    Part,
    load_parts,
    run,
)
from reachy_mini_bridge import ReachyMiniConfig

FAKE = ReachyMiniConfig.from_dict({"backend": "fake"})
# The test signal, a file player, and the adapter to a server of your own: no voice of theirs.
NOT_SPEECH_VOICES = {"tone", "audiofile", "openai-compatible"}


def write_voice(voices_dir: Path, name: str, module: dict[str, object]) -> None:
    (voices_dir / f"{name}.json").write_text(json.dumps({"tts": {"module": module}}))


async def go_now() -> None:
    return None


def tour_parts(*names: str) -> list[Part]:
    return [Part(name, name.title(), (Line(f"Line of {name}."),)) for name in names]


@pytest.fixture
def voices_dir(tmp_path: Path) -> Path:
    write_voice(tmp_path, "one", {"type": "tone"})
    write_voice(tmp_path, "two", {"type": "tone"})
    write_voice(tmp_path, "hosted", {"type": "tone", "api_key_env": "NOT_READ_BY_TONE"})
    return tmp_path


def test_the_parts_play_in_order_with_their_times(voices_dir: Path) -> None:
    beats = asyncio.run(run(FAKE, tour_parts("one", "two"), voices_dir, go=go_now))

    assert [beat.voice for beat in beats] == ["one", "two"]
    first, second = beats
    assert 0 <= first.start_s < first.end_s <= second.start_s < second.end_s
    assert second.gap_before_s == pytest.approx(second.start_s - first.end_s, abs=0.002)
    assert first.ttfa_ms > 0
    assert first.lines == ("Line of one.",)


def test_the_pauses_separate_the_voices_and_a_voice_s_lines(voices_dir: Path) -> None:
    parts = [
        Part("one", "One", (Line("First line."), Line("Second line."))),
        Part("two", "Two", (Line("Third line."),)),
    ]

    async def timed(voice_pause_s: float, line_pause_s: float) -> list[Beat]:
        return await run(
            FAKE,
            parts,
            voices_dir,
            voice_pause_s=voice_pause_s,
            line_pause_s=line_pause_s,
            go=go_now,
        )

    first, second = asyncio.run(timed(0.6, 0.2))

    assert len(first.line_starts_s) == 2
    assert first.line_starts_s[0] == first.start_s
    assert first.start_s < first.line_starts_s[1] < first.end_s
    assert second.line_starts_s == (second.start_s,)
    assert second.gap_before_s >= 0.6
    tight_first, tight_second = asyncio.run(timed(0.0, 0.0))
    assert tight_second.gap_before_s < second.gap_before_s
    line_gap = first.line_starts_s[1] - first.line_starts_s[0]
    assert line_gap > tight_first.line_starts_s[1] - tight_first.line_starts_s[0]


def test_a_line_s_own_pause_replaces_the_tour_s(voices_dir: Path) -> None:
    parts = [
        Part("one", "One", (Line("First line."), Line("Second.", pause_s=0.7))),
        Part("two", "Two", (Line("Third line.", pause_s=0.0),)),
    ]

    first, second = asyncio.run(
        run(FAKE, parts, voices_dir, voice_pause_s=1.0, line_pause_s=0.0, go=go_now)
    )
    tight_first, _ = asyncio.run(
        run(
            FAKE,
            [Part("one", "One", (Line("First line."), Line("Second.")))] + parts[1:],
            voices_dir,
            voice_pause_s=1.0,
            line_pause_s=0.0,
            go=go_now,
        )
    )

    assert second.gap_before_s < 0.5
    line_gap = first.line_starts_s[1] - first.line_starts_s[0]
    tight_line_gap = tight_first.line_starts_s[1] - tight_first.line_starts_s[0]
    assert line_gap - tight_line_gap == pytest.approx(0.7, abs=0.15)


def test_a_text_reads_plain_lines_and_lines_with_a_pause(tmp_path: Path) -> None:
    text = tmp_path / "text.json"
    lines: list[object] = ["Plain.", {"text": "Paused.", "pause_s": 0.2}]
    text.write_text(
        json.dumps({"parts": [{"voice": "v", "label": "V", "lines": lines}]})
    )

    assert load_parts(text)[0].lines == (Line("Plain."), Line("Paused.", 0.2))


def test_a_negative_pause_is_refused(tmp_path: Path) -> None:
    text = tmp_path / "text.json"
    lines = [{"text": "Early.", "pause_s": -0.2}]
    text.write_text(
        json.dumps({"parts": [{"voice": "v", "label": "V", "lines": lines}]})
    )

    with pytest.raises(ValueError, match="negative pause_s"):
        load_parts(text)


def test_a_voice_that_cannot_be_built_is_dropped_and_the_tour_goes_on(
    voices_dir: Path,
) -> None:
    write_voice(voices_dir, "broken", {"type": "no-such-module"})

    parts = tour_parts("one", "broken", "absent", "two")
    beats = asyncio.run(run(FAKE, parts, voices_dir, go=go_now))

    assert [(beat.part, beat.voice) for beat in beats] == [(1, "one"), (4, "two")]


def test_local_only_leaves_the_hosted_voices_out(voices_dir: Path) -> None:
    parts = tour_parts("one", "hosted", "two")

    everyone = asyncio.run(run(FAKE, parts, voices_dir, go=go_now))
    local = asyncio.run(run(FAKE, parts, voices_dir, local_only=True, go=go_now))

    assert [(beat.voice, beat.hosted) for beat in everyone] == [
        ("one", False),
        ("hosted", True),
        ("two", False),
    ]
    assert [beat.voice for beat in local] == ["one", "two"]


def test_nothing_is_performed_before_the_go(voices_dir: Path) -> None:
    async def scenario() -> list[str]:
        waiting = asyncio.Event()
        release = asyncio.Event()

        async def go() -> None:
            waiting.set()
            await release.wait()

        tour = asyncio.create_task(run(FAKE, tour_parts("one"), voices_dir, go=go))
        await waiting.wait()
        await asyncio.sleep(0.05)
        assert not tour.done()
        release.set()
        return [beat.voice for beat in await tour]

    assert asyncio.run(scenario()) == ["one"]


def test_no_voice_left_starts_no_session(tmp_path: Path) -> None:
    async def never() -> None:
        raise AssertionError("no go without a voice")

    assert asyncio.run(run(FAKE, tour_parts("absent"), tmp_path, go=never)) == []


def test_every_part_of_the_default_text_has_a_voice_file_with_a_gain() -> None:
    for part in load_parts(DEFAULT_TEXT):
        tts = ReachyMiniConfig.from_json_file(VOICES / f"{part.voice}.json").tts
        assert tts is not None
        assert "gain_db" in tts


def test_the_default_text_gives_every_speech_module_one_voice() -> None:
    modules: list[str] = []
    for part in load_parts(DEFAULT_TEXT):
        tts = ReachyMiniConfig.from_json_file(VOICES / f"{part.voice}.json").tts
        assert tts is not None
        modules.append(tts["module"]["type"])

    assert sorted(modules) == sorted(
        {name for name in REGISTRY if name not in NOT_SPEECH_VOICES}
    )
