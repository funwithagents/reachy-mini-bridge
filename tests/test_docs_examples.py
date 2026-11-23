"""The documentation's runnable examples, executed as written.

The first code a new user runs is extracted from its Markdown file and executed on the
``fake`` so none of it can drift from the bridge: the README's quick start, the
getting-started application (with its beep synthesizer), the custom idle move guide's
move, the custom face detector guide's program, the tracing guide's program, and the testing guide's unit-test example (specs/core/bridge.md "Front door";
docs/guides/testing.md "Unit tests").
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _first_python_block(path: Path, heading: str) -> str:
    """The first fenced ``python`` block under the Markdown ``heading`` of ``path``."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"\n{heading}\n")
    match = re.search(r"```python\n(.*?)```", text[start:], re.DOTALL)
    assert match is not None, f"no python block under {heading!r} in {path}"
    return match.group(1)


def _run_as_main(path: Path, heading: str) -> None:
    # The block ends with `asyncio.run(main())`: executing it runs the whole session.
    code = _first_python_block(path, heading)
    assert "asyncio.run(main())" in code
    exec(compile(code, path.name, "exec"), {"__name__": "__main__"})  # noqa: S102


def test_the_readme_quick_start_runs_on_the_fake() -> None:
    # Motors, the emotion, a camera frame — on the default config, which names no
    # detector, so the quick start must not start tracking.
    _run_as_main(ROOT / "README.md", "## Quick start")


def test_the_getting_started_application_runs_on_the_fake() -> None:
    # The complete first application: motors, an emotion, `say` through the page's own
    # synthesizer, one mic chunk, a camera frame.
    _run_as_main(ROOT / "docs" / "getting-started.md", "## Your first application")


def test_the_custom_idle_move_guide_runs_on_the_fake() -> None:
    # Registers the guide's move on a session in the custom idle mode and lets it play.
    _run_as_main(ROOT / "docs" / "guides" / "custom-idle-move.md", "## The move")


def test_the_custom_face_detector_guide_runs_on_the_fake() -> None:
    # A stand-in detector registered through `custom`: the report fills from the fake's
    # frames and the tracker engages the face it reports.
    _run_as_main(
        ROOT / "docs" / "guides" / "custom-face-detector.md", "## A complete program"
    )


def test_the_tracing_guide_prints_each_span_with_its_steps() -> None:
    # In a process of its own: the program installs the OpenTelemetry SDK's provider,
    # which a process can set once. What it prints is what the guide then reads.
    code = _first_python_block(
        ROOT / "docs" / "guides" / "tracing.md", "## See the spans"
    )
    assert "asyncio.run(main())" in code
    done = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    lines = {line.split(":")[0]: line for line in done.stdout.splitlines()}

    assert list(lines)[-1] == "reaction"  # the caller's span closes over the bridge's
    say = lines["reachy_mini_bridge.say"]
    assert " completed | first_push +" in say and ", playback_ended +" in say
    emotion = lines["reachy_mini_bridge.play_emotion"]
    steps = ["queued", "motion_started", "trajectory_started", "trajectory_ended"]
    assert [emotion.index(step) for step in steps] == sorted(
        emotion.index(step) for step in steps
    )
    assert " completed | queued +" in emotion


def test_the_testing_guide_unit_example_passes() -> None:
    # The guide defines `my_greeting` (the consumer's code) and the test that measures
    # it through the fake's recorded targets; run that test exactly as a consumer would.
    code = _first_python_block(
        ROOT / "docs" / "guides" / "testing.md",
        "## Unit tests — the `fake` backend",
    )
    namespace: dict[str, Any] = {"__name__": "docs_example"}
    exec(compile(code, "testing.md", "exec"), namespace)  # noqa: S102
    tests = [v for k, v in namespace.items() if k.startswith("test_") and callable(v)]
    assert len(tests) == 1, "the guide's example defines one test"
    tests[0]()
