"""The documentation's runnable examples, executed as written.

The README's quick start and the testing guide's unit-test example are the first code a
new user runs; each is extracted from its Markdown file and executed on the ``fake`` so
neither can drift from the bridge again (specs/core/bridge.md "Front door";
docs/testing-with-the-bridge.md "Unit tests").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _first_python_block(path: Path, heading: str) -> str:
    """The first fenced ``python`` block under the Markdown ``heading`` of ``path``."""
    text = path.read_text()
    start = text.index(f"\n{heading}\n")
    match = re.search(r"```python\n(.*?)```", text[start:], re.DOTALL)
    assert match is not None, f"no python block under {heading!r} in {path}"
    return match.group(1)


def test_the_readme_quick_start_runs_on_the_fake() -> None:
    # The block ends with `asyncio.run(main())`: executing it runs the whole session —
    # motors, the emotion, a camera frame — on the default config, which names no
    # detector, so the quick start must not start tracking.
    code = _first_python_block(ROOT / "README.md", "## Quick start")
    assert "asyncio.run(main())" in code
    exec(compile(code, "README.md", "exec"), {"__name__": "__main__"})  # noqa: S102


def test_the_testing_guide_unit_example_passes() -> None:
    # The guide defines `my_greeting` (the consumer's code) and the test that measures
    # it through the fake's recorded targets; run that test exactly as a consumer would.
    code = _first_python_block(
        ROOT / "docs" / "testing-with-the-bridge.md",
        "## Unit tests — the `fake` backend",
    )
    namespace: dict[str, Any] = {"__name__": "docs_example"}
    exec(compile(code, "testing-with-the-bridge.md", "exec"), namespace)  # noqa: S102
    tests = [v for k, v in namespace.items() if k.startswith("test_") and callable(v)]
    assert len(tests) == 1, "the guide's example defines one test"
    tests[0]()
