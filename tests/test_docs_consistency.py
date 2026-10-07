"""The documentation's structural guards: one home per fact, and every pointer to it valid.

Every local Markdown link in the documentation resolves — its heading fragment to a
heading of the page it names — and every ``tests/...py::test_...`` reference names a test
that exists; every spec's and plan's ``**Status:**`` line matches its index row; the example config and the configuration
reference name every bridge-owned config field, and nothing else; every profile under
``examples/configs/`` is a valid ``ReachyMiniConfig``. Implementation plans are history
and are not link-checked; the spec and plan templates carry deliberate placeholders.
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

import pytest

from reachy_mini_bridge.config import (
    AudioSettings,
    DaemonConfig,
    FaceDetectionSettings,
    MotionSettings,
    ReachyMiniConfig,
    SimCameraSettings,
    SimDisplaySettings,
)

ROOT = Path(__file__).resolve().parent.parent
_TEMPLATES = {"_spec-template.md", "_plan-template.md"}
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_STATUS = re.compile(r"^\*\*Status:\*\*\s*(.+?)\s*$", re.MULTILINE)
# A `[name.md](path) | description | Status |` row of an index table.
_INDEX_ROW = re.compile(r"^\|\s*\[([^\]]+\.md)\]\([^)]+\)\s*\|.*\|\s*([^|]+?)\s*\|\s*$")


# The documentation proper: the root pages and the folders of Markdown the repo publishes.
# Scanned on disk rather than through `git ls-files`, so a page added or moved in the
# working tree is checked before it is staged.
_DOC_ROOTS = (
    "README.md",
    "AGENTS.md",
    "CONTRIBUTING.md",
    "docs",
    "specs",
    "plans",
    "examples",
)


def _documentation_markdown() -> list[Path]:
    found: list[Path] = []
    for root in _DOC_ROOTS:
        path = ROOT / root
        found += [path] if path.is_file() else sorted(path.rglob("*.md"))
    return found


# --- links ------------------------------------------------------------------------------


def test_every_local_markdown_link_resolves() -> None:
    broken: list[str] = []
    for path in _documentation_markdown():
        rel = path.relative_to(ROOT)
        if rel.parts[0] == "plans" or path.name in _TEMPLATES:
            continue
        for match in _LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0]
            if target and not (path.parent / target).exists():
                broken.append(f"{rel}: {target}")
    assert not broken, "local links to missing files:\n" + "\n".join(broken)


def _heading_slugs(page: Path) -> set[str]:
    """The fragments the page's headings answer to, as GitHub derives them: lowercase,
    punctuation dropped, spaces to hyphens, a repeated heading numbered from ``-1``."""
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    in_code = False
    for line in page.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_code = not in_code
        if in_code or not re.match(r"#{1,6} ", line):
            continue
        text = line.lstrip("#").strip().replace("`", "")
        slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        slugs.add(slug if count == 0 else f"{slug}-{count}")
    return slugs


def test_every_heading_fragment_names_a_heading_of_its_page() -> None:
    broken: list[str] = []
    for path in _documentation_markdown():
        rel = path.relative_to(ROOT)
        if rel.parts[0] == "plans" or path.name in _TEMPLATES:
            continue
        for match in _LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1)
            if (
                target.startswith(("http://", "https://", "mailto:"))
                or "#" not in target
            ):
                continue
            page, fragment = target.split("#", 1)
            page_path = path if not page else path.parent / page
            if not page_path.is_file() or page_path.suffix != ".md":
                continue  # a missing file is the link test's finding
            if fragment not in _heading_slugs(page_path):
                broken.append(f"{rel}: {target}")
    assert not broken, "heading fragments with no heading:\n" + "\n".join(broken)


_TEST_REFERENCE = re.compile(r"(tests(?:-e2e)?/[\w/.-]+\.py)::(test_\w+)")


def test_every_test_reference_names_a_test_that_exists() -> None:
    broken: list[str] = []
    for path in _documentation_markdown():
        rel = path.relative_to(ROOT)
        if rel.parts[0] == "plans":
            continue
        for match in _TEST_REFERENCE.finditer(path.read_text(encoding="utf-8")):
            module, name = ROOT / match.group(1), match.group(2)
            if not module.is_file() or f"def {name}(" not in module.read_text(
                encoding="utf-8"
            ):
                broken.append(f"{rel}: {match.group(0)}")
    assert not broken, "references to tests that do not exist:\n" + "\n".join(broken)


# --- statuses ---------------------------------------------------------------------------


def _index_statuses(index: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in index.read_text(encoding="utf-8").splitlines():
        match = _INDEX_ROW.match(line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def _file_status(path: Path) -> str:
    """The status word of a spec or plan: a ``**Status:**`` line may carry a note after
    an em dash (``Done — built and seen live on …``); the index row carries the word."""
    match = _STATUS.search(path.read_text(encoding="utf-8"))
    assert match is not None, f"{path.relative_to(ROOT)} has no **Status:** line"
    return match.group(1).split(" — ", 1)[0].strip()


@pytest.mark.parametrize("folder", ["specs", "plans"])
def test_every_status_line_matches_its_index_row(folder: str) -> None:
    index = ROOT / folder / "_index.md"
    rows = _index_statuses(index)
    files = [p for p in (ROOT / folder).rglob("*.md") if not p.name.startswith("_")]
    assert files, f"no {folder} found"
    mismatches: list[str] = []
    for path in files:
        name = path.name
        if name not in rows:
            mismatches.append(f"{path.relative_to(ROOT)}: no row in {folder}/_index.md")
            continue
        status = _file_status(path)
        if status != rows[name]:
            mismatches.append(
                f"{path.relative_to(ROOT)}: file says {status!r}, index says {rows[name]!r}"
            )
    stale_rows = set(rows) - {p.name for p in files}
    mismatches += [
        f"{folder}/_index.md: row for a missing file {name}" for name in stale_rows
    ]
    assert not mismatches, "\n".join(mismatches)


# --- the config inventory ---------------------------------------------------------------

# Fields set from code, never from a file (specs/core/config.md): a JSON key is a ConfigError.
_CODE_ONLY_FIELDS = {"face_detector", "idle_move"}


def _fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)} - _CODE_ONLY_FIELDS


# Every bridge-owned block, by its JSON path, to the dataclass that defines its fields.
# `robot` (upstream's kwargs) and `tts` (tts-engine's block) carry foreign keys.
_BLOCKS: dict[str, type] = {
    "": ReachyMiniConfig,
    "daemon": DaemonConfig,
    "daemon.camera": SimCameraSettings,
    "daemon.sim_displays": SimDisplaySettings,
    "audio": AudioSettings,
    "face_detection": FaceDetectionSettings,
    "motion": MotionSettings,
}


def _block(data: dict[str, object], path: str) -> dict[str, object]:
    for key in filter(None, path.split(".")):
        value = data[key]
        assert isinstance(value, dict), f"{path} is not an object"
        data = value
    return data


def test_the_example_config_names_every_field_once() -> None:
    data = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    for path, cls in _BLOCKS.items():
        assert set(_block(data, path)) == _fields(cls), (
            f"config.example.json block {path or '<top level>'} must list exactly the "
            f"fields of {cls.__name__}"
        )


def _reference_section(text: str, block: str) -> str:
    """The configuration reference's text for ``block``, from its heading to the next."""
    heading = block.split(".")[-1]
    if "." in block:
        # A sub-block has a bold lead-in inside its parent's section (`**`daemon.camera`**`).
        start = text.index(f"**`{block}`**")
    else:
        start = text.index(f"\n## `{heading}`")
    rest = text[start + 1 :]
    end = re.search(r"\n(## |\*\*`[a-z_.]+`\*\*)", rest)
    return rest[: end.start()] if end else rest


def test_the_configuration_reference_documents_every_field() -> None:
    text = (ROOT / "docs" / "reference" / "configuration.md").read_text(
        encoding="utf-8"
    )
    missing: list[str] = []
    for path, cls in _BLOCKS.items():
        for field in sorted(_fields(cls)):
            if path == "":
                # Top-level blocks are the page's `## \`block\`` sections.
                if f"\n## `{field}`" not in text:
                    missing.append(field)
                continue
            section = _reference_section(text, path)
            if f"| `{field}` |" not in section:
                missing.append(f"{path}.{field}")
    assert not missing, f"configuration.md documents no row for: {missing}"


# --- the profiles -----------------------------------------------------------------------

_PROFILES = sorted((ROOT / "examples" / "configs").glob("*.json"))


@pytest.mark.parametrize("profile", _PROFILES, ids=lambda p: p.name)
def test_every_profile_is_a_valid_config(profile: Path) -> None:
    config = ReachyMiniConfig.from_json_file(profile)
    assert config.backend in {"real", "sim", "fake"}


@pytest.mark.parametrize(
    "profile", [p for p in _PROFILES if p.name.startswith("sim-")], ids=lambda p: p.name
)
def test_the_readmes_headless_edit_keeps_a_sim_profile_valid(profile: Path) -> None:
    """The profiles README tells a reader to run a sim profile headless by setting
    ``headless`` *and* dropping the camera overlay; that documented edit must parse."""
    data = json.loads(profile.read_text(encoding="utf-8"))
    data["daemon"]["headless"] = True
    data["daemon"]["sim_displays"] = {"camera_overlay": False}
    config = ReachyMiniConfig.from_dict(data)
    assert config.daemon.headless
    assert not config.daemon.sim_displays.camera_overlay


def test_every_profile_is_listed_in_its_readme() -> None:
    readme = (ROOT / "examples" / "configs" / "README.md").read_text(encoding="utf-8")
    assert _PROFILES, "no profiles under examples/configs/"
    unlisted = [p.name for p in _PROFILES if f"[{p.name}]({p.name})" not in readme]
    assert not unlisted, f"examples/configs/README.md does not list: {unlisted}"
