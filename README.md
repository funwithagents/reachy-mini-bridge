# reachy-mini-bridge

A bridge to the [Reachy Mini](https://github.com/pollen-robotics/reachy_mini) robot's API: it wraps the native SDK, adds a high-level interaction API (`ReachyMiniApi`) on top, and exposes that API as agent/LLM tools (`ReachyMiniTools`) — so callers never talk to the raw robot API directly.

> **Status: layers 0–1 built.** The connection seam, the interaction API (`ReachyMiniApi`), and the audio/media session are implemented and run on the `real` / `sim` / `fake` backends; the agent-tools layer (`ReachyMiniTools`) is still in design. See [specs/_index.md](specs/_index.md) for per-spec status.

## Documentation

- **[specs/_overview.md](specs/_overview.md)** — global view of the project: architecture, the three layers, the backends. Start here.
- **[specs/_index.md](specs/_index.md)** — index of the specs and their status.
- **[AGENTS.md](AGENTS.md)** — operating manual: how specs/plans/statuses work, commands, verification.
- **[docs/testing-with-the-bridge.md](docs/testing-with-the-bridge.md)** — for projects that depend on the bridge: how to test your own code against the `fake` / `sim` / `real` backends (unit and e2e).
- **[docs/reachy-mini-api.md](docs/reachy-mini-api.md)** — reference notes on the upstream `reachy_mini` SDK.

## Development

```
uv sync --dev
uv run ruff check .
uv run ruff format .
uv run pyright
uv run pytest
```
